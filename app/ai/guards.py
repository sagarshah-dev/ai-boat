"""Deterministic checks on what the model proposes. Code decides, not the model.

The model will always produce SOME amount and SOME description — it once logged "tea by sagar"
as ₹10. These checks compare its output with the user's own words before anything is saved.
Same rules as the old bot (main.py amount_in_text / has_item_word), plus lakh/crore.
"""
import re
from decimal import Decimal

# Dates like 14-10-2026 or 14/10 are removed first, so "tea on 10-10-2026" can't "ground" ₹10.
# Dots are NOT date separators: "1.5k" must stay a number.
_DATE_LIKE = re.compile(r"\b\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?\b")

# "5 kg", "20 litre", "3 bags" are quantities, never the amount.
_QUANTITY = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:kgs?|kilos?|gms?|grams?|g|ltrs?|litres?|liters?|l|ml|bags?|nos|pcs?|"
    r"pieces?|km|ft|feet|bottles?|packets?|boxes|box|dozen|trips?|days?|hours?|hrs?)\b",
    re.IGNORECASE)

# Old bot read "2 lakh" as the number 2, so a model answer of ₹2 counted as grounded.
_MULTIPLIER = {"k": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
               "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000}
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:(k|lakhs?|lacs?|crores?|cr)(?![a-z]))?",
                     re.IGNORECASE)

ARITHMETIC = re.compile(r"\d\s*[x×*+]\s*\d", re.IGNORECASE)

_FILLER = {"rs", "inr", "rupee", "rupees", "paid", "pay", "by", "for", "to", "the", "a", "an",
           "of", "and", "ne", "na", "ni", "nu", "no", "ma", "thi", "aapya", "apya", "aapyo",
           "diya", "kiya", "total", "amount", "expense", "kharch", "kharcho", "k", "only",
           "lakh", "lakhs", "lac", "lacs", "crore", "crores", "cr"}

# Closed set -> regex, never the model. A bare "yes" has exactly one meaning or none.
YES = re.compile(r"^\s*(y|yes|ya|yeah|yep|ok|okay|okey|done|confirm|sure|ha|haa|હા|हाँ|ha ji|haan)"
                 r"\s*[.!]?\s*$", re.IGNORECASE)
NO = re.compile(r"^\s*(n|no|nope|cancel|stop|nahi|naa|na|ના|नहीं|rehva do|rehne do)\s*[.!]?\s*$",
                re.IGNORECASE)


def numbers_in(text: str) -> list[Decimal]:
    """Every amount written in digits, with k / lakh / crore applied. Dates and quantities skipped."""
    cleaned = _QUANTITY.sub(" ", _DATE_LIKE.sub(" ", str(text or "")))
    out = []
    for num, unit in _NUMBER.findall(cleaned):
        try:
            value = Decimal(num.replace(",", ""))
        except ArithmeticError:
            continue
        if unit:
            value *= _MULTIPLIER[unit.lower()]
        out.append(value)
    return out


def amount_in_text(amount: Decimal, text: str) -> bool:
    """True if `amount` is written in `text` as digits (200, 1,200, ₹1200, 2k, 1.5k, 2 lakh).
    Words ("be hajar") or arithmetic ("2 x 50") return False on purpose: for money, one extra
    question is cheaper than a wrong entry."""
    return any(abs(v - amount) < Decimal("0.005") for v in numbers_in(text))


def has_item_word(text: str, people_words: set[str]) -> bool:
    """True if the text names something besides amounts, filler words and people.
    "200" or "200 by sagar" has no item; "200 tea" has one."""
    for w in re.findall(r"[^\W\d_]+", str(text or "")):
        lw = w.lower()
        if lw in _FILLER or lw in people_words:
            continue
        if len(w) >= 2 or not w.isascii():   # Gujarati "ચા" is a real word
            return True
    return False


def amount_only_pattern(people_words: set[str]) -> re.Pattern:
    """A reply that is just an amount: 100, ₹100, rs 100, 1,200, 2k, 1.5 lakh — optionally with
    'paid by <name>'. Used to complete a waiting "how much was it?"."""
    names = "|".join(sorted((re.escape(w) for w in people_words), key=len, reverse=True)) or "(?!)"
    return re.compile(
        r"^\s*(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|lakhs?|lacs?|crores?|cr|rs\.?|rupees?|₹|/-)?"
        r"(?:\s+(?:paid\s+)?by\s+(?:" + names + r"))?\s*$",
        re.IGNORECASE)


