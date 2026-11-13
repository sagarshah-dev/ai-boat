"""Short-term memory: the last few exchanges of one person in one chat, for a short time.

The model is stateless: every message starts a new chat, so "E divase biju kai kharch thayo?"
could not know which day (live baseline 13-11-2026: 0 of 3 follow-ups understood). This is a
sliding window — the standard form of conversation memory — kept in Postgres because Cloud Run
forgets everything between requests:

- per chat AND member: in the group, Biren's last message must not explain Sagar's "e";
- the last 3 exchanges, from the last 30 minutes: enough for a follow-up, too little for old
  context to steer a new question;
- the text the user saw (the bot's final reply), not tool calls: numbers in a reply are context
  only — every number the bot writes still comes from a tool, and every write still needs its
  own check or ✅ (agent.MEMORY_RULES).

Old rows are deleted when a new one is written, so no scheduled job is needed.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import ConversationTurn

EXCHANGES = 3                   # user message + bot reply = one exchange
WINDOW = timedelta(minutes=30)
MAX_CHARS = 1200                # one long reply (a report list) must not fill the prompt


def _clip(text: str) -> str:
    text = str(text or "").strip()
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + "…"


def record(session: Session, chat_id: int, member_id: int, user_text: str, bot_text: str,
           now: datetime | None = None) -> None:
    """Remember one exchange, and forget what is now outside the window."""
    now = now or datetime.now(timezone.utc)
    user_text, bot_text = _clip(user_text), _clip(bot_text)
    if not user_text:
        return
    session.add(ConversationTurn(chat_id=chat_id, member_id=member_id, role="user",
                                 text=user_text, created_at=now))
    if bot_text:
        session.add(ConversationTurn(chat_id=chat_id, member_id=member_id, role="bot",
                                     text=bot_text, created_at=now + timedelta(microseconds=1)))
    session.flush()
    mine = (ConversationTurn.chat_id == chat_id) & (ConversationTurn.member_id == member_id)
    keep = select(ConversationTurn.id).where(mine).order_by(ConversationTurn.created_at.desc(),
                                                             ConversationTurn.id.desc()).limit(EXCHANGES * 2)
    session.execute(delete(ConversationTurn).where(
        mine, (ConversationTurn.created_at < now - WINDOW) | ConversationTurn.id.not_in(keep)))


def recent(session: Session, chat_id: int, member_id: int,
           now: datetime | None = None) -> list[tuple[str, str]]:
    """(role, text), oldest first: what this person and the bot said in the last 30 minutes."""
    now = now or datetime.now(timezone.utc)
    rows = session.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.chat_id == chat_id, ConversationTurn.member_id == member_id,
               ConversationTurn.created_at >= now - WINDOW)
        .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc())
        .limit(EXCHANGES * 2)).all()
    turns = [(r.role, r.text) for r in reversed(rows)]
    while turns and turns[0][0] == "bot":     # a chat history starts with the user
        turns.pop(0)
    return turns
