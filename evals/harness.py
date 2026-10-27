"""Shared setup for the evals: the real app and the REAL Gemini model, on a throwaway Postgres.

Unit tests (tests/) script the model, so they prove the checks work IF the model does X. The
evals prove what the model actually does. Nothing touches production: Telegram is faked (replies
are collected), email is off, and the database is EVAL_DATABASE_URL, which is wiped per case.

Messages go through POST /telegram/webhook exactly as Telegram sends them, so an eval runs every
layer the bot runs: dedup, access check, commands, agent, checks, proposals, templates.
"""
import itertools
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

PEOPLE = {"Sagar": (1001, "admin"), "Biren": (2001, "owner"), "Lalo": (3001, "owner")}
CHAT = -100500            # one group chat: Biren's "200" must never complete Sagar's "tea"
SECRET = "eval-secret-0123456789"
_ids = itertools.count(1)


def configure() -> None:
    """Environment for the app, set BEFORE it is imported. Refuses to run on a real database."""
    key, db = os.getenv("GEMINI_API_KEY", ""), os.getenv("EVAL_DATABASE_URL", "")
    if not key:
        sys.exit("GEMINI_API_KEY is not set — evals need the real model.")
    if not db:
        sys.exit("EVAL_DATABASE_URL is not set — point it at a throwaway Postgres with pgvector.")
    if db == os.getenv("DATABASE_URL"):
        sys.exit("EVAL_DATABASE_URL equals DATABASE_URL — refusing to wipe the real database.")
    if not os.getenv("RAG_MIN_SCORE"):
        os.environ.pop("RAG_MIN_SCORE", None)   # an empty workflow input means "the default"
    os.environ.update({
        "DATABASE_URL": db, "TELEGRAM_TOKEN": "eval-token-not-real",
        "TELEGRAM_WEBHOOK_SECRET": SECRET, "ADMIN_TELEGRAM_ID": str(PEOPLE["Sagar"][0]),
        "SETTLE_PIN": "eval-pin", "SMTP_HOST": "", "JOBS_SECRET": "",
    })
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = db
    command.upgrade(cfg, "head")


class Recorder:
    """Wraps the real model and records every tool it asked for, so grading can say "the model
    picked search_expenses" without parsing reply wording (which changes every run)."""

    def __init__(self, llm):
        self.llm, self.calls = llm, []
        self.embed_model = llm.embed_model

    def start(self, system, tools, budget_s):
        return _RecordingChat(self, self.llm.start(system, tools, budget_s))

    def complete(self, *a, **kw):
        return self.llm.complete(*a, **kw)

    def embed(self, *a, **kw):
        return self.llm.embed(*a, **kw)


class _RecordingChat:
    def __init__(self, rec, chat):
        self.rec, self.chat = rec, chat

    def _seen(self, turn):
        self.rec.calls += [{"name": c.name, "args": c.args} for c in turn.calls]
        return turn

    def send_user(self, text):
        return self._seen(self.chat.send_user(text))

    def send_tool_results(self, results):
        return self._seen(self.chat.send_tool_results(results))

    def send_media(self, text, data, mime_type):
        return self._seen(self.chat.send_media(text, data, mime_type))


class Telegram:
    """Collects what the bot would have sent."""

    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, buttons=None):
        self.sent.append(text)
        return True

    def send_typing(self, chat_id):
        pass

    def send_photo(self, *a, **kw):
        return True

    def send_document(self, *a, **kw):
        return True

    def clear_buttons(self, *a):
        return True

    def edit_message_text(self, *a):
        return True

    def answer_callback(self, *a):
        return True

    def get_file_bytes(self, file_id):
        return None


def reset(keep_plants: bool = True) -> dict:
    """Empty every table (the plants list from migration 0002 stays) and add the three owners.
    Returns {name: member id}."""
    from sqlalchemy import text
    from app.db.models import Base, Member
    from app.db.session import get_engine, session_scope
    names = ", ".join(t.name for t in Base.metadata.sorted_tables
                      if not (keep_plants and t.name == "plants"))
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    with session_scope() as s:
        for name, (tid, role) in PEOPLE.items():
            s.add(Member(telegram_user_id=tid, name=name, role=role, status="active"))
        s.flush()
        return {m.name: m.id for m in s.query(Member)}


def clear_waiting() -> None:
    from sqlalchemy import delete
    from app.db.models import PendingAction
    from app.db.session import session_scope
    with session_scope() as s:
        s.execute(delete(PendingAction))


def app_client(llm, tg):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.telegram.webhook import get_llm, get_mailer, get_telegram
    app.dependency_overrides[get_llm] = lambda: llm
    app.dependency_overrides[get_telegram] = lambda: tg
    app.dependency_overrides[get_mailer] = lambda: None
    return TestClient(app)


def pin_today(day: date) -> None:
    """Cases are written for a fixed "today" ("kale" = a known date)."""
    from app.telegram import commands
    commands.today = lambda tz: day


def send(client, tg, who: str, text: str) -> str:
    from app.config import get_settings
    before = len(tg.sent)
    r = client.post("/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": get_settings().telegram_webhook_secret},
                    json={"update_id": next(_ids), "message": {
                        "message_id": next(_ids), "from": {"id": PEOPLE[who][0], "first_name": who},
                        "chat": {"id": CHAT}, "text": text}})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    return "\n".join(tg.sent[before:])


def write_report(name: str, lines: list[str]) -> None:
    report = "\n".join(lines) + "\n"
    (HERE / name).write_text(report, encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report)
