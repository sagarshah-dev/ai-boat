"""Meetings: storage, time rules, and the checks that keep a model from booking a wrong slot.

Amounts can be grounded strictly (the digits must be in the message). Dates cannot — "kale 5
vagye" is the normal way to say it and has no date in it. So the checks are different in kind:
  1. the message must carry SOME date/time signal, or the model is inventing the meeting;
  2. a weekday the user named must match the date the model worked out (wrong week, last year);
  3. future, and within a year;
  4. "5 vagye" with no savare/saanje/am/pm is asked, never guessed (old bot guessed PM);
  5. no overlap with an existing meeting.
Ported from the old bot (main.py meeting gates, sheets.py Meetings tab).
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Meeting, Member

DEFAULT_DURATION = 60
MIN_DURATION = 5
MAX_DURATION = 8 * 60
MAX_AHEAD_DAYS = 365
SCHEDULED, CANCELLED = "scheduled", "cancelled"

WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_NAMES = {
    0: ["monday", "mon", "somvar", "somwar", "som", "સોમવાર", "सोमवार"],
    1: ["tuesday", "tue", "tues", "mangalvar", "mangalwar", "mangal", "મંગળવાર", "मंगलवार"],
    2: ["wednesday", "wed", "budhvar", "budhwar", "budh", "બુધવાર", "बुधवार"],
    3: ["thursday", "thu", "thurs", "guruvar", "guruwar", "guru", "ગુરુવાર", "गुरुवार"],
    4: ["friday", "fri", "shukravar", "shukrawar", "sukravar", "shukra", "શુક્રવાર", "शुक्रवार"],
    5: ["saturday", "sat", "shanivar", "shaniwar", "shani", "શનિવાર", "शनिवार"],
    6: ["sunday", "sun", "ravivar", "raviwar", "ravi", "itvar", "itwar", "રવિવાર", "रविवार"],
}


def strip_marks(s: str) -> str:
    """'સોમવારે' -> base letters only. Python's \\w does not include Gujarati vowel signs, so
    without this the word splits into pieces and no lookup ever matches."""
    return "".join(ch for ch in unicodedata.normalize("NFD", str(s))
                   if unicodedata.category(ch) not in ("Mn", "Mc", "Me"))


def words(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[^\W\d_]+", strip_marks(text)) if w]


_WEEKDAYS = {strip_marks(n).lower(): d for d, names in _WEEKDAY_NAMES.items() for n in names}
# Case endings ("ravivare", "somvarna"): a word of 4+ letters that STARTS with a weekday counts.
# The 4-letter floor keeps "sun"/"mon" from firing on "sunny" or "money".
_WEEKDAY_PREFIXES = sorted((n for n in _WEEKDAYS if len(n) >= 4), key=len, reverse=True)

_DAYPART = {
    "am": "am", "pm": "pm", "morning": "am", "savare": "am", "saware": "am", "sawar": "am",
    "subah": "am", "afternoon": "pm", "bapore": "pm", "evening": "pm", "saanje": "pm",
    "sanje": "pm", "sham": "pm", "shaam": "pm", "night": "pm", "ratre": "pm", "raat": "pm",
    "noon": "pm", "સવારે": "am", "બપોરે": "pm", "સાંજે": "pm", "રાત્રે": "pm",
}
_DAYPART = {strip_marks(k).lower(): v for k, v in _DAYPART.items()}
_TIME_WORDS = {"vagye", "vage", "vaagye", "baje", "baaje", "oclock", strip_marks("વાગે").lower()}
_DATE_WORDS = {strip_marks(w).lower() for w in (
    "today", "tonight", "tomorrow", "tmrw", "overmorrow", "next", "coming", "this", "weekend",
    "week", "month", "aaje", "aaj", "aje", "kale", "kaal", "kal", "aavti", "aavta", "aavtikale",
    "parmo", "parmadi", "parmadive", "આજે", "કાલે", "આવતી", "આવતા", "પરમ")} \
    | set(_DAYPART) | _TIME_WORDS | set(_WEEKDAYS)
_DATE_PREFIXES = sorted((w for w in _DATE_WORDS if len(w) >= 4), key=len, reverse=True)


def datetime_signal(text: str) -> bool:
    """Does the message say anything at all about WHEN? A digit counts, and so does a time word.
    'meeting rakho' has neither: the model would be inventing the appointment."""
    if re.search(r"\d", str(text or "")):
        return True
    return any(w in _DATE_WORDS or any(w.startswith(p) for p in _DATE_PREFIXES) for w in words(text))


def weekday_named(text: str) -> int | None:
    """The weekday the user named, or None (none, or two different ones = undecided)."""
    found = set()
    for w in words(text):
        d = _WEEKDAYS.get(w)
        if d is None:
            d = next((_WEEKDAYS[p] for p in _WEEKDAY_PREFIXES if w.startswith(p)), None)
        if d is not None:
            found.add(d)
    return found.pop() if len(found) == 1 else None


def daypart(text: str) -> str | None:
    """'am' / 'pm' if the message says which half of the day, else None."""
    low = str(text or "").lower()
    if re.search(r"\d\s*(a\.?m\.?)\b", low):
        return "am"
    if re.search(r"\d\s*(p\.?m\.?)\b", low):
        return "pm"
    for w in words(text):
        if w in _DAYPART:
            return _DAYPART[w]
        hit = next((_DAYPART[p] for p in _DAYPART if len(p) >= 4 and w.startswith(p)), None)
        if hit:
            return hit
    return None


def ambiguous_hour(text: str) -> int | None:
    """'5 vagye' / 'at 7' with no am/pm/savare/saanje -> 5 / 7. The old bot let the model guess
    PM. A 24-hour time ('17:00') or 12 o'clock noon/midnight words are not ambiguous."""
    if daypart(text):
        return None
    t = strip_marks(str(text or "")).lower()
    if re.search(r"\b(1[3-9]|2[0-3]|0\d):\d{2}\b", t):
        return None
    m = (re.search(r"\b(\d{1,2})(?::\d{2})?\s*(?:" + "|".join(sorted(_TIME_WORDS)) + r"|o'?\s?clock)", t)
         or re.search(r"\bat\s+(\d{1,2})(?::\d{2})?\b(?!\s*(?:am|pm))", t))
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    return None