# "Just a time": 5, 5pm, 17:00, 5:30, 5 vagye, saanje 5, સાંજે 5. Kept apart from the amount
# pattern: "5" means ₹5 when an expense waits and 5 o'clock when a meeting waits — the waiting
# question's kind decides, so the same text is never ambiguous.
_TIME_WORDS = (r"vagye|vage|vaagye|baje|baaje|o\s*clock|o'clock|saanje|sanje|saware|savare|sawar|"
               r"bapore|ratre|morning|evening|afternoon|night|વાગે|સાંજે|સવારે|બપોરે|રાત્રે")
TIME_ONLY = re.compile(r"^\s*(?:" + _TIME_WORDS + r")?\s*\d{1,2}(?:[:.]\d{2})?\s*"
                       r"(?:am|pm|a\.m\.|p\.m\.|" + _TIME_WORDS + r")?\s*$", re.IGNORECASE)
DAYPART_ONLY = re.compile(r"^\s*(am|pm|morning|evening|afternoon|night|savare|saware|saanje|sanje|"
                          r"bapore|ratre|સવારે|સાંજે|બપોરે|રાત્રે)\s*$", re.IGNORECASE)


# ── Lists of expenses in one message ────────────────────────────────────────────────────────
# Live bug (28/29-10-2026): "150 tea, 250 vadapav, 500 sevsad, coffee 300 all by lalo" saved ONE
# entry and nobody was told about the other three; "..., coffee all by sagar" saved
# "sevsad, coffee" ₹500. The model's calls are checked one by one, so the checks below look at
# the whole message: which parts became an entry, and which did not.

# A comma between two digits is digit grouping ("1,12,412"), not a list.
_SEGMENT = re.compile(r"[;\n]+|(?<!\d),|,(?!\d)")
_LIST_FILLER = _FILLER | {"all", "badha", "badhu", "both", "banne", "ane", "aur", "by", "paid",
                          "e", "ae", "che", "hata", "hatu", "ka", "ki", "ke"}
# "by card" / "by upi" is HOW it was paid, not WHO.
_PAY_METHODS = {"card", "cash", "upi", "gpay", "phonepe", "paytm", "online", "cheque", "check",
                "bank", "me", "myself", "self", "mi"}
_BY_WORD = re.compile(r"\bby\s+([^\W\d_]+)", re.IGNORECASE)


def segments(text: str) -> list[str]:
    """"150 tea, 250 vadapav; coffee" -> ["150 tea", "250 vadapav", "coffee"]. Only commas,
    semicolons and new lines split: "tea and nasta 200" is one bill, one entry."""
    return [s.strip() for s in _SEGMENT.split(str(text or "")) if s.strip()]


def item_words(text: str, people_words: set[str]) -> list[str]:
    """The words that name things ("coffee"), without amounts, names and filler."""
    out = []
    for w in re.findall(r"[^\W\d_]+", str(text or "")):
        lw = w.lower()
        if lw in _LIST_FILLER or lw in people_words or lw in _PAY_METHODS:
            continue
        if len(w) >= 2 or not w.isascii():
            out.append(lw)
    return out


def unknown_payer_word(text: str, people_words: set[str]) -> str | None:
    """"500 diesel by bhavesh" -> "bhavesh": someone was named who does not share expenses.
    The model can only answer with a member, so it would quietly pick one — the sender."""
    for word in _BY_WORD.findall(str(text or "")):
        lw = word.lower()
        if lw not in people_words and lw not in _PAY_METHODS and lw not in ("all", "the"):
            return word
    return None


_JOINER = re.compile(r"\s+(?:and|ane|aur|tatha|&)\s+", re.IGNORECASE)


def amount_segment(amount: Decimal, text: str) -> str | None:
    """The part of the message where this amount is written: its comma-part, and inside that
    the clause between "and"/"ane" when the part holds more than one amount ("200 tea by sagar
    and 500 diesel by biren"). Only for finding THIS amount's payer — "tea and nasta 200" is
    still one entry, because a part with one amount is never split."""
    part = next((s for s in segments(text) if amount_in_text(amount, s)), None)
    if part is None or len(numbers_in(part)) < 2:
        return part
    return next((c for c in _JOINER.split(part) if amount_in_text(amount, c)), part)


def amountless_items(text: str, people_words: set[str]) -> list[str]:
    """In a list like "150 tea, 250 vadapav, coffee", the parts that name an item but no amount
    ("coffee"). A single phrase ("tea and nasta 200") never counts: it is one entry."""
    parts = segments(text)
    if len(parts) < 2 or not any(numbers_in(p) for p in parts):
        return []
    out = []
    for p in parts:
        if not numbers_in(p):
            out += item_words(p, people_words)
    return out
