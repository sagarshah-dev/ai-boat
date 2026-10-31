"""What a member's message means: text, photo, voice note, or a tapped button.

Exact command words (balance, recent, undo, settle, a name or a category) are matched by regex:
free, instant, and the same every time. Everything else goes to the AI. No growing synonym
lists here — "hisab aapo" reaches the model, which calls the same balance code.

Order of checks for a text message, first match wins:
  1. a settle PIN being typed      2. the one line for a farm photo, or advice after 📚
  3. yes / no / a number / details for a waiting preview, or "general advice"
  4. command words                 5. the AI
"""
import hmac
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.ai import agent, farm_tools, guards, media, meeting_tools, proposals
from app.config import Settings
from app.core.formatting import inr, today
from app.core.replies import Document, Out, Reply
from app.db.models import Member
from app.integrations.gemini import LLM, LlmError
from app.telegram import farm_ui
from app.services import balance, expenses, farm, mail, members, pending, rag_index, reports, settlements
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
    "report [scope] — PDF, e.g. \"report\", \"report fuel\", \"report last month\"\n"
    "email [scope] — the same PDF by email\n"
    "settle — settle up (needs the PIN)\n\n"
    "Farm journal:\n"
    "\"aaje badha aamba ma neem spray, 50 ml, savare\" — a farm note (shown first, then Save)\n"
    "a photo with one line — a farm note with its photo\n"
    "\"humic chhelle kyare aapyu?\", \"ketla chhod che?\" — exact answers from the notes\n"
    "\"pehla daag padya tyare shu karyu?\" — searched by meaning, with sources\n"
    "a long voice note — saved as the farmer's advice\n"
    "menu — buttons for all of this\n\n"
    "Or ask in your own words: \"hisab aapo\", \"aa mahine ketlo kharch thayo?\""
)

NOTHING_WAITING = agent.NOTHING_WAITING

