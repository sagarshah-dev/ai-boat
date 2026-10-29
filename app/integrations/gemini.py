"""Google Gemini, behind a small interface the agent depends on (LLM / Chat / Turn).

The agent never imports the Gemini SDK. Tests use a scripted fake with the same interface, and
switching provider later means writing one more class here, not touching the agent.

Retry rules carried over from the old bot (main.py _generate):
- only the model call is retried, never a tool — so a retry can never save an expense twice;
- 429 / 5xx / timeouts are retried with short waits, then the fallback model is tried;
- a timeout on the main model moves straight to the fallback (waiting again rarely helps);
- one total time budget per message, so a slow day ends with a clear error, not a hang.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 20
RETRY_WAITS_S = (1, 3)
RETRYABLE_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ToolSpec:
    """Provider-neutral tool description. `parameters` is an OpenAPI-style object schema."""
    name: str
    description: str
    parameters: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Turn:
    calls: list[ToolCall]
    text: str
    tokens: int = 0


class LlmError(Exception):
    """The model could not answer. `kind` decides what the user is told."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


class Chat(Protocol):
    def send_user(self, text: str) -> Turn: ...
    def send_tool_results(self, results: list[tuple[str, dict]]) -> Turn: ...
    def send_media(self, text: str, data: bytes, mime_type: str) -> Turn: ...


class LLM(Protocol):
    embed_model: str

    def start(self, system: str, tools: list[ToolSpec], budget_s: float) -> Chat: ...

    def complete(self, system: str, prompt: str, *, json_schema: dict | None = None,
                 temperature: float = 0.1, budget_s: float = 30) -> str: ...

    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]: ...


EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768          # the rag_chunks column is vector(768); changing it needs a migration
EMBED_BATCH = 50


def normalise(v: list[float]) -> list[float]:
    """Gemini returns unit vectors only at full size; at 768 dims it does not, and comparing
    un-normalised vectors makes long notes look "closer" than short ones for no reason."""
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def error_kind(err: Exception) -> str:
    """Why the call failed, in words the user can act on. The old bot said 'overloaded' for
    everything; on 26-10 the real cause was a timeout."""
    code = getattr(err, "code", None) if isinstance(err, genai_errors.APIError) else None
    name = type(err).__name__.lower()
    if code == 504 or "timeout" in name or "timedout" in name:
        return "timeout"
    if code == 429:
        return "quota"
    if code in (500, 502, 503):
        return "overloaded"
    if code in (400, 401, 403, 404):
        return "config"
    return "unknown"


def _retryable(err: Exception) -> bool:
    kind = error_kind(err)
    return kind in ("timeout", "quota", "overloaded")


def _declaration(spec: ToolSpec) -> types.FunctionDeclaration:
    params = types.Schema.model_validate(spec.parameters) if spec.parameters else None
    return types.FunctionDeclaration(name=spec.name, description=spec.description, parameters=params)


