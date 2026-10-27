"""What a member's message means: text, photo, voice note, or a tapped button.

Exact command words (balance, recent, undo, settle, a name or a category) are matched by regex:
free, instant, and the same every time. Everything else goes to the AI. No growing synonym
lists here — "hisab aapo" reaches the model, which calls the same balance code.

Order of checks for a text message, first match wins:
  1. a settle PIN being typed      2. yes / no / a number for a waiting preview
  3. command words                 4. the AI
"""
import hmac
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai import agent, guards, media, proposals
from app.config import Settings
from app.core.formatting import today
from app.core.replies import Out, Reply
from app.db.models import Member
from app.integrations.gemini import LLM, LlmError
from app.services import balance, expenses, members, pending, settlements
from app.services.expenses import CATEGORIES

log = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 AI BOaT\n\n"
    "Add an expense: just type it — \"150 tea by sagar\", \"diesel 2k\".\n"
    "Several lines = several expenses. A photo of a bill or khata, or a voice note, works too.\n\n"
    "Commands:\n"
    "balance — who is pending, who gets back\n"
    "recent — last 5 entries\n"
    "undo — remove your own last entry\n"
    "<name> or <category> — e.g. \"sagar\", \"fuel expenses\"\n"
    "search ... — e.g. \"search diesel after 1 oct\"\n"
    "settle — settle up (needs the PIN)\n\n"
    "Or ask in your own words: \"hisab aapo\", \"aa mahine ketlo kharch thayo?\""
)

NOTHING_WAITING = agent.NOTHING_WAITING

Typing = Callable[[int], None]
FetchFile = Callable[[str], bytes | None]
_PICK = re.compile(r"^\s*#?\s*([1-9])\s*$")


@dataclass
class Env:
    """Everything a handler needs besides the message itself."""
    session: Session
    sender: Member
    chat_id: int
    settings: Settings
    llm: LLM
    typing: Typing | None = None
    fetch_file: FetchFile | None = None

    def ctx(self, *, confirm_writes: bool = False) -> agent.Context:
        return agent.Context(session=self.session, sender=self.sender, chat_id=self.chat_id,
                             today=today(self.settings.timezone), llm=self.llm,
                             confirm_writes=confirm_writes)

    def show_typing(self) -> None:
        if self.typing:
            self.typing(self.chat_id)


# ── Text ────────────────────────────────────────────────────────────────────────────────────

def handle_text(env: Env, text: str) -> list[Out]:
    raw = text.strip()
    if not raw:
        return []
    s, sender, chat_id = env.session, env.sender, env.chat_id

    # 1. A settle PIN is checked before anything else, so it can never be read as an amount or
    #    reach the model.
    waiting_pin, expired = pending.pop(s, chat_id, sender.id, pending.SETTLE_PIN)
    if waiting_pin is not None:
        if expired:
            return [Reply("⌛ PIN entry timed out. Send \"settle\" again to retry.")]
        if _pin_ok(raw, env.settings):
            return [Reply(_settle(env))]
        return [Reply("❌ Incorrect PIN. Settlement cancelled — send \"settle\" to try again.")]

    # 2. A waiting preview gets exactly one answer. Anything else drops it and is handled as a
    #    new message — an ignored preview must never leave someone stuck.
    answered = _answer_preview(env, raw)
    if answered is not None:
        return answered

    # 3. Command words. "/balance@AIBoatHQ_bot" -> "balance".
    cmd = re.sub(r"^/(\w+)@\w+", r"\1", raw).lstrip("/") if raw.startswith("/") else raw
    lower = cmd.lower().strip()

    if lower in ("help", "menu"):
        return [Reply(HELP_TEXT)]
    if lower == "settle":
        if not env.settings.settle_pin:
            return [Reply("⚙️ Settling is switched off: no SETTLE_PIN is configured.")]
        pending.put(s, chat_id, sender.id, pending.SETTLE_PIN, {})
        return [Reply("🔐 Enter the settle PIN to confirm:")]
    m = re.fullmatch(r"settle\s+(\S+)", lower)
    if m:
        if _pin_ok(m.group(1), env.settings):
            return [Reply(_settle(env))]
        return [Reply("❌ Send exactly: settle <PIN>")]
    if lower in ("balance", "bal"):
        return [Reply(balance.compute(s).text)]
    if lower == "recent":
        return [Reply(agent.recent_text(expenses.recent(s, 5)))]
    if lower == "undo":
        undone = expenses.undo_last(s, sender)
        return [Reply(agent.undo_text(undone) if undone
                      else f"No recent unsettled entry by {sender.name} to undo.")]
    filtered = _filter(s, lower)
    if filtered is not None:
        return [Reply(filtered)]

    # 4. The AI.
    env.show_typing()
    ctx = env.ctx()
    if "\n" in cmd:
        return [Reply(_batch(ctx, cmd))]
    outcome = agent.handle(ctx, cmd)
    return [Reply(outcome.reply, outcome.buttons)]