def parse_time(s: object) -> str | None:
    """'17:00', '17.00', '5:30 pm', '9 am' -> 'HH:MM'. A bare '5' is rejected: the model must
    commit to 5am or 5pm, and the preview shows which."""
    t = str(s or "").strip().lower().replace(".", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$", t) or re.match(r"^(\d{1,2})()\s*(am|pm)$", t)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = (hour % 12) + (12 if ampm == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def pretty_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def pretty_when(start: datetime, duration_min: int) -> str:
    """'Sat 17-10-2026, 5:00 PM – 6:00 PM' — weekday spelled out so a wrong week is obvious."""
    end = start + timedelta(minutes=duration_min)
    return (f"{WEEKDAY_SHORT[start.weekday()]} {start.strftime('%d-%m-%Y')}, "
            f"{pretty_time(start)} – {pretty_time(end)}")


@dataclass(frozen=True)
class Slot:
    start: datetime          # timezone-aware, owners' zone
    duration_min: int


class SlotProblem(Exception):
    """Why a proposed time was refused. `kind` tells the agent who answers: the user (code asks)
    or the model (it fixes its own arguments)."""

    def __init__(self, kind: str, message: str, **data):
        super().__init__(message)
        self.kind, self.message, self.data = kind, message, data


def check_slot(session: Session, *, date_str: str, time_str: str, duration: object, message: str,
               today: date, now: datetime, tz: str, ignore_id: int | None = None) -> Slot:
    from app.core.formatting import parse_date   # local: formatting imports nothing from here
    hhmm = parse_time(time_str)
    if hhmm is None:
        raise SlotProblem("model", f"time '{time_str}' is not 24-hour HH:MM. Call again with e.g. 17:00.")
    d = parse_date(date_str)
    if d is None:
        raise SlotProblem("model", f"date '{date_str}' is not DD-MM-YYYY. Call again with a valid date.")
    if d < today:
        raise SlotProblem("model", f"{date_str} is in the past (today is {today:%d-%m-%Y}). Ask the "
                                   "user which future date they mean.")
    if (d - today).days > MAX_AHEAD_DAYS:
        raise SlotProblem("model", f"{date_str} is more than a year away — almost certainly a wrong "
                                   "year. Ask the user to confirm the date.")
    named = weekday_named(message)
    if named is not None and d.weekday() != named:
        raise SlotProblem("model", f"The user said {WEEKDAY_SHORT[named]}, but {date_str} is a "
                                   f"{WEEKDAY_SHORT[d.weekday()]}. Work out the right date and call again.")
    hour = ambiguous_hour(message)
    if hour is not None:
        raise SlotProblem("ampm", f"{hour} — morning or evening?", hour=hour)
    try:
        minutes = int(duration or DEFAULT_DURATION)
    except (TypeError, ValueError):
        minutes = DEFAULT_DURATION
    if not MIN_DURATION <= minutes <= MAX_DURATION:
        minutes = DEFAULT_DURATION
    start = datetime.strptime(f"{date_str} {hhmm}", "%d-%m-%Y %H:%M").replace(tzinfo=ZoneInfo(tz))
    if start <= now:
        raise SlotProblem("model", f"{pretty_time(start)} on {date_str} has already passed. Ask the "
                                   "user for a later time.")
    hits = conflicts(session, start, minutes, ignore_id)
    if hits:
        listed = "; ".join(f"#{m.id} {m.title} ({pretty_when(m.starts_at.astimezone(start.tzinfo), m.duration_min)})"
                           for m in hits[:3])
        raise SlotProblem("conflict", f"That time clashes with {listed}.")
    return Slot(start, minutes)


# ── Storage ─────────────────────────────────────────────────────────────────────────────────

def conflicts(session: Session, start: datetime, minutes: int, ignore_id: int | None = None) -> list[Meeting]:
    end = start + timedelta(minutes=minutes)
    rows = session.scalars(select(Meeting).where(
        Meeting.status == SCHEDULED, Meeting.starts_at < end).order_by(Meeting.starts_at))
    return [m for m in rows if m.id != ignore_id
            and m.starts_at + timedelta(minutes=m.duration_min) > start]


def get(session: Session, meeting_id: object) -> Meeting | None:
    try:
        return session.get(Meeting, int(meeting_id), with_for_update=True)
    except (TypeError, ValueError):
        return None


def listing(session: Session, scope: str, now: datetime, on: date | None = None,
            tz: str = "Asia/Kolkata") -> list[Meeting]:
    q = select(Meeting).where(Meeting.status == SCHEDULED)
    midnight = datetime.combine(now.date(), datetime.min.time(), ZoneInfo(tz))
    if scope == "upcoming":
        q = q.where(Meeting.starts_at >= midnight).order_by(Meeting.starts_at)
    elif scope == "past":
        q = q.where(Meeting.starts_at < midnight).order_by(Meeting.starts_at.desc())
    else:
        q = q.order_by(Meeting.starts_at.desc())
    rows = list(session.scalars(q))
    if on is not None:
        rows = [m for m in rows if m.starts_at.astimezone(ZoneInfo(tz)).date() == on]
    return rows


def add(session: Session, *, title: str, slot: Slot, agenda: str, by: Member,
        event_id: str | None) -> Meeting:
    m = Meeting(title=title, starts_at=slot.start, duration_min=slot.duration_min, agenda=agenda,
                calendar_event_id=event_id, created_by=by.id, status=SCHEDULED)
    session.add(m)
    session.flush()
    return m