class GeminiChat:
    def __init__(self, client: Any, models: list[str], system: str, tools: list[ToolSpec],
                 budget_s: float, sleep=time.sleep, clock=time.monotonic,
                 json_schema: dict | None = None, temperature: float = 0.2):
        self._client = client
        self._models = models
        self._sleep, self._clock = sleep, clock
        self._deadline = clock() + budget_s
        self._contents: list[types.Content] = []
        self._config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[_declaration(t) for t in tools])] if tools else None,
            temperature=temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # A fixed answer form (RAG): the model fills fields, code checks every one of them.
            response_mime_type="application/json" if json_schema else None,
            response_schema=types.Schema.model_validate(json_schema) if json_schema else None,
        )
        self.model_used: str = models[0]

    def send_user(self, text: str) -> Turn:
        self._contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        return self._next()

    def send_media(self, text: str, data: bytes, mime_type: str) -> Turn:
        """An instruction plus a photo or voice note in one user turn."""
        self._contents.append(types.Content(role="user", parts=[
            types.Part.from_text(text=text), types.Part.from_bytes(data=data, mime_type=mime_type)]))
        return self._next()

    def escalate(self) -> None:
        """From the next call on, ask the fallback (stronger) model first. Used when the main
        model's answer was incomplete, not failed: retrying the same model rarely changes it."""
        if len(self._models) > 1:
            self._models = self._models[1:] + self._models[:1]

    def send_tool_results(self, results: list[tuple[str, dict]]) -> Turn:
        parts = [types.Part.from_function_response(
            # Round-trip through JSON so Decimal / date values become plain JSON for the API.
            name=name, response={"result": json.loads(json.dumps(result, default=str))})
            for name, result in results]
        self._contents.append(types.Content(role="user", parts=parts))
        return self._next()

    def _next(self) -> Turn:
        resp = self._generate()
        candidate = resp.candidates[0].content if resp.candidates else None
        if candidate is not None:
            # History is only extended with a COMPLETE model turn, so a failed call can never
            # leave half a turn behind for the next attempt.
            self._contents.append(candidate)
        calls = [ToolCall(fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
        tokens = (resp.usage_metadata.total_token_count or 0) if resp.usage_metadata else 0
        text = "" if calls else (resp.text or "").strip()
        return Turn(calls=calls, text=text, tokens=tokens)

    def _generate(self):
        last: Exception | None = None
        for model in self._models:
            for attempt in range(len(RETRY_WAITS_S) + 1):
                if self._clock() + CALL_TIMEOUT_S > self._deadline:
                    raise LlmError(error_kind(last) if last else "timeout", "time budget used up")
                try:
                    resp = self._client.models.generate_content(
                        model=model, contents=self._contents, config=self._config)
                    self.model_used = model
                    return resp
                except Exception as err:  # noqa: BLE001 — classified below, re-raised as LlmError
                    last = err
                    if not _retryable(err):
                        raise LlmError(error_kind(err), str(err)) from err
                    if error_kind(err) == "timeout" and model != self._models[-1]:
                        log.warning("gemini timeout, moving to next model", extra={"fields": {"model": model}})
                        break
                    if attempt < len(RETRY_WAITS_S):
                        log.warning("gemini retry", extra={"fields": {
                            "model": model, "code": getattr(err, "code", None), "attempt": attempt + 1}})
                        self._sleep(RETRY_WAITS_S[attempt])
        assert last is not None
        raise LlmError(error_kind(last), str(last)) from last


@dataclass
class GeminiLLM:
    api_key: str
    model: str
    fallback_model: str = ""
    _client: Any = field(default=None, repr=False)

    def __post_init__(self):
        if self._client is None:
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=CALL_TIMEOUT_S * 1000))

    embed_model: str = EMBED_MODEL

    def _models(self) -> list[str]:
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)
        return models

    def start(self, system: str, tools: list[ToolSpec], budget_s: float) -> GeminiChat:
        return GeminiChat(self._client, self._models(), system, tools, budget_s)

    def complete(self, system: str, prompt: str, *, json_schema: dict | None = None,
                 temperature: float = 0.1, budget_s: float = 30) -> str:
        """One question, one answer, same retry rules as the chat."""
        chat = GeminiChat(self._client, self._models(), system, [], budget_s,
                          json_schema=json_schema, temperature=temperature)
        return chat.send_user(prompt).text

    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        """Texts -> unit vectors, batched. Retried only on temporary errors; anything else is an
        LlmError and the caller decides what "not indexed yet" means."""
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            for attempt in range(len(RETRY_WAITS_S) + 1):
                try:
                    resp = self._client.models.embed_content(
                        model=self.embed_model, contents=batch,
                        config=types.EmbedContentConfig(task_type=task, output_dimensionality=EMBED_DIM))
                    out.extend(normalise(list(e.values)) for e in resp.embeddings)
                    break
                except Exception as err:  # noqa: BLE001 — classified, re-raised as LlmError
                    if not _retryable(err) or attempt == len(RETRY_WAITS_S):
                        raise LlmError(error_kind(err), str(err)) from err
                    time.sleep(RETRY_WAITS_S[attempt])
        return out
