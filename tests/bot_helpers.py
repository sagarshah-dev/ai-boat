"""Talk to the bot the way Telegram does, and read back what it replied."""
import itertools
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Expense, Member
from app.db.session import session_scope
from app.main import app
from app.telegram.webhook import get_llm, get_telegram
from tests.conftest import ADMIN_ID

SECRET = "test-secret-0123456789"
SAGAR, BIREN, LALO, RAMESH = ADMIN_ID, 2001, 3001, 4001
GROUP = -100777
_ids = itertools.count(10_000)


class Bot:
    def __init__(self, client, tg, llm):
        self.client, self.tg, self.llm = client, tg, llm

    def send(self, user_id: int, text: str, chat_id: int | None = None) -> str:
        """Sends one message and returns everything the bot replied to that chat, joined."""
        chat = chat_id or user_id
        before = len(self.tg.sent)
        update = {"update_id": next(_ids), "message": {
            "message_id": next(_ids), "from": {"id": user_id, "first_name": "x"},
            "chat": {"id": chat}, "text": text}}
        r = self.client.post("/telegram/webhook", json=update,
                             headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        assert r.status_code == 200
        return "\n".join(m["text"] for m in self.tg.sent[before:] if m["chat_id"] == chat)


@pytest.fixture
def bot(tg, llm):
    """Sagar (admin), Biren and Lalo (owners) share expenses; Ramesh is a guest."""
    with session_scope() as s:
        for tid, name, role in ((SAGAR, "Sagar", "admin"), (BIREN, "Biren", "owner"),
                                (LALO, "Lalo", "owner"), (RAMESH, "Ramesh", "guest")):
            s.add(Member(telegram_user_id=tid, name=name, role=role, status="active"))
    app.dependency_overrides[get_telegram] = lambda: tg
    app.dependency_overrides[get_llm] = lambda: llm
    with TestClient(app) as c:
        yield Bot(c, tg, llm)
    app.dependency_overrides.clear()


def member_id(telegram_id: int) -> int:
    with session_scope() as s:
        return s.scalar(select(Member.id).where(Member.telegram_user_id == telegram_id))


def add_rows(*rows: tuple[str, int, str], when: date = date(2026, 10, 1), by: int = SAGAR):
    """Directly insert expenses: (payer telegram id given as name lookup) — ('Sagar', 300, 'tea')."""
    with session_scope() as s:
        ids = {m.name: m.id for m in s.scalars(select(Member))}
        for payer, amount, desc, *rest in rows:
            category = rest[0] if rest else "Misc"
            s.add(Expense(expense_date=when, amount=Decimal(amount), description=desc,
                          paid_by=ids[payer], entered_by=member_id(by), category=category))


def expenses_in_db(include_deleted: bool = False) -> list[Expense]:
    with session_scope() as s:
        q = select(Expense).order_by(Expense.id)
        if not include_deleted:
            q = q.where(Expense.deleted_at.is_(None))
        return list(s.scalars(q))
