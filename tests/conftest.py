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

# Settings are read from the environment; set them before the app is imported.
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "TELEGRAM_TOKEN": "test-token-not-real",
    "TELEGRAM_WEBHOOK_SECRET": "test-secret-0123456789",
    "ADMIN_TELEGRAM_ID": str(ADMIN_ID),
    "GEMINI_API_KEY": "test-key-not-real",
    "SETTLE_PIN": SETTLE_PIN,
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


@pytest.fixture(autouse=True)
def clean_tables(migrated_db):
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
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

    def _next(self):
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


class FakeLLM:
    """Queue the model's turns with .script(...); every chat pops from the same queue in order."""

    def __init__(self):
        self.turns: list = []
        self.chats: list[FakeChat] = []

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