def _answer_preview(env: Env, raw: str) -> list[Out] | None:
    """None = no preview was waiting (or this message is something else): carry on routing."""
    is_yes, is_no = bool(guards.YES.match(raw)), bool(guards.NO.match(raw))
    pick = _PICK.match(raw)
    waiting, expired = pending.pop(env.session, env.chat_id, env.sender.id, pending.CONFIRM)
    if waiting is None:
        return None
    if expired:
        return [Reply(proposals.TIMED_OUT)] if (is_yes or is_no or pick) else None
    p = waiting.payload
    needs_pick = len(p.get("choices", [])) > 1
    if needs_pick and pick:
        choice = int(pick.group(1)) - 1
        if choice < len(p["choices"]):
            return _apply(env, p, choice)
    if is_yes and needs_pick:
        # "yes" cannot say WHICH one. Keep the question open instead of guessing.
        proposals.store(env.session, env.chat_id, env.sender, p)
        return [Reply("Tap the number it belongs to, or ❌ Cancel.", proposals.buttons(p))]
    if is_yes:
        return _apply(env, p, None)
    if is_no:
        return [Reply(proposals.DROPPED)]
    hook = proposals.REGISTRY[p["kind"]]
    if getattr(hook, "on_other_text", None):
        return hook.on_other_text(env.ctx(), p, raw)
    return None


def _apply(env: Env, p: dict, pick: int | None) -> list[Out]:
    nested = env.session.begin_nested()
    try:
        out = proposals.apply(env.ctx(), p, pick)
        nested.commit()
        return out
    except Exception:  # noqa: BLE001 — nothing half-applied stays; the user is told plainly
        nested.rollback()
        log.exception("applying a confirmed change failed", extra={"fields": {"kind": p.get("kind")}})
        return [Reply("⚠️ That could not be saved, so nothing was changed. Please send it again.")]


def _pin_ok(given: str, settings: Settings) -> bool:
    return bool(settings.settle_pin) and hmac.compare_digest(given.strip().encode(),
                                                             settings.settle_pin.encode())


def _settle(env: Env) -> str:
    return settlements.settle(env.session, env.sender, today(env.settings.timezone)).text


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


# ── Buttons ─────────────────────────────────────────────────────────────────────────────────

BUTTON_TEXT = re.compile(r"^(yes|no|[1-9])$")


def handle_button(env: Env, data: str) -> list[Out]:
    """A tapped button carries the text a person could have typed ("yes", "no", "2").
    Only known values are accepted: button data comes from the client, and an unknown value
    must not reach the AI (or cost a model call) as if someone had typed it."""
    if BUTTON_TEXT.match(data):
        return handle_text(env, data)
    return []


# ── Photo ───────────────────────────────────────────────────────────────────────────────────

def handle_photo(env: Env, file_id: str, caption: str) -> list[Out]:
    # A new photo replaces any unanswered preview: one thing owns "yes" at a time.
    pending.drop(env.session, env.chat_id, env.sender.id, pending.CONFIRM)
    data = env.fetch_file(file_id) if env.fetch_file else None
    if not data:
        return [Reply("❌ Couldn't download that photo. Please send it again.")]
    env.show_typing()
    ctx = env.ctx()
    try:
        items, problems = media.read_photo(env.llm, data, sender=env.sender, people=ctx.people,
                                           today=ctx.today, caption=caption)
    except LlmError as err:
        return [Reply(agent.LLM_ERROR_TEXT.get(err.kind, agent.LLM_ERROR_TEXT["unknown"]))]
    if not items:
        why = ("\n" + "\n".join(problems)) if problems else ""
        return [Reply("❌ Couldn't find an expense I could save in that photo. Try again, or type "
                      f"it as text.{why}")]
    p = proposals.expenses_proposal(items, "photo")
    proposals.store(env.session, env.chat_id, env.sender, p)
    out: list[Out] = [Reply(proposals.preview(p), proposals.buttons(p))]
    if problems:
        out.append(Reply("Not included:\n" + "\n".join(problems)))
    return out


# ── Voice ───────────────────────────────────────────────────────────────────────────────────

def handle_voice(env: Env, file_id: str, mime_type: str, caption: str,
                 duration_s: int = 0) -> list[Out]:
    pending.drop(env.session, env.chat_id, env.sender.id, pending.CONFIRM)
    data = env.fetch_file(file_id) if env.fetch_file else None
    if not data:
        return [Reply("❌ Couldn't download that voice note. Please send it again.")]
    env.show_typing()
    try:
        transcript = media.transcribe(env.llm, data, mime_type or "audio/ogg")
    except LlmError as err:
        return [Reply(agent.LLM_ERROR_TEXT.get(err.kind, agent.LLM_ERROR_TEXT["unknown"]))]
    if not transcript:
        return [Reply("❌ That voice note sounded empty. Try again, or type it.")]

    # The transcript is shown first, so a mishearing is obvious before anything is saved.
    out: list[Out] = [Reply(f"🎤 Heard: \"{transcript}\"")]
    # A typed caption is the user's own words, so it travels with the transcript.
    text = f"{transcript} ({caption})" if caption else transcript
    outcome = agent.handle(env.ctx(confirm_writes=True), text)
    out.append(Reply(outcome.reply, outcome.buttons))
    return out
