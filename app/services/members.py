"""Who may use the bot: access requests, admin approval, lookups.

Knows nothing about Telegram messages or buttons — it takes plain values and returns an outcome,
and the Telegram layer decides what to say. The same functions will serve a web admin page or a
WhatsApp channel unchanged.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Member


@dataclass(frozen=True)
class TgUser:
    id: int
    name: str
    username: str | None = None


class StartOutcome(Enum):
    ADMIN_NOT_CONFIGURED = "admin_not_configured"
    ADMIN_READY = "admin_ready"
    ALREADY_ACTIVE = "already_active"
    REQUEST_CREATED = "request_created"
    REQUEST_PENDING = "request_pending"


class Decision(Enum):
    OWNER = "owner"
    GUEST = "guest"
    REJECT = "reject"


class DecisionOutcome(Enum):
    NOT_ADMIN = "not_admin"
    NOT_FOUND = "not_found"
    ALREADY_DECIDED = "already_decided"
    APPROVED = "approved"
    REJECTED = "rejected"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find(session: Session, telegram_user_id: int) -> Member | None:
    return session.scalar(select(Member).where(Member.telegram_user_id == telegram_user_id))


def get_active(session: Session, telegram_user_id: int) -> Member | None:
    member = find(session, telegram_user_id)
    return member if member and member.status == "active" else None


SPLIT_ROLES = ("admin", "owner")


def split_members(session: Session) -> list[Member]:
    """The people expenses are shared between: active admin + owners, in joining order.
    Guests use the bot but never pay a share and never appear in balances."""
    return list(session.scalars(
        select(Member).where(Member.status == "active", Member.role.in_(SPLIT_ROLES))
        .order_by(Member.id)))


def match_name(people: list[Member], raw: object) -> Member | None:
    """'sagar', ' SAGAR ', 'Sagar Shah' -> the Sagar member, case-insensitive.

    Names come from Telegram ("Sagar Shah"), but people type first names ("tea by sagar"). The
    full name always matches; a first name matches only when exactly one person has it, so two
    Rameshes can never be mixed up — the bot asks instead."""
    wanted = " ".join(str(raw or "").lower().split())
    if not wanted:
        return None
    exact = [m for m in people if " ".join(m.name.lower().split()) == wanted]
    if exact:
        return exact[0]
    by_first = [m for m in people if m.name.lower().split()[:1] == [wanted]]
    return by_first[0] if len(by_first) == 1 else None


def name_words(people: list[Member]) -> set[str]:
    """Every word of every member's name, lower-case — for 'is this word a person?' checks."""
    return {w for m in people for w in m.name.lower().split()}


def request_access(session: Session, user: TgUser,
                   admin_telegram_id: int) -> tuple[StartOutcome, Member | None]:
    """Handles /start. The configured admin is let in directly; everyone else waits for approval."""
    member = find(session, user.id)

    if admin_telegram_id and user.id == admin_telegram_id:
        if member is None:
            member = Member(telegram_user_id=user.id, name=user.name, telegram_username=user.username)
            session.add(member)
        member.role, member.status = "admin", "active"
        member.joined_at = member.joined_at or _now()
        session.flush()
        return StartOutcome.ADMIN_READY, member

    # Without an admin nobody could approve the request, so don't create one that can never move.
    if not admin_telegram_id:
        return StartOutcome.ADMIN_NOT_CONFIGURED, None

    if member is not None and member.status == "active":
        return StartOutcome.ALREADY_ACTIVE, member
    if member is not None and member.status == "pending":
        # Repeated /start must not flood the admin with the same request.
        return StartOutcome.REQUEST_PENDING, member

    if member is None:
        member = Member(telegram_user_id=user.id, name=user.name, telegram_username=user.username,
                        role="guest", status="pending")
        session.add(member)
    else:  # removed earlier; may ask again, the admin can say no again
        member.status = "pending"
        member.name, member.telegram_username = user.name, user.username
    session.flush()
    return StartOutcome.REQUEST_CREATED, member


def decide(session: Session, admin_telegram_id: int, member_id: int,
           decision: Decision) -> tuple[DecisionOutcome, Member | None]:
    """Admin's answer to a pending request. Checks the caller in the database, not only the
    button: anyone in a chat can press a button that was shown there."""
    caller = get_active(session, admin_telegram_id)
    if caller is None or caller.role != "admin":
        return DecisionOutcome.NOT_ADMIN, None

    member = session.get(Member, member_id, with_for_update=True)
    if member is None:
        return DecisionOutcome.NOT_FOUND, None
    if member.status != "pending":
        # Button pressed twice, or two taps racing — the first one wins.
        return DecisionOutcome.ALREADY_DECIDED, member

    if decision is Decision.REJECT:
        member.status = "removed"
        session.flush()
        return DecisionOutcome.REJECTED, member

    member.role = decision.value
    member.status = "active"
    member.joined_at = _now()
    session.flush()
    return DecisionOutcome.APPROVED, member
