"""Test setup: a real Postgres (with pgvector), migrated to head, wiped before every test.

Real Postgres, not SQLite: the app uses Postgres-only features (ON CONFLICT, JSONB, arrays,
vector), and a test that passes on a different database proves nothing.

TEST_DATABASE_URL must point at a throwaway database — every table is emptied per test.
In CI it is the Postgres container from .github/workflows/tests.yml.
"""
import os

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
if not TEST_DB:
    pytest.exit("TEST_DATABASE_URL is not set (see README, 'Running tests')", returncode=2)
if TEST_DB == os.environ.get("DATABASE_URL"):
    pytest.exit("TEST_DATABASE_URL equals DATABASE_URL — refusing to wipe the real database",
                returncode=2)

ADMIN_ID = 1001
SETTLE_PIN = "7777"
MCP_PASSWORD = "test-owner-password-123"

# Settings are read from the environment; set them before the app is imported.
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "TELEGRAM_TOKEN": "test-token-not-real",
    "TELEGRAM_WEBHOOK_SECRET": "test-secret-0123456789",
    "ADMIN_TELEGRAM_ID": str(ADMIN_ID),
    "GEMINI_API_KEY": "test-key-not-real",
    "SETTLE_PIN": SETTLE_PIN,
    # The fake embedder below is cruder than Gemini's, so its similarity scores are lower. The
    # real threshold (0.62) is measured with the real model by the evals, not by these tests.
    "RAG_MIN_SCORE": "0.3",
    # MCP on in every test, so its routes are proven not to shadow the bot's.
    "PUBLIC_BASE_URL": "https://testserver",
    "MCP_OWNER_PASSWORD": MCP_PASSWORD,
})

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import get_engine  # noqa: E402


def alembic_config() -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.attributes["database_url"] = TEST_DB
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    cfg = alembic_config()
    command.downgrade(cfg, "base")  # start clean even if an earlier run crashed midway
    command.upgrade(cfg, "head")
    yield


