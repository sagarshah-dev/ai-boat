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
from app.telegram.webhook import get_llm, get_mailer, get_telegram
from tests.conftest import ADMIN_ID

SECRET = "test-secret-0123456789"
SAGAR, BIREN, LALO, RAMESH = ADMIN_ID, 2001, 3001, 4001
GROUP = -100777
_ids = itertools.count(10_000)


class Bot:
    def __init__(self, client, tg, llm, mailer=None):
        self.client, self.tg, self.llm, self.mailer = client, tg, llm, mailer

    def post(self, update: dict, chat: int) -> str:
        before = len(self.tg.sent)
        r = self.client.post("/telegram/webhook", json=update,
                             headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        assert r.status_code == 200
        return "\n".join(m["text"] for m in self.tg.sent[before:] if m["chat_id"] == chat)

    def send(self, user_id: int, text: str, chat_id: int | None = None) -> str:
        """Sends one message and returns everything the bot replied to that chat, joined."""
        chat = chat_id or user_id
        return self.post({"update_id": next(_ids), "message": {
            "message_id": next(_ids), "from": {"id": user_id, "first_name": "x"},
            "chat": {"id": chat}, "text": text}}, chat)

    def photo(self, user_id: int, data: bytes = b"jpeg-bytes", caption: str = "",
              chat_id: int | None = None) -> str:
        chat = chat_id or user_id
        fid = f"photo{next(_ids)}"
        self.tg.files[fid] = data
        message = {"message_id": next(_ids), "from": {"id": user_id}, "chat": {"id": chat},
                   "photo": [{"file_id": "small"}, {"file_id": fid}]}
        if caption:
            message["caption"] = caption
        return self.post({"update_id": next(_ids), "message": message}, chat)

    def voice(self, user_id: int, data: bytes = b"ogg-bytes", caption: str = "",
              duration: int = 5, chat_id: int | None = None) -> str:
        chat = chat_id or user_id
        fid = f"voice{next(_ids)}"
        self.tg.files[fid] = data
        message = {"message_id": next(_ids), "from": {"id": user_id}, "chat": {"id": chat},
                   "voice": {"file_id": fid, "mime_type": "audio/ogg", "duration": duration}}
        if caption:
            message["caption"] = caption
        return self.post({"update_id": next(_ids), "message": message}, chat)

    def tap(self, user_id: int, data: str, chat_id: int | None = None, message_id: int = 777) -> str:
        chat = chat_id or user_id
        return self.post({"update_id": next(_ids), "callback_query": {
            "id": f"cb{next(_ids)}", "from": {"id": user_id}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}}, chat)


@pytest.fixture
def bot(tg, llm, mailer):
    """Sagar (admin), Biren and Lalo (owners) share expenses; Ramesh is a guest."""
    with session_scope() as s:
        for tid, name, role in ((SAGAR, "Sagar", "admin"), (BIREN, "Biren", "owner"),
                                (LALO, "Lalo", "owner"), (RAMESH, "Ramesh", "guest")):
            s.add(Member(telegram_user_id=tid, name=name, role=role, status="active"))
    app.dependency_overrides[get_telegram] = lambda: tg
    app.dependency_overrides[get_llm] = lambda: llm
    app.dependency_overrides[get_mailer] = lambda: mailer
    with TestClient(app) as c:
        yield Bot(c, tg, llm, mailer)
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
