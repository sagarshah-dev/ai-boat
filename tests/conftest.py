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

# Settings are read from the environment; set them before the app is imported.
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "TELEGRAM_TOKEN": "test-token-not-real",
    "TELEGRAM_WEBHOOK_SECRET": "test-secret-0123456789",
    "ADMIN_TELEGRAM_ID": str(ADMIN_ID),
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
        self.fail = False

    def send_message(self, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return not self.fail

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