Typing = Callable[[int], None]
FetchFile = Callable[[str], bytes | None]
_PICK = re.compile(r"^\s*#?\s*([1-9])\s*$")
_GENERAL = re.compile(r"^\s*(💡\s*)?(general\s+)?advice\s*$", re.IGNORECASE)
MODE_TTL = timedelta(minutes=5)


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

    def ctx(self, *, confirm_writes: bool = False, photo: str = "",
            source: str = "text") -> agent.Context:
        return agent.Context(session=self.session, sender=self.sender, chat_id=self.chat_id,
                             today=today(self.settings.timezone), llm=self.llm,
                             confirm_writes=confirm_writes, settings=self.settings,
                             now=datetime.now(ZoneInfo(self.settings.timezone)),
                             photo=photo, source=source)

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
            return _settle(env)
        return [Reply("❌ Incorrect PIN. Settlement cancelled — send \"settle\" to try again.")]

    # 2. A farm photo sent without a caption: this line is its description.
    if not raw.startswith("/"):
        photo, expired = pending.pop(s, chat_id, sender.id, pending.FARM_PHOTO)
        if photo is not None and not expired:
            env.show_typing()
            return _reply(agent.handle(env.ctx(photo=photo.payload["file_id"], source="photo"), raw))
        # After tapping 📚: typed text is advice — same preview as a voice note.
        mode, expired = pending.pop(s, chat_id, sender.id, pending.ADVICE_MODE)
        if mode is not None and not expired:
            return _advice_preview(env, raw, "text")

    # 3. A waiting preview gets exactly one answer. Anything else drops it and is handled as a
    #    new message — an ignored preview must never leave someone stuck.
    answered = _answer_preview(env, raw)
    if answered is not None:
        return answered
    if _GENERAL.match(raw) and pending.get(s, chat_id, sender.id, pending.GENERAL):
        env.show_typing()
        return farm_tools.general_answer(env.ctx())

    # 3. Command words. "/balance@AIBoatHQ_bot" -> "balance".
    cmd = re.sub(r"^/(\w+)@\w+", r"\1", raw).lstrip("/") if raw.startswith("/") else raw
    lower = cmd.lower().strip()

    if lower in ("help", "start"):
        return [Reply(HELP_TEXT)]
    if lower == "menu":
        return [Reply(farm_ui.MENU_TEXT, farm_ui.menu_buttons())]
    if lower == "reindex":
        env.show_typing()
        try:
            r = rag_index.sync(s, env.llm)
        except LlmError as err:
            return [Reply(agent.LLM_ERROR_TEXT.get(err.kind, agent.LLM_ERROR_TEXT["unknown"]))]
        return [Reply(f"🔎 Search index updated — {r['embedded']} changed, {r['deleted']} removed, "
                      f"{r['total']} pieces in total.")]
    if lower == "settle":
        if not env.settings.settle_pin:
            return [Reply("⚙️ Settling is switched off: no SETTLE_PIN is configured.")]
        pending.put(s, chat_id, sender.id, pending.SETTLE_PIN, {})
        return [Reply("🔐 Enter the settle PIN to confirm:")]
    m = re.fullmatch(r"settle\s+(\S+)", lower)
    if m:
        if _pin_ok(m.group(1), env.settings):
            return _settle(env)
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
    if re.match(r"report\b", lower):
        return _report(env, cmd)
    if re.match(r"email\b", lower):
        return _email(env, cmd)
    if re.match(r"(search|find)\b", lower):
        # Any wording after the keyword goes to the AI; the fixed grammar is the fallback when
        # the AI is down — searching must keep working without the model.
        env.show_typing()
        outcome = agent.handle(env.ctx(), cmd)
        if outcome.llm_error:
            scope = reports.parse_scope(s, cmd, today(env.settings.timezone))
            return [Reply("🤖 AI is unavailable — basic search:\n\n"
                          + reports.search_text(scope.rows(s), scope.label))]
        return [Reply(outcome.reply, outcome.buttons)]

    # 5. The AI. Several lines are several expenses — unless it is a farm note typed over
    #    two lines, which is one message.
    env.show_typing()
    ctx = env.ctx()
    notes = farm_ui.several_notes(cmd)
    if notes:
        return [Reply(farm_ui.several_notes_reply(notes))]
    if "\n" in cmd and not farm_ui.multi_line_to_ai(cmd):
        return [Reply(_batch(ctx, cmd))]
    return _reply(agent.handle(ctx, cmd))


def _reply(outcome: agent.Outcome) -> list[Out]:
    return [Reply(outcome.reply, outcome.buttons), *outcome.extras]


def _advice_preview(env: Env, text: str, source: str) -> list[Out]:
    if len(text.strip()) < farm.ADVICE_MIN_CHARS:
        return [Reply("📚 That is too short to be advice. Nothing was saved — send the full advice "
                      "or plan.")]
    title = f"Farmer voice note {today(env.settings.timezone).strftime('%d-%m-%Y')}" if source == "voice" else ""
    p = farm_tools.advice_proposal(text.strip(), title, source, title_from_model=False)
    proposals.store(env.session, env.chat_id, env.sender, p)
    return [Reply(proposals.preview(p), proposals.buttons(p))]


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


def _now(env: Env) -> datetime:
    return datetime.now(ZoneInfo(env.settings.timezone))


def _settle(env: Env) -> list[Out]:
    """Settle, then a PDF snapshot of exactly what was settled (taken from the same rows and the
    same numbers, so the archive can never disagree with the plan), emailed if email is set up."""
    day = today(env.settings.timezone)
    result = settlements.settle(env.session, env.sender, day)
    out: list[Out] = [Reply(result.text)]
    if result.settlement_id is None:
        return out
    rows = expenses.by_ids(env.session, result.balance.expense_ids)
    scope = reports.Scope("since-settle", f"Settlement #{result.settlement_id} snapshot")
    report = reports.build_report(env.session, scope, today=day, now=_now(env),
                                  balance=result.balance, rows=rows)
    out.append(Document(f"settlement-{result.settlement_id}-{report.filename.split('-', 2)[-1]}",
                        report.data, f"📄 Settlement snapshot — {report.count} entries, ₹{inr(report.total)}"))
    if mail.ready(env.settings):
        out.append(mail.report_email(env.settings, report, "Settlement Report", result.balance))
    return out


