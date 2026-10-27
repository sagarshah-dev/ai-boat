"""Dates and money in the forms the owners read: dd-MM-yyyy and ₹1,12,412."""
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

DATE_FORMAT = "%d-%m-%Y"


def today(tz: str) -> date:
    """The owners' calendar day. Cloud Run's clock is UTC, which is still "yesterday" in India
    until 05:30 — an expense typed at 1 a.m. would get the wrong date."""
    return datetime.now(ZoneInfo(tz)).date()


def parse_date(text: object) -> date | None:
    """'05-10-2026' -> date(2026, 10, 5). None when blank or malformed."""
    try:
        return datetime.strptime(str(text).strip(), DATE_FORMAT).date()
    except (TypeError, ValueError):
        return None


def fmt_date(d: date) -> str:
    return d.strftime(DATE_FORMAT)


def to_rupees(value: object) -> Decimal:
    """Exact money value with paise. Floats from the model go through str() so 0.1 stays 0.1."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_rupees(value: Decimal | int | float) -> int:
    """Half-up, the way people round money. Python's round() is banker's rounding (0.5 -> 0)."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def inr(value: Decimal | int | float) -> str:
    """Indian digit grouping, whole rupees: 112412 -> '1,12,412'."""
    n = round_rupees(value)
    s, sign = str(abs(n)), "-" if n < 0 else ""
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups) + "," + tail
