"""Short-lived "the bot is waiting for your answer" state: a missing amount, a settle PIN.

Stored in the database, not in memory: the old bot kept this in a Map, so a restart in the
middle of "how much was it?" silently forgot the question.
One row per (chat, member, kind): in a group, Biren's "200" can never complete Sagar's tea.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import PendingAction

AMOUNT = "amount"
TIME = "time"              # a meeting waiting for its time (or am/pm, or a free slot)
SETTLE_PIN = "settle_pin"
CONFIRM = "confirm"        # a previewed change waiting for yes / no
PLANT = "plant"            # a farm note waiting for which plant it is about
GENERAL = "general"        # a farm search found nothing; the 💡 General advice button is live
FARM_PHOTO = "farm_photo"  # a farm photo without a caption, waiting for its one line
FARM_MODE = "farm_mode"    # 🌱 tapped in the menu: the next photo is a farm note
ADVICE_MODE = "advice_mode"  # 📚 tapped in the menu: the next text / voice note is advice
SALAH = "salah"            # a 🌱 Farm Salah graph paused for this person (24 h): thread id + what it waits for
SALAH_NOTE = "salah_note"  # the 📝 "save as a farm note" button under a Farm Salah plan answer (24 h)

TTL = timedelta(minutes=2)


@dataclass(frozen=True)
class Pending:
    kind: str
    payload: dict
    attempts: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get(session: Session, chat_id: int, member_id: int, kind: str) -> Pending | None:
    """The live entry, or None. An expired entry is deleted on the way, so it can't linger."""
    row = session.scalar(select(PendingAction).where(
        PendingAction.chat_id == chat_id, PendingAction.member_id == member_id,
        PendingAction.kind == kind))
    if row is None:
        return None
    if row.expires_at <= _now():
        session.delete(row)
        session.flush()
        return None
    return Pending(kind=row.kind, payload=dict(row.payload), attempts=row.attempts)


def pop(session: Session, chat_id: int, member_id: int,
        kind: str) -> tuple[Pending | None, bool]:
    """Removes and returns the entry, and whether it had already expired. For one-shot answers
    like the settle PIN, where "too late" deserves its own reply: an expired PIN must not fall
    through and be read as an ordinary message (it would be echoed back as an amount)."""
    row = session.scalar(select(PendingAction).where(
        PendingAction.chat_id == chat_id, PendingAction.member_id == member_id,
        PendingAction.kind == kind).with_for_update())
    if row is None:
        return None, False
    expired = row.expires_at <= _now()
    found = Pending(kind=row.kind, payload=dict(row.payload), attempts=row.attempts)
    session.delete(row)
    session.flush()
    return found, expired


def put(session: Session, chat_id: int, member_id: int, kind: str, payload: dict,
        attempts: int = 0, ttl: timedelta = TTL) -> None:
    """Create or replace — each new question restarts its window."""
    values = {"chat_id": chat_id, "member_id": member_id, "kind": kind, "payload": payload,
              "attempts": attempts, "expires_at": _now() + ttl}
    stmt = pg_insert(PendingAction).values(**values).on_conflict_do_update(
        constraint="uq_pending_actions_chat_member_kind",
        set_={k: values[k] for k in ("payload", "attempts", "expires_at")})
    session.execute(stmt)


def drop(session: Session, chat_id: int, member_id: int, kind: str) -> None:
    session.execute(delete(PendingAction).where(
        PendingAction.chat_id == chat_id, PendingAction.member_id == member_id,
        PendingAction.kind == kind))
