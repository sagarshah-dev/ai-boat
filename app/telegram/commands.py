"""What a member's text message means.

Exact command words (balance, recent, undo, settle, a name or a category) are matched by regex:
free, instant, and the same every time. Everything else goes to the AI. No growing synonym
lists here — "hisab aapo" reaches the model, which calls the same balance code.
"""
import hmac
import re
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.ai import agent
from app.config import Settings
from app.core.formatting import today
from app.db.models import Member
from app.integrations.gemini import LLM
from app.services import balance, expenses, members, pending, settlements
from app.services.expenses import CATEGORIES

HELP_TEXT = (
    "🤖 AI BOaT\n\n"
    "Add an expense: just type it — \"150 tea by sagar\", \"diesel 2k\".\n"
    "Several lines = several expenses.\n\n"
    "Commands:\n"
    "balance — who is pending, who gets back\n"
    "recent — last 5 entries\n"
    "undo — remove your own last entry\n"
    "<name> or <category> — e.g. \"sagar\", \"fuel expenses\"\n"
    "search ... — e.g. \"search diesel after 1 oct\"\n"
    "settle — settle up (needs the PIN)\n\n"
    "Or ask in your own words: \"hisab aapo\", \"aa mahine ketlo kharch thayo?\""
)
NOT_YET = "📷 Photos and voice notes come in a later update. Please type it for now."

Typing = Callable[[int], None]


def handle_text(session: Session, sender: Member, chat_id: int, text: str, settings: Settings,
                llm: LLM, typing: Typing | None = None) -> list[str]:
    raw = text.strip()
    if not raw:
        return []

    # A settle PIN being typed is checked before anything else, so the PIN can never be read
    # as an amount or reach the model.
    waiting_pin, expired = pending.pop(session, chat_id, sender.id, pending.SETTLE_PIN)
    if waiting_pin is not None:
        if expired:
            return ["⌛ PIN entry timed out. Send \"settle\" again to retry."]
        if _pin_ok(raw, settings):
            return [_settle(session, sender, settings)]
        return ["❌ Incorrect PIN. Settlement cancelled — send \"settle\" to try again."]

    # "/balance@AIBoatHQ_bot" -> "balance": slash commands are aliases of the plain words.
    cmd = re.sub(r"^/(\w+)@\w+", r"\1", raw).lstrip("/") if raw.startswith("/") else raw
    lower = cmd.lower().strip()

    if lower in ("help", "menu"):
        return [HELP_TEXT]
    if lower == "settle":
        if not settings.settle_pin:
            return ["⚙️ Settling is switched off: no SETTLE_PIN is configured."]
        pending.put(session, chat_id, sender.id, pending.SETTLE_PIN, {})
        return ["🔐 Enter the settle PIN to confirm:"]
    m = re.fullmatch(r"settle\s+(\S+)", lower)
    if m:
        if _pin_ok(m.group(1), settings):
            return [_settle(session, sender, settings)]
        return ["❌ Send exactly: settle <PIN>"]
    if lower in ("balance", "bal"):
        return [balance.compute(session).text]
    if lower == "recent":
        return [agent.recent_text(expenses.recent(session, 5))]
    if lower == "undo":
        undone = expenses.undo_last(session, sender)
        return [agent.undo_text(undone) if undone
                else f"No recent unsettled entry by {sender.name} to undo."]

    filtered = _filter(session, lower)
    if filtered is not None:
        return [filtered]

    ctx = agent.Context(session=session, sender=sender, chat_id=chat_id,
                        today=today(settings.timezone), llm=llm)
    if typing:
        typing(chat_id)   # the model can take a few seconds; show "typing…"
    if "\n" in cmd:
        return [_batch(ctx, cmd)]
    return [agent.handle(ctx, cmd).reply]


def _pin_ok(given: str, settings: Settings) -> bool:
    return bool(settings.settle_pin) and hmac.compare_digest(given.strip().encode(),
                                                             settings.settle_pin.encode())


def _settle(session: Session, sender: Member, settings: Settings) -> str:
    return settlements.settle(session, sender, today(settings.timezone)).text


def _filter(session: Session, lower: str) -> str | None:
    """'sagar', 'fuel', 'fuel expenses' -> every matching entry with a total. None if the text is
    not exactly a name or a category, so ordinary sentences still go to the AI."""
    m = re.fullmatch(r"(.+?)(?:\s+expenses?)?", lower)
    word = m.group(1).strip() if m else ""
    people = members.split_members(session)
    payer = members.match_name(people, word)
    category = next((c for c in CATEGORIES if c.lower() == word), None)
    if payer is None and category is None:
        return None
    rows = expenses.search(session, paid_by=payer, category=category)
    return agent.search_text(rows, expenses.total(rows))


def _batch(ctx: agent.Context, text: str) -> str:
    """One expense per line, in order (sequential so IDs follow the lines). A bad line never
    stops the others; the reply says which lines were saved and which were not."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    saved, failed = [], []
    for line in lines:
        outcome = agent.handle(ctx, line, batch=True)
        if outcome.added:
            saved += outcome.added
        else:
            failed.append(line)
    parts = []
    if saved:
        parts.append(f"✅ Logged {len(saved)} entr{'y' if len(saved) == 1 else 'ies'}:\n"
                     + "\n".join(agent.added_text(v).removeprefix("✅ Logged ") for v in saved))
    if failed:
        parts.append(f"❌ Not saved ({len(failed)} line{'s' if len(failed) > 1 else ''}) — send "
                     "each again with its amount and item, e.g. \"150 tea by sagar\":\n"
                     + "\n".join(f"\"{ln}\"" for ln in failed))
    return "\n\n".join(parts)