def _migration(filename: str):
    """Reference data comes from the migrations; TRUNCATE removes it, so put it back the same
    way production gets it (read from the migration file, not a second copy in the tests)."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "migrations", "versions", filename)
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PLANT_SEED = _migration("0002_farm_seed.py").PLANTS
_M0004 = _migration("0004_stopwords_and_mango_varieties.py")
VARIETY_SEED = _M0004.VARIETIES
STOPWORD_SEED = _M0004.STOPWORDS + _migration("0006_more_stopwords.py").WORDS


@pytest.fixture(autouse=True)
def clean_tables(migrated_db):
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        # LangGraph's checkpoints (migration 0005) are not in models.py; a paused plan from one
        # test must not be resumable in the next.
        conn.execute(text("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes"))
        for name, aliases, count in PLANT_SEED:
            conn.execute(text("INSERT INTO plants (plant, variety, aliases, count, count_date) "
                              "VALUES (:p, '', :a, :c, DATE '2026-10-19')"),
                         {"p": name, "a": aliases.split(","), "c": count})
        for plant, variety, aliases in VARIETY_SEED:
            conn.execute(text("INSERT INTO plants (plant, variety, aliases, count, count_date) "
                              "VALUES (:p, :v, :a, 0, NULL)"),
                         {"p": plant, "v": variety, "a": aliases})
        for w in STOPWORD_SEED:
            conn.execute(text("INSERT INTO search_stopwords (word) VALUES (:w) "
                              "ON CONFLICT DO NOTHING"), {"w": w})
    from app.services.rag_index import reset_stopwords_cache
    reset_stopwords_cache()
    yield


class FakeTelegram:
    """Stands in for TelegramClient and records what the bot would have sent."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[dict] = []
        self.typing: list[int] = []
        self.photos: list[dict] = []
        self.documents: list[dict] = []
        self.cleared: list[tuple] = []
        self.files: dict[str, bytes] = {}      # file_id -> bytes the "user" uploaded
        self.fail = False
        self.calls: list[tuple] = []

    def send_message(self, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return not self.fail

    def send_typing(self, chat_id):
        self.typing.append(chat_id)

    def send_photo(self, chat_id, file_id, caption=""):
        self.photos.append({"chat_id": chat_id, "file_id": file_id, "caption": caption})
        return not self.fail

    def send_document(self, chat_id, filename, data, caption=""):
        self.documents.append({"chat_id": chat_id, "filename": filename, "data": data,
                               "caption": caption})
        return not self.fail

    def clear_buttons(self, chat_id, message_id):
        self.cleared.append((chat_id, message_id))
        return not self.fail

    def get_file_bytes(self, file_id):
        return self.files.get(file_id)

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return not self.fail

    def answer_callback(self, callback_id, text=None):
        self.answers.append({"callback_id": callback_id, "text": text})
        return not self.fail

    def call(self, method, payload=None):
        self.calls.append((method, payload))
        return None if self.fail else True

    def texts_to(self, chat_id) -> list[str]:
        return [m["text"] for m in self.sent if m["chat_id"] == chat_id]


@pytest.fixture
def tg():
    return FakeTelegram()


# ── Scripted stand-in for Gemini ──────────────────────────────────────────────────────────

from app.integrations.gemini import LlmError, ToolCall, Turn  # noqa: E402


def call(name, **args) -> Turn:
    """The model asks for one tool."""
    return Turn(calls=[ToolCall(name, args)], text="")


def calls(*pairs) -> Turn:
    """The model asks for several tools in one turn: calls(("add_expense", {...}), ...)."""
    return Turn(calls=[ToolCall(n, a) for n, a in pairs], text="")


def say(text) -> Turn:
    """The model answers in words, no tool."""
    return Turn(calls=[], text=text)


class FakeChat:
    def __init__(self, llm, system, tools):
        self.llm, self.system, self.tools = llm, system, tools
        self.user_messages: list[str] = []
        self.tool_results: list[list] = []
        self.media: list[tuple] = []
        self.escalated = 0   # times the agent switched to the stronger fallback model
        self.history: list[tuple[str, str]] = []   # short-term memory the agent sent first

    def add_history(self, turns):
        self.history += list(turns)

    def escalate(self):
        self.escalated += 1

    def _next(self):
        own = self.llm.queue_for(self.system)
        if own is not None:
            if not own:
                raise AssertionError("an agent asked the model more often than the test scripted")
            item = own.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if not self.llm.turns:
            raise AssertionError("the bot asked the model more often than the test scripted")
        item = self.llm.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send_user(self, text):
        self.user_messages.append(text)
        return self._next()

    def send_tool_results(self, results):
        self.tool_results.append(results)
        return self._next()

    def send_media(self, text, data, mime_type):
        self.media.append((text, data, mime_type))
        return self._next()


# Fake embeddings (same as the old bot's test_farm.py): words in one synonym group share a
# dimension, so "daag" and "fodla" are close the way a real model puts them close. Like the real
# model, they do NOT know romanised "udhai" = termite: the stored English translation is what
# connects the two, and the tests check exactly that.
GROUPS = [
    {"fungus", "fodla", "daag", "dag", "spots", "spot", "fungal", "phug", "fug", "ફૂગ", "ડાઘ", "pila", "yellow"},
    {"mango", "aamba", "aam", "keri", "alphonso", "kesar", "hafus", "amba"},
    {"neem"}, {"copper", "oxychloride"}, {"humic"},
    {"paani", "water", "watering", "irrigation", "piyat"},
    {"udhai"}, {"termite", "termites", "दीमक", "ઉધઈ"}, {"chiku", "sapota"}, {"guava", "jamfal"},
    {"lemon", "limbu"}, {"tractor", "service"}, {"spray", "sprayed", "chhantkav"},
    {"navā", "nava", "new", "young", "chhod", "plants"},
]


def fake_embed(text: str) -> list[float]:
    import re as _re
    from app.core.text import strip_marks, words
    from app.integrations.gemini import EMBED_DIM, normalise
    groups = [{strip_marks(x).lower() for x in g} for g in GROUPS]
    v = [0.0] * EMBED_DIM
    for t in words(text) + _re.findall(r"\d+(?::\d+)+", text):
        g = next((i for i, grp in enumerate(groups) if t in grp), None)
        idx = g if g is not None else 40 + (sum(map(ord, t)) * 7) % (EMBED_DIM - 40)
        v[idx] += 3.0 if g is not None else 1.0
    v[EMBED_DIM - 1] += 0.5      # shared baseline: unrelated texts are not exactly 0, as in reality
    return normalise(v)


# Word-by-word "translation" for the farm tests. Real translation is checked by the evals.
TRANSLATE = {"udhai": "termite", "fodla": "blisters", "daag": "spots", "limdo": "neem",
             "limda": "neem", "paani": "water", "aamba": "mango", "pan": "leaves", "par": "on",
             "ma": "in", "aaje": "today", "aavi": "came", "karyu": "done", "dava": "pesticide",
             "naakhi": "applied", "jevu": "like"}


def fake_translate(text: str) -> str:
    import re as _re
    return " ".join(TRANSLATE.get(w.lower(), w) for w in _re.findall(r"[\w:.]+", text))


class FakeLLM:
    """Queue the model's turns with .script(...); every chat pops from the same queue in order.

    complete() (one-shot answers: RAG, general advice) pops from the same queue and returns the
    turn's text. Translations are not scripted per test (fake_translate), so a test about a farm
    note never has to know that the note also gets translated in the background."""

    embed_model = "fake-embedding"

    def __init__(self):
        self.turns: list = []
        self.chats: list[FakeChat] = []
        self.completions: list[tuple[str, str]] = []
        self.translated: list[str] = []
        self.embedded: list[str] = []
        self.embed_error: Exception | None = None
        self.translate_error: Exception | None = None
        # Agents that run at the same time (LangGraph Send) cannot share one ordered queue: which
        # thread asks first is not fixed. Each gets its own queue, picked by a phrase of its
        # system prompt.
        self.keyed: dict[str, list] = {}

    def script_for(self, key, *turns):
        """Turns for the chat (or complete() call) whose system prompt contains `key`."""
        self.keyed.setdefault(key, []).extend(turns)
        return self

    def queue_for(self, system):
        return next((q for k, q in self.keyed.items() if k in (system or "")), None)

    def complete(self, system, prompt, *, json_schema=None, temperature=0.1, budget_s=30):
        from app.services.rag_index import TRANSLATE_RULES
        self.completions.append((system, prompt))
        if system == TRANSLATE_RULES:
            if self.translate_error:
                raise self.translate_error
            self.translated.append(prompt)
            return fake_translate(prompt)
        own = self.queue_for(system)
        if own is not None:
            if not own:
                raise AssertionError("a writer asked the model more often than the test scripted")
            item = own.pop(0)
            if isinstance(item, Exception):
                raise item
            return item.text
        if not self.turns:
            raise AssertionError("the bot asked the model more often than the test scripted")
        item = self.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item.text

    def embed(self, texts, task="RETRIEVAL_DOCUMENT"):
        if self.embed_error:
            raise self.embed_error
        self.embedded.extend(texts)
        return [fake_embed(t) for t in texts]

    def script(self, *turns):
        self.turns.extend(turns)
        return self

    def start(self, system, tools, budget_s):
        chat = FakeChat(self, system, tools)
        self.chats.append(chat)
        return chat

    @property
    def last(self) -> FakeChat:
        return self.chats[-1]


class FakeMailer:
    def __init__(self):
        self.sent = []
        self.ok = True

    @property
    def configured(self):
        return True

    def send(self, email):
        self.sent.append(email)
        return self.ok


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def llm():
    fake = FakeLLM()
    yield fake
    assert not fake.turns, f"scripted model turns were never used: {fake.turns}"


__all__ = ["call", "calls", "say", "LlmError"]

# The `bot` fixture (a chat with Sagar, Biren, Lalo and a guest) lives with its helpers.
from tests.bot_helpers import bot  # noqa: E402,F401