def _report(env: Env, cmd: str) -> list[Out]:
    s, day = env.session, today(env.settings.timezone)
    scope = reports.parse_scope(s, cmd, day)
    report = reports.build_report(s, scope, today=day, now=_now(env))
    if not report.count:
        return [Reply(f"📄 No entries found for {scope.label} — no PDF to send.")]
    return [Document(report.filename, report.data,
                     f"📄 {scope.label} — {report.count} entries, ₹{inr(report.total)} total")]


def _email(env: Env, cmd: str) -> list[Out]:
    if not mail.ready(env.settings):
        return [Reply(mail.NOT_SET_UP)]
    s, day = env.session, today(env.settings.timezone)
    scope = reports.parse_scope(s, cmd, day)
    report = reports.build_report(s, scope, today=day, now=_now(env))
    if not report.count:
        # The old bot mailed an empty PDF here; "report" already said "no entries". Now both do.
        return [Reply(f"📧 No entries found for {scope.label} — nothing to email.")]
    return [Reply(f"📧 Emailing {scope.label} — {report.count} entries, ₹{inr(report.total)}…"),
            mail.report_email(env.settings, report, "Expense Report")]


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
_FOLLOW_UP = re.compile(r"^fu:(\d+):([wpnu])$")
_MENU = re.compile(r"^menu:([a-z]+)$")
_PLANT = re.compile(r"^plant:(.{1,40})$")


def handle_button(env: Env, data: str) -> list[Out]:
    """A tapped button carries the text a person could have typed ("yes", "no", "2", "Mango"),
    or one of a few fixed actions (menu, 7-day follow-up). Only known values are accepted:
    button data comes from the client, and an unknown value must not reach the AI (or cost a
    model call) as if someone had typed it."""
    if BUTTON_TEXT.match(data):
        return handle_text(env, data)
    if data == "advice":
        if not pending.get(env.session, env.chat_id, env.sender.id, pending.GENERAL):
            return [Reply(NOTHING_WAITING)]
        env.show_typing()
        return farm_tools.general_answer(env.ctx())
    if data == "kn:msg":
        return _advice_as_message(env)
    m = _PLANT.match(data)
    if m:
        return _plant_button(env, m.group(1))
    m = _FOLLOW_UP.match(data)
    if m:
        return farm_tools.followup_answer(env.ctx(), int(m.group(1)),
                                          farm_tools.FOLLOW_UP_BUTTONS[m.group(2)])
    m = _MENU.match(data)
    if m:
        return _menu_item(env, m.group(1))
    m = farm_tools.HOP.match(data)
    if m:
        return farm_tools.hop_answer(env.ctx(), int(m.group(1)))
    return []


def _plant_button(env: Env, name: str) -> list[Out]:
    """Valid only while this person's note is waiting for its plant; the name must be a known
    plant, so a forged button can't inject text."""
    waiting = pending.get(env.session, env.chat_id, env.sender.id, pending.PLANT)
    if not waiting:
        return [Reply(NOTHING_WAITING)]
    known = farm.plant_names(farm.plants(env.session))
    if name == "+add":
        return _add_plant(env, waiting.payload.get("unknown", ""))
    if name == "all":
        return handle_text(env, "badha chhod")
    if name in known:
        return handle_text(env, name)
    return []


def _add_plant(env: Env, name: str) -> list[Out]:
    """➕ under "not in the plant list": admin only (a plant list is farm history), then the
    waiting note continues with the new plant as if it had been typed."""
    if env.sender.role != "admin":
        return [Reply("Only the admin can add a plant.")]
    if not name:
        return []
    farm.add_plant(env.session, name, today(env.settings.timezone))
    return [Reply(f"✅ Added \"{name.title()}\" to the plant list (count 0 — set it later)."),
            *handle_text(env, name)]


