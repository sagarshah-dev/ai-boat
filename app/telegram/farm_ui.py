"""Small, pure routing decisions for the farm journal: is this photo a farm note or a bill? is
this voice note the farmer's advice? plus the /menu. Ported from the old bot's farmUi.js.

Pure functions, no database and no network, so every rule is unit-tested on its own.
"""
import re

from app.core.replies import Buttons

MENU_TEXT = (
    "🏡 AI BOaT — tap or just type:\n\n"
    "🌱 Farm note — \"aaje badha aamba ma neem spray, 50 ml per 15 litre, savare\"\n"
    "🔍 Ask the farm — \"pehla daag padya tyare shu karyu?\"\n"
    "📷 Photo + one line — saved with the note\n"
    "📚 Advice / plan — voice note (1 min+) or tap 📚 and type it\n"
    "💰 Expense — \"500 diesel by biren\"\n"
    "📅 Meeting — \"kale saanje 5 vagye meeting\"\n\n"
    "Send \"help\" for every command."
)


def menu_buttons() -> Buttons:
    return [[("🌱 Farm note", "menu:note"), ("🔍 Ask farm", "menu:ask")],
            [("🌳 Plant count", "menu:counts"), ("📒 This week", "menu:week")],
            [("💰 Balance", "menu:balance"), ("📅 Meetings", "menu:meetings")],
            [("📚 Save advice / plan", "menu:advice")]]


# Words that make a photo caption or a multi-line message a FARM note rather than an expense.
# This only routes the message; the plant itself is decided later from the plants table.
# "pan" (leaf) is left out on purpose: in Gujarati it also means "but".
_FARM_WORDS = (
    "aamba", "amba", "aambo", "keri", "mango", "nariyel", "nariyal", "coconut", "jamfal", "guava",
    "peru", "chiku", "chikoo", "sapota", "limbu", "nimbu", "lemon", "chhod", "chod", "zad",
    "plant", "plants", "tree", "spray", "dava", "dawa", "khatar", "fertilizer", "neem", "copper",
    "humic", "fungus", "fodla", "daag", "udhai", "termite", "jivat", "pest", "leaves", "leaf",
    "fal", "fruit", "phool", "flower", "ropya", "ropyu", "sukai", "kapani", "paani", "piyat", "drip")
_FARM_RE = re.compile(r"\b(" + "|".join(_FARM_WORDS) + r")(ma|na|ni|nu|ne|no)?\b", re.IGNORECASE)
_FARM_GU_RE = re.compile("આંબ|કેરી|નાળિયેર|જામફળ|ચીકુ|લીંબુ|છોડ|ઝાડ|દવા|ખાતર|સ્પ્રે|પાન|ફૂગ|ઉધઈ|જીવાત")
_MONEY_RE = re.compile(r"₹|\brs\.?(?=\s|\d|$)|\brupiya|\brupees?\b|\binr\b|/-", re.IGNORECASE)
_KNOWLEDGE_WORDS = re.compile(r"gyan|salah|salaah|advice|knowledge|farmer|khedut|સલાહ|જ્ઞાન|ખેડૂત",
                              re.IGNORECASE)


def looks_like_farm(text: str) -> bool:
    t = str(text or "")
    return bool(_FARM_RE.search(t) or _FARM_GU_RE.search(t))


def photo_is_farm_note(caption: str, farm_mode: bool) -> bool:
    """A receipt is an expense; a photo of a leaf is a farm note. Money in the caption ("₹450",
    "450 rs") keeps it on the expense path even if it names a plant."""
    if farm_mode:
        return True
    c = str(caption or "")
    if not c.strip() or _MONEY_RE.search(c):
        return False
    return looks_like_farm(c)


def is_knowledge_voice(duration_s: int, caption: str, advice_mode: bool, min_seconds: int) -> bool:
    """The farmer's advice is a long voice note; a farm note or an expense is a short one."""
    return advice_mode or int(duration_s or 0) >= min_seconds or bool(_KNOWLEDGE_WORDS.search(caption or ""))


# A line that opens with its own date ("15-Oct:", "18/10", "aaje", "kale") is its own note.
_DATED_LINE = re.compile(
    r"^\s*(\d{1,2}\s*[-/. ]\s*(\d{1,2}|[a-z]{3,9})\b|aaje|aje|kale|gaikale|parmdivse|today|yesterday)",
    re.IGNORECASE)


_UNIT_AFTER = re.compile(
    r"\d+(?:\.\d+)?\s*(ml|l|ltr|litre|liter|lit|g|gm|gram|grams|kg|min|mins|minute|minutes|kalak|"
    r"hour|hours|divas|day|days|%|x|plant|plants|chhod|chod|zad|tree|trees|feet|ft|inch)\b",
    re.IGNORECASE)


def _bare_number(line: str) -> bool:
    """A number with no unit after the date ("khatar 500") is money: an expense, not a note."""
    rest = _DATED_LINE.sub("", line, count=1)
    return bool(re.search(r"\d", _UNIT_AFTER.sub("", rest)))


def several_notes(text: str) -> list[str]:
    """The lines of a message that holds several dated farm notes, else []. Each note needs its
    own preview and its own "Save": one "yes" can only ever confirm one thing. A note typed over
    two lines has one date at most, so it is not split."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    dated = [ln for ln in lines if _DATED_LINE.match(ln) and looks_like_farm(ln)]
    if len(dated) < 2 or _MONEY_RE.search(text) or any(_bare_number(ln) for ln in lines):
        return []
    return lines


def several_notes_reply(lines: list[str]) -> str:
    # Live bug 29-10: four notes in one message, the same error three times, nothing saved and
    # no word about why. Now nothing is saved and the user is told exactly what to do.
    listed = "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines, 1))
    return (f"📒 {len(lines)} farm notes in one message — nothing was saved.\n"
            "Send them one per message, so each gets its own preview and ✅ Save:\n\n" + listed)


def multi_line_to_ai(text: str) -> bool:
    """Several lines usually mean several expenses. A farm note typed over two lines must reach
    the AI as ONE message instead."""
    return "\n" in text and looks_like_farm(text) and not _MONEY_RE.search(text)


def preview(text: str, limit: int = 1500) -> str:
    """Telegram refuses messages over 4096 characters: long transcripts are previewed."""
    return text if len(text) <= limit else f"{text[:limit]}…\n({len(text) - limit} more characters)"
