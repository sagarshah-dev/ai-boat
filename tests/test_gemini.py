"""The Gemini wrapper's retry rules, against a fake SDK client (no network)."""
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.integrations.gemini import GeminiChat, LlmError, ToolSpec, error_kind


def ok_response(text="hi", calls=None):
    fcs = [SimpleNamespace(name=n, args=a) for n, a in (calls or [])]
    return SimpleNamespace(
        candidates=[SimpleNamespace(content="model-turn")], function_calls=fcs or None,
        text=text, usage_metadata=SimpleNamespace(total_token_count=42))


class FakeModels:
    def __init__(self, outcomes, clock=None, cost_s=0.0):
        self.outcomes = list(outcomes)
        self.models_called = []
        self.timeouts_ms = []        # per-call timeout (None = the client's 20 s default)
        self._clock, self._cost = clock, cost_s

    def generate_content(self, model, contents, config):
        self.models_called.append(model)
        self.timeouts_ms.append(config.http_options.timeout if config.http_options else None)
        if self._clock is not None:
            self._clock["now"] += self._cost   # each call takes this long
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def chat(outcomes, models=("main",), budget=55.0, clock=None):
    fake = SimpleNamespace(models=FakeModels(outcomes))
    tools = [ToolSpec("add_expense", "add", {"type": "OBJECT", "properties": {
        "amount": {"type": "NUMBER"}, "paid_by": {"type": "STRING", "enum": ["Sagar"]}}})]
    c = GeminiChat(fake, list(models), "system", tools, budget, sleep=lambda s: None,
                   clock=clock or (lambda: 0.0))
    return c, fake.models


def server(code):
    return errors.ServerError(code, {"error": {"message": "x"}})


def client_err(code):
    return errors.ClientError(code, {"error": {"message": "x"}})


def test_plain_answer():
    c, m = chat([ok_response("hello")])
    turn = c.send_user("hi")
    assert (turn.text, turn.calls, turn.tokens) == ("hello", [], 42)


def test_tool_call_is_returned_not_text():
    c, _ = chat([ok_response(text="ignored", calls=[("add_expense", {"amount": 5})])])
    turn = c.send_user("5 tea")
    assert turn.calls[0].name == "add_expense" and turn.calls[0].args == {"amount": 5}
    assert turn.text == ""


def test_overloaded_is_retried_then_succeeds():
    c, m = chat([server(503), server(503), ok_response()])
    assert c.send_user("hi").text == "hi"
    assert m.models_called == ["main", "main", "main"]


def test_overloaded_three_times_moves_to_fallback():
    c, m = chat([server(503)] * 3 + [ok_response()], models=("main", "backup"))
    c.send_user("hi")
    assert m.models_called == ["main"] * 3 + ["backup"]
    assert c.model_used == "backup"


def test_timeout_skips_straight_to_fallback():
    c, m = chat([server(504), ok_response()], models=("main", "backup"))
    c.send_user("hi")
    assert m.models_called == ["main", "backup"]


def test_bad_request_is_not_retried():
    c, m = chat([client_err(400)])
    with pytest.raises(LlmError) as e:
        c.send_user("hi")
    assert e.value.kind == "config" and m.models_called == ["main"]


def test_gives_up_with_the_right_kind():
    c, _ = chat([client_err(429)] * 3)
    with pytest.raises(LlmError) as e:
        c.send_user("hi")
    assert e.value.kind == "quota"


def test_time_budget_stops_when_too_little_is_left():
    t = {"now": 0.0}
    c, m = chat([server(503)] * 5, budget=30.0, clock=lambda: t["now"])
    t["now"] = 23.0           # 7 s left: below MIN_ATTEMPT_S, a call that short mostly times out
    with pytest.raises(LlmError):
        c.send_user("hi")
    assert m.models_called == []


def timed_chat(outcomes, budget, cost_s, models=("main",)):
    t = {"now": 0.0}
    fake = SimpleNamespace(models=FakeModels(outcomes, clock=t, cost_s=cost_s))
    c = GeminiChat(fake, list(models), "system", [], budget, sleep=lambda s: None,
                   clock=lambda: t["now"])
    return c, fake.models


def test_rag_budget_can_still_retry_after_a_slow_failure():
    """Live 05-11: the RAG answer has 25 s. With a fixed 20 s per call, one failure taking 5 s
    left 20 s, the check said "a 20 s call would overrun", and nothing was retried."""
    c, m = timed_chat([server(503), ok_response("jawab")], budget=25.0, cost_s=5.0)
    assert c.send_user("hi").text == "jawab"
    assert m.models_called == ["main", "main"]
    assert m.timeouts_ms == [None, None]                # 20 s were still left: full timeout


def test_retry_gets_only_the_time_that_is_left():
    c, m = timed_chat([server(503), ok_response()], budget=25.0, cost_s=12.0)
    c.send_user("hi")
    assert m.timeouts_ms == [None, 13_000]              # 25 - 12 s left, not 20 s


def test_fallback_model_is_reached_inside_the_rag_budget():
    c, m = timed_chat([server(504), ok_response()], budget=25.0, cost_s=6.0,
                      models=("main", "backup"))
    c.send_user("hi")                                   # timeout on main -> straight to backup
    assert m.models_called == ["main", "backup"] and m.timeouts_ms == [None, 19_000]


@pytest.fixture
def gemini_log():
    """setup_logging() replaces the root handlers, so listen on the module's logger directly."""
    import logging

    class Keep(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    lg, h = logging.getLogger("app.integrations.gemini"), Keep()
    # alembic's fileConfig (run by the test setup) disables loggers that already exist.
    was_off, lg.disabled = lg.disabled, False
    lg.addHandler(h)
    yield h
    lg.removeHandler(h)
    lg.disabled = was_off


def test_failure_is_logged_with_its_reason(gemini_log):
    caplog = gemini_log
    c, _ = chat([client_err(400)])
    with pytest.raises(LlmError):
        c.send_user("hi")
    rec = [r for r in caplog.records if r.getMessage() == "gemini call failed"]
    assert rec and rec[0].fields["kind"] == "config" and rec[0].fields["code"] == 400
    assert rec[0].fields["error"] == "ClientError" and "x" in rec[0].fields["detail"]


def test_budget_give_up_is_logged_with_the_last_reason(gemini_log):
    caplog = gemini_log
    t = {"now": 0.0}
    c, _ = chat([server(503)] * 5, budget=30.0, clock=lambda: t["now"])
    t["now"] = 25.0
    with pytest.raises(LlmError):
        c.send_user("hi")
    rec = [r for r in caplog.records if r.getMessage() == "gemini call failed"]
    assert rec and rec[0].fields["detail"] == "time budget used up"


@pytest.mark.parametrize("err,kind", [
    (server(503), "overloaded"), (server(500), "overloaded"), (server(504), "timeout"),
    (client_err(429), "quota"), (client_err(403), "config"), (ValueError("x"), "unknown"),
    (TimeoutError("x"), "timeout"),
])
def test_error_kind(err, kind):
    assert error_kind(err) == kind


@pytest.mark.parametrize("people", [["Sagar", "Biren", "Lalo"], []])
def test_real_tool_specs_convert_to_gemini_declarations(people):
    from app.ai.tools import expense_tools
    from app.integrations.gemini import _declaration
    decls = [_declaration(t) for t in expense_tools(people)]
    assert [d.name for d in decls] == ["add_expenses", "get_balances", "get_recent",
                                       "search_expenses", "delete_last_expense",
                                       "edit_last_expense"]
    item = decls[0].parameters.properties["items"].items
    assert item.properties["paid_by"].enum == (people or None)
