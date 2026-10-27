"""Photos and voice notes -> something the rest of the bot already understands.

Photo of a khata / receipt / payment screenshot -> a list of expenses, shown as a preview and
saved only after "yes". There is no typed text to ground an amount against, so the human reading
the preview IS the check — every item shows the payer, category and date it will be saved with,
and anything the model was unsure about is flagged.

Voice note -> a word-for-word transcript, which then goes through the SAME agent and gates as
typed text, in confirm mode: a transcript can be misheard ("pandar" / "pachhis"), so every
write is previewed first.
"""
import json
import logging
import re
from datetime import date
from decimal import InvalidOperation

from app.ai import proposals
from app.core.formatting import fmt_date, parse_date, to_rupees
from app.db.models import Member
from app.integrations.gemini import LLM
from app.services import members
from app.services.expenses import CATEGORIES

log = logging.getLogger(__name__)

MEDIA_BUDGET_S = 40

_CAPTION_PAYER = re.compile(r"\bby\s+([^\W\d_]+)", re.IGNORECASE)


def photo_prompt(people: list[Member], today: date, caption: str) -> str:
    names = ", ".join(m.name for m in people)
    caption_note = (
        f'\nThe user also typed this caption with the photo: "{caption}"\n'
        "The caption is typed by the user and is MORE RELIABLE than anything you read from the "
        "image: if it says who paid, the category or any other detail, use the caption.") if caption else ""
    return (
        "This is a photo of a handwritten expense ledger, a receipt or a payment screenshot.\n"
        "Extract EVERY distinct expense in it — there may be one or several.\n"
        "For each: amount, date, description, paid_by, merchant, category.\n"
        "date: only if it is written on the image (a receipt date, a payment time like "
        f"'05 Sept 2026'). Convert to DD-MM-YYYY with {fmt_date(today)} as today. Empty if there is "
        "no date — never guess.\n"
        f"paid_by: exactly one of {names}, closest spelling; if unclear, empty and add \"paid_by\" "
        "to missing.\n"
        "merchant: who was paid, else empty.\n"
        f"category: exactly one of {', '.join(CATEGORIES)}.\n"
        "If any field is unclear or guessed, add its name to that item's missing list — the user "
        "sees this before anything is saved." + caption_note +
        "\nReturn ONLY a JSON array, no markdown:\n"
        '[{"amount":0,"date":"","description":"","paid_by":"","merchant":"","category":"","missing":[]}]\n'
        "If there are no expenses, return []."
    )


TRANSCRIBE_PROMPT = (
    "Transcribe this voice note to text, word for word.\n"
    "Keep the speaker's language and script: Gujarati stays Gujarati, Hindi stays Hindi, English "
    "stays English, romanized Gujarati/Hindi stays romanized the same way.\n"
    "Write numbers as digits (five hundred -> 500, pandar -> 15, sade paanch -> 5:30).\n"
    "Do not translate, summarise, explain or add anything that was not said.\n"
    "Return ONLY the transcript text, no quotes, no preamble."
)


def parse_json_array(raw: str) -> list[dict]:
    """Models wrap JSON in code fences or a sentence even when told not to. Take the array out;
    a bare object becomes a one-item list. Anything unreadable is an empty list, never a guess."""
    text = re.sub(r"```[a-z]*", "", raw or "", flags=re.IGNORECASE).strip()
    start, end = text.find("["), text.rfind("]")
    try:
        data = json.loads(text[start:end + 1] if start != -1 and end > start else text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def caption_payer(people: list[Member], caption: str) -> Member | None:
    """'paid by lalo' typed in the caption beats anything read from the image."""
    m = _CAPTION_PAYER.search(caption or "")
    return members.match_name(people, m.group(1)) if m else None


def items_from_model(raw: str, *, sender: Member, people: list[Member], today: date,
                     caption: str) -> tuple[list[dict], list[str]]:
    """-> (items ready for a proposal, problems that stop a save).

    An item with no amount is dropped (a ledger heading, a total line). A payer nobody can name
    is a problem, not a guess: the user is asked to say who paid."""
    override = caption_payer(people, caption)
    sender_shares = members.match_name(people, sender.name)
    items, problems = [], []
    for row in parse_json_array(raw):
        try:
            amount = to_rupees(row.get("amount") or 0)
        except (InvalidOperation, ValueError):
            continue
        if amount <= 0:
            continue
        missing = [m for m in (row.get("missing") or []) if isinstance(m, str)]
        missing = ["paid by" if m in ("paid_by", "paidBy") else m for m in missing]

        payer = override or members.match_name(people, row.get("paid_by") or row.get("paidBy"))
        if override:
            missing = [m for m in missing if m != "paid by"]
        if payer is None and sender_shares is not None:
            payer = sender_shares
            if "paid by" not in missing:
                missing.append("paid by")      # defaulted, so it is flagged for the user to check
        if payer is None:
            problems.append(f"₹{amount} — who paid? Send it again with a caption like \"by "
                            f"{people[0].name.split()[0] if people else 'name'}\".")
            continue

        description = str(row.get("description") or "").strip()
        if not re.search(r"[^\W\d_]", description):
            description = "(no description)"
            missing.append("description")

        raw_date = str(row.get("date") or "").strip()
        on = parse_date(raw_date) if raw_date else None
        if on is None:
            on = today
            missing.append("date (today used)")

        category = str(row.get("category") or "")
        if category.strip().lower() not in {c.lower() for c in CATEGORIES}:
            missing.append("category")

        items.append(proposals.expense_item(
            amount=amount, description=description, paid_by=payer, category=category,
            merchant=str(row.get("merchant") or ""), on=on, missing=list(dict.fromkeys(missing))))
    return items, problems


def read_photo(llm: LLM, data: bytes, *, sender: Member, people: list[Member], today: date,
               caption: str) -> tuple[list[dict], list[str]]:
    chat = llm.start("You read expense records accurately.", [], MEDIA_BUDGET_S)
    turn = chat.send_media(photo_prompt(people, today, caption), data, "image/jpeg")
    return items_from_model(turn.text, sender=sender, people=people, today=today, caption=caption)


def transcribe(llm: LLM, data: bytes, mime_type: str) -> str:
    chat = llm.start("You transcribe voice notes exactly.", [], MEDIA_BUDGET_S)
    raw = chat.send_media(TRANSCRIBE_PROMPT, data, mime_type).text
    # Models like to wrap transcripts in quotes or a code fence even when told not to.
    text = re.sub(r"```[a-z]*|```", "", raw or "", flags=re.IGNORECASE).strip()
    return text.strip("\"'` ").strip()

