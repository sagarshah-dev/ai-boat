"""Helpers shared by the farm tests: seed journal rows, read them back, script the model."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import JournalEntry, Knowledge, Member, Plant, RagChunk
from app.db.session import session_scope
from app.services import rag_index
from tests.bot_helpers import SAGAR
from tests.conftest import call, say

TODAY = datetime.now(ZoneInfo("Asia/Kolkata")).date()


def ago(days: int):
    return TODAY - timedelta(days=days)


def seed(*rows: dict, by: int = SAGAR) -> list[int]:
    """Journal rows straight into the table. Keys are JournalEntry fields; `date` = entry_date,
    `by` = the Telegram id of who added it."""
    with session_scope() as s:
        ids = {m.telegram_user_id: m.id for m in s.scalars(select(Member))}
        out = []
        for r in rows:
            r = dict(r)
            e = JournalEntry(entry_date=r.pop("date", TODAY), plant=r.pop("plant", "Mango"),
                             activity=r.pop("activity", "spray"), added_by=ids[r.pop("by", by)], **r)
            s.add(e)
            s.flush()
            out.append(e.id)
    return out


def seed_knowledge(title: str, text: str, when=None, by: int = SAGAR, source: str = "voice") -> int:
    with session_scope() as s:
        mid = s.scalar(select(Member.id).where(Member.telegram_user_id == by))
        k = Knowledge(entry_date=when or TODAY, title=title, text=text, source=source, added_by=mid)
        s.add(k)
        s.flush()
        return k.id


def add_plant(plant: str, variety: str, aliases: str, count: int = 0):
    with session_scope() as s:
        s.add(Plant(plant=plant, variety=variety, aliases=aliases.split(","), count=count))


def journal() -> list[JournalEntry]:
    with session_scope() as s:
        return list(s.scalars(select(JournalEntry).order_by(JournalEntry.id)))


def entry(jid: int) -> JournalEntry:
    with session_scope() as s:
        return s.get(JournalEntry, jid)


def knowledge() -> list[Knowledge]:
    with session_scope() as s:
        return list(s.scalars(select(Knowledge).order_by(Knowledge.id)))


def chunk_ids() -> set[str]:
    with session_scope() as s:
        return set(s.scalars(select(RagChunk.id)))


def sync(llm) -> dict:
    with session_scope() as s:
        return rag_index.sync(s, llm)


def log(**args):
    return call("log_farm_activity", **args)


def rag_answer(answer: str, cited: list[str], missing: str = ""):
    """What the answering model returns for a farm search (a JSON form)."""
    return say(json.dumps({"answer": answer, "cited_ids": cited, "missing": missing}))