def _advice_as_message(env: Env) -> list[Out]:
    """▶️ on a long voice note: it was not advice after all, so run it like any voice note."""
    waiting, expired = pending.pop(env.session, env.chat_id, env.sender.id, pending.CONFIRM)
    if waiting is None or waiting.payload.get("kind") != "farm_advice":
        if waiting is not None and not expired:
            proposals.store(env.session, env.chat_id, env.sender, waiting.payload)   # not ours: put back
        return [Reply(NOTHING_WAITING)]
    if expired:
        return [Reply(proposals.TIMED_OUT)]
    env.show_typing()
    voice = waiting.payload.get("source") == "voice"
    return _reply(agent.handle(env.ctx(confirm_writes=voice, source="voice" if voice else "text"),
                               waiting.payload["text"]))


def _menu_item(env: Env, item: str) -> list[Out]:
    s, chat_id, who = env.session, env.chat_id, env.sender.id
    if item == "note":
        pending.put(s, chat_id, who, pending.FARM_MODE, {}, ttl=MODE_TTL)
        return [Reply("🌱 Send the farm note as text or voice, or a photo with one line "
                      "(e.g. \"aamba na pan par daag, neem spray karyu\").")]
    if item == "ask":
        return [Reply("🔍 Ask in your own words, e.g. \"pehla daag padya tyare shu karyu?\" "
                      "or \"humic chhelle kyare aapyu?\"")]
    if item == "advice":
        pending.put(s, chat_id, who, pending.ADVICE_MODE, {}, ttl=MODE_TTL)
        return [Reply("📚 Send the advice or plan now (within 5 minutes) — a voice note, or type "
                      "it. I'll show it before anything is saved.")]
    if item == "balance":
        return [Reply(balance.compute(s).text)]
    if item == "counts":
        return [Reply(farm_tools.counts_text(env.ctx(), farm.plants(s)))]
    if item == "week":
        ctx = env.ctx()
        monday = ctx.today - timedelta(days=ctx.today.weekday())
        return [Reply(farm_tools.activities_text(ctx, df=monday, dt=ctx.today))]
    if item == "meetings":
        return [Reply(meeting_tools.upcoming_text(env.ctx()))]
    return [Reply(farm_ui.MENU_TEXT, farm_ui.menu_buttons())]


# ── Photo ───────────────────────────────────────────────────────────────────────────────────

def handle_photo(env: Env, file_id: str, caption: str) -> list[Out]:
    s, chat_id, who = env.session, env.chat_id, env.sender.id
    # A new photo replaces any unanswered preview: one thing owns "yes" at a time.
    pending.drop(s, chat_id, who, pending.CONFIRM)
    mode, expired = pending.pop(s, chat_id, who, pending.FARM_MODE)
    if farm_ui.photo_is_farm_note(caption, mode is not None and not expired):
        # A leaf, not a bill. The photo is not downloaded or stored: Telegram keeps it, and its
        # file_id on the note lets the bot show it again with a later answer.
        if not caption.strip():
            pending.put(s, chat_id, who, pending.FARM_PHOTO, {"file_id": file_id}, ttl=MODE_TTL)
            return [Reply("📷 Got the photo. Now send one line about it, e.g. \"aamba na pan par "
                          "daag\" (within 5 minutes).")]
        env.show_typing()
        return _reply(agent.handle(env.ctx(photo=file_id, source="photo"), caption))
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
    mode, expired = pending.pop(env.session, env.chat_id, env.sender.id, pending.ADVICE_MODE)
    advice_mode = mode is not None and not expired
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

    # The farmer's advice is a long voice note: shown, and kept until a button decides — never
    # saved, and never run as commands, on a guess.
    if farm_ui.is_knowledge_voice(duration_s, caption, advice_mode, env.settings.knowledge_min_seconds):
        return [Reply(f"🎤 Heard ({duration_s or '?'}s):\n\"{farm_ui.preview(transcript)}\""),
                *_advice_preview(env, transcript, "voice")]

    # The transcript is shown first, so a mishearing is obvious before anything is saved.
    out: list[Out] = [Reply(f"🎤 Heard: \"{transcript}\"")]
    # A typed caption is the user's own words, so it travels with the transcript.
    text = f"{transcript} ({caption})" if caption else transcript
    return out + _reply(agent.handle(env.ctx(confirm_writes=True, source="voice"), text))
