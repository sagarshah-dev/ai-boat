"""Word helpers shared by every check that compares the model's output with the user's words.

Python's \\w does not include Gujarati vowel signs, so "સોમવારે" would split into pieces and no
lookup would match. Marks are stripped first; the same tokeniser is used everywhere so a word
found by one check is the same word for every other check.
"""
import re
import unicodedata

_WORD_RE = re.compile(r"[^\W\d_]+")


def strip_marks(s: object) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", str(s or ""))
                   if unicodedata.category(ch) not in ("Mn", "Mc", "Me"))


def words(text: object) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(strip_marks(text)) if w]


def norm(text: object) -> str:
    return " ".join(words(text))
