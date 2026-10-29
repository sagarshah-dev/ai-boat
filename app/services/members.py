"""Who may use the bot: access requests, admin approval, lookups.

Knows nothing about Telegram messages or buttons — it takes plain values and returns an outcome,
and the Telegram layer decides what to say. The same functions will serve a web admin page or a
WhatsApp channel unchanged.
"""
import re
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


def _aliases(m: Member) -> list[str]:
    return [a.lower() for a in (m.aliases or []) if a]


def match_name(people: list[Member], raw: object) -> Member | None:
    """'sagar', ' SAGAR ', 'Sagar Shah', 'jignesh' -> that member, case-insensitive.

    Names come from Telegram ("Sagar Shah"), but people type first names ("tea by sagar") or
    nicknames ("by bako"). The full name and an alias always match; a first name matches only
    when exactly one person has it, so two Rameshes can never be mixed up — the bot asks."""
    wanted = " ".join(str(raw or "").lower().split())
    if not wanted:
        return None
    exact = [m for m in people if " ".join(m.name.lower().split()) == wanted
             or wanted in _aliases(m)]
    if len(exact) == 1:
        return exact[0]
    by_first = [m for m in people if m.name.lower().split()[:1] == [wanted]]
    return by_first[0] if len(by_first) == 1 else None


def name_words(people: list[Member]) -> set[str]:
    """Every word of every member's name and every alias, lower-case — for 'is this word a
    person?' checks."""
    return {w for m in people for w in m.name.lower().split()} | {a for m in people
                                                                    for a in _aliases(m)}


# Words, Gujarati included: [^\W\d_] splits Gujarati at its vowel signs (લાલો -> લ, લ).
_TOKEN = re.compile(r"[^\s\d.,;:!?()\[\]{}\"'/₹+*=-]+")
# A Gujarati name with a case ending written on it: લાલોએ, બિરેનને.
_GU_SUFFIX = ("એ", "ને", "ના", "ની", "નું", "નો")


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(str(text or ""))]


def mentioned(people: list[Member], text: str) -> list[Member]:
    """The members whose name or alias is written in `text`, in the user's own words.

    Used to decide the payer from what the user typed, not from the model's argument: the model
    can only pick from its enum, so when the right person was missing it picked the sender."""
    toks = tokens(text)
    found = []
    for m in people:
        words = set(_aliases(m)) | set(m.name.lower().split()[:1])
        for t in toks:
            if t in words or (not t.isascii() and any(
                    t == w + suf for w in words if not w.isascii() for suf in _GU_SUFFIX)):
                found.append(m)
                break
    return found


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

    if decision is Decision.OWNER:
        seeded = _unclaimed_owner(session, member.name)
        if seeded is not None:
            # Lalo was seeded as a payer before he ever used the bot (migration 0003). His
            # request is the same person: link it, don't add a second "Lalo" to the split.
            tid, username = member.telegram_user_id, member.telegram_username
            session.delete(member)
            session.flush()
            seeded.telegram_user_id, seeded.telegram_username = tid, username
            session.flush()
            return DecisionOutcome.APPROVED, seeded

    member.role = decision.value
    member.status = "active"
    member.joined_at = _now()
    session.flush()
    return DecisionOutcome.APPROVED, member


def _unclaimed_owner(session: Session, telegram_name: str) -> Member | None:
    """A seeded owner (no Telegram ID yet) whose name or alias is this Telegram name's first
    word. Exactly one, or None: a guess here would hand someone else's share to the newcomer."""
    first = (telegram_name or "").lower().split()[:1]
    if not first:
        return None
    rows = session.scalars(select(Member).where(
        Member.telegram_user_id.is_(None), Member.role == "owner", Member.status == "active"))
    hits = [m for m in rows if first[0] in _aliases(m) or m.name.lower().split()[:1] == first]
    return hits[0] if len(hits) == 1 else None
