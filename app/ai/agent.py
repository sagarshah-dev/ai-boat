"""Free text -> Gemini function calling -> checked tool runs -> reply.

The model decides WHICH tool and with what arguments. This file decides whether those
arguments are allowed, runs the tool, and writes every reply that carries money from a fixed
template — so a number the user reads is always the number in the database, never a paraphrase.

Ported from the old bot's main.py (/ai/ask), expense part. Differences:
- identity is the Telegram member, not a first name;
- "how much was it?" waits in the database (app/services/pending.py), not in memory;
- every tool runs inside a savepoint: a failing tool rolls back only itself;
- recent and search replies are templates too (the old bot let the model total them);
- "2 lakh" grounds ₹2,00,000 (the old bot matched it to ₹2).
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.ai import farm_tools, guards, meeting_tools, proposals, tools
from app.config import Settings
from app.core.formatting import entries, fmt_date, inr, parse_date, to_rupees
from app.core.replies import Buttons, Out
from app.db.models import Member
from app.integrations.gemini import LLM, LlmError, ToolCall
from app.services import balance, expenses, farm, members, pending
from app.services.expenses import ExpenseView

log = logging.getLogger(__name__)

MAX_ROUNDS = 5           # safety brake on the tool loop
MAX_ROWS = 20            # rows listed in one reply / sent to the model
LLM_BUDGET_S = 55        # total model time for one message
MAX_TRIES = 3            # chances to answer "how much was it?"

LLM_ERROR_TEXT = {
    "timeout": "🤖 Gemini took too long to answer. Nothing was saved — please send it again.",
    "overloaded": "🤖 AI is busy right now (Google Gemini is overloaded). Nothing was saved — "
                  "please try again in a minute.",
    "quota": "🤖 Today's free AI limit is used up. Nothing was saved — please try again later "
             "(the limit resets daily).",
    "config": "⚠️ AI setup problem on our side (not your message). Nothing was saved. "
              "Admin: check the logs.",
    "unknown": "🤖 The AI could not answer. Nothing was saved — please try again in a minute.",
}
STILL_WORKS = "\nThese still work: balance, recent, undo, settle."
NOTHING_WAITING = "Nothing is waiting for your yes/no right now."


@dataclass
class Context:
    session: Session
    sender: Member
    chat_id: int
    today: date
    llm: LLM
    people: list[Member] = field(default_factory=list)   # who shares expenses
    # Voice notes: every write is PROPOSED and saved only after "yes" (a transcript can be misheard).
    confirm_writes: bool = False
    settings: Settings | None = None
    now: datetime | None = None        # the owners' local time; tests pin it
    # Farm notes: a photo sent with the note (Telegram file_id), and where the words came from.
    photo: str = ""
    source: str = "text"
    details_asked: bool = False        # the one "dose / time?" question was already asked
    # The user's own words for a farm note when the text the model sees was glued from a question
    # and its answer ("pan par fodla" + "Mango"). The answer fills a field; it is not the note.
    note_text: str = ""
    unknown_plant: str = ""            # a plant the note names that the plants list does not have

    def __post_init__(self):
        if not self.people:
            self.people = members.split_members(self.session)
        self.people_words = members.name_words(self.people)


@dataclass
class ToolRun:
    name: str
    args: dict
    result: dict

    @property
    def ok(self) -> bool:
        return not self.result.get("error") and not self.result.get("skipped")

    @property
    def needs(self) -> str | None:
        """Which missing piece the CODE asks the user for (not the model): amount, description,
        time, ampm, new_time (slot taken), datetime (both day and time missing)."""
        for key in ("amount", "description", "payer", "time", "ampm", "new_time", "datetime",
                    "plant"):
            if self.result.get(f"needs_{key}"):
                return key
        return None


@dataclass
class Outcome:
    reply: str
    added: list[ExpenseView] = field(default_factory=list)
    runs: list[ToolRun] = field(default_factory=list)
    buttons: Buttons | None = None
    llm_error: str | None = None   # set when the model could not answer (kind: quota, timeout...)
    extras: list[Out] = field(default_factory=list)   # photos of the farm notes an answer cites


# ── Entry point ─────────────────────────────────────────────────────────────────────────────

def handle(ctx: Context, text: str, *, batch: bool = False) -> Outcome:
    """One message. batch=True is one line of a multi-line message: no follow-up question is
    remembered, because several lines could not all wait for an answer at once."""
    user_text = text.strip()
    waiting = None
    if not batch:
        waiting = next((w for kind in (pending.AMOUNT, pending.TIME, pending.PLANT)
                        if (w := pending.get(ctx.session, ctx.chat_id, ctx.sender.id, kind))), None)

    if not batch and not waiting and (guards.YES.match(user_text) or guards.NO.match(user_text)):
        # Never sent to the model: a bare "yes" with nothing to confirm has no safe meaning.
        return Outcome(NOTHING_WAITING)

    is_slot = bool(waiting) and _is_slot_reply(ctx, waiting, user_text)
    # "tea by sagar" (waiting) + "100" -> "tea by sagar 100", then the normal path: every gate
    # applies to the glued text exactly as if it had been typed in one go.
    message = _glue(waiting, user_text, ctx.people_words) if is_slot else user_text
    if is_slot and waiting.kind == pending.PLANT:
        # The note's photo and source travel with the question "which plant?".
        ctx.photo = ctx.photo or waiting.payload.get("photo", "")
        ctx.source = waiting.payload.get("source", ctx.source)
        # Live bug 29-10: the tapped "Guava" was saved inside the note ("...30 min karyu Guava"),
        # so a search for guava found a pomegranate note.
        ctx.note_text = waiting.payload.get("note", waiting.payload["message"])
        ctx.unknown_plant = waiting.payload.get("unknown", "")

    runs, reply, llm_error = _run_model(ctx, message)
    extras = [o for r in runs if r.ok for o in r.result.get("outs", [])]
    added = [r.result["expense"] for r in runs if r.name == tools.ADD_EXPENSE and r.ok
             and "expense" in r.result]
    found = [r.result["proposal"] for r in runs if r.ok and r.result.get("proposal")]
    proposal = None if batch else _merge_proposals(found)
    # "..., coffee by lalo" -> remember "coffee by lalo", so a plain "50" next completes it
    # (live bug 29-10: the bot said "send coffee 100", the user sent "50", it was lost).
    ask = None if (batch or proposal) else amount_question(ctx, message, runs)
    # Whatever the model did, the user hears about every part of the message that did NOT
    # become an entry (live bug: 4 expenses typed, 1 saved, no warning).
    reply = "\n\n".join(p for p in (reply, list_notes(ctx, message, runs, asking=ask)) if p)
    if batch:
        return Outcome(reply, added, runs, llm_error=llm_error, extras=extras)

    if proposal:
        proposals.store(ctx.session, ctx.chat_id, ctx.sender, proposal)
        # The preview must be shown word for word: the user is about to say "yes" to exactly it.
        reply = "\n\n".join(p for p in (reply, proposals.preview(proposal),
                                         dropped_proposals_note(found)) if p)
        return Outcome(reply, added, runs, proposals.buttons(proposal), extras=extras)
    reply, buttons = _follow_up(ctx, waiting, is_slot, user_text, runs, reply)
    if ask and not pending.get(ctx.session, ctx.chat_id, ctx.sender.id, pending.AMOUNT):
        pending.drop(ctx.session, ctx.chat_id, ctx.sender.id, pending.TIME)
        pending.put(ctx.session, ctx.chat_id, ctx.sender.id, pending.AMOUNT,
                    {"message": ask, "tries_left": MAX_TRIES})
    if buttons is None:
        buttons = next((r.result["buttons"] for r in runs if r.ok and r.result.get("buttons")), None)
    return Outcome(reply, added, runs, buttons, llm_error=llm_error, extras=extras)


def _is_slot_reply(ctx: Context, waiting: pending.Pending, text: str) -> bool:
    if waiting.kind == pending.PLANT:
        return farm_tools.is_plant_reply(farm.plants(ctx.session), text)
    if waiting.kind == pending.AMOUNT:
        return (bool(guards.amount_only_pattern(ctx.people_words).match(text))
                or _is_item_amount_reply(ctx, waiting, text))
    if waiting.payload.get("mode") == "ampm" and guards.DAYPART_ONLY.match(text):
        return True
    return bool(guards.TIME_ONLY.match(text))


def _is_item_amount_reply(ctx: Context, waiting: pending.Pending, text: str) -> bool:
    """"coffee 50" while "coffee by lalo" waits: the same item again, now with one amount. Not a
    new entry — as a new message it would be paid by the sender, not Lalo."""
    words = guards.item_words(text, ctx.people_words)
    asked = set(guards.item_words(waiting.payload["message"], ctx.people_words))
    return (len(guards.numbers_in(text)) == 1 and bool(words) and set(words) <= asked
            and not members.mentioned(ctx.people, text))


def _glue(waiting: pending.Pending, reply: str, people_words: set[str]) -> str:
    original = waiting.payload["message"]
    mode = waiting.payload.get("mode", "add")
    # People's names are not items: "100 by biren" keeps its payer (eval 29-10 caught the
    # names being dropped, so Biren's 100 became the sender's).
    if waiting.kind == pending.AMOUNT and guards.item_words(reply, people_words):
        # "coffee 50": only the amount is new; the item and the payer are already in `original`.
        return f"{original} {guards.numbers_in(reply)[0].normalize():f}"
    if mode == "ampm":
        return f"{original} ({reply})"
    if mode == "replace":
        # The slot was taken: the new time replaces the old one, the rest (day, title) stays.
        return f"{original} — not at that time, make it {reply} instead"
    return f"{original} {reply}"


def _merge_proposals(found: list[dict]) -> dict | None:
    """One "yes" confirms one thing. Several expenses heard in one voice note become one list;
    otherwise the first proposal is kept and the rest are left for the user to send again."""
    if not found:
        return None
    items = [it for p in found if p["kind"] == "expenses" for it in p["items"]]
    if items and all(p["kind"] == "expenses" for p in found):
        return proposals.expenses_proposal(items, found[0].get("source", "voice"))
    return found[0]


def dropped_proposals_note(found: list[dict]) -> str:
    """Said out loud when one message held several things to confirm and only the first is shown."""
    if len(found) < 2 or all(p["kind"] == "expenses" for p in found):
        return ""
    return (f"ℹ️ Your message had {len(found)} things to save; this is the first. "
            "Send the others one per message.")


# ── The model loop ──────────────────────────────────────────────────────────────────────────

def system_prompt(ctx: Context) -> str:
    names = ", ".join(m.name for m in ctx.people)
    sender_pays = members.match_name(ctx.people, ctx.sender.name) is not None
    payer_rule = (f"If paid_by is not mentioned, default it to the sender ({ctx.sender.name}). "
                  if sender_pays else
                  f"The sender ({ctx.sender.name}) is a guest and never pays a share: paid_by must "
                  f"be one of {names}. If the message does not say who paid, ask. ")
    return (
        f"You are the assistant of a farmhouse shared by {names}. "
        f"Today is {fmt_date(ctx.today)} ({ctx.today.strftime('%a')}). "
        f"The sender's name is {ctx.sender.name}. "
        "Use the available tools to handle the user's request. "
        + payer_rule +
        "LANGUAGE: reply in the same language and script as the user's latest message. "
        "English -> English. Gujarati script -> Gujarati script. Romanized Hindi/Gujarati -> the "
        "same romanized style. Never mix scripts. "
        "FORMAT: plain text only. No markdown, no asterisks, no tables. Use '-' for lists and the "
        "₹ symbol with Indian digit grouping (₹1,12,412). "
        "WORDING: never use the words 'owe', 'owes' or 'owed'. Say '<name> pending ₹X', "
        "'<name> gets back ₹X' and '<A> pays <B> ₹X'. "
        "DATES: for any date condition pass date_from/date_to (DD-MM-YYYY) to search_expenses; "
        "never filter or total dates yourself. "
        "AMOUNT: never guess an amount. If the user wants to add an expense but the message has "
        "no amount in digits, call add_expenses with that item at amount 0 — the system will ask for it. "
        "MEETINGS: schedule_meeting, cancel_meeting and reschedule_meeting only PROPOSE; the user "
        "confirms with yes, so never say a meeting is booked, cancelled or moved. Work out dates "
        "from 'kale', 'tomorrow', 'next sunday' with today's date; give time as 24-hour HH:MM. "
        "If the date or the time is missing, leave it empty — the system asks. Always call "
        "find_meetings before cancel_meeting or reschedule_meeting; if more than one meeting could "
        "match, ask which one. "
        + farm_tools.SYSTEM_RULES +
        "BALANCE: if you used get_balances with other tools, never rewrite its numbers. "
        "If a tool returns an error, explain it simply and ask the user what is needed."
    )


def _run_model(ctx: Context, message: str) -> tuple[list[ToolRun], str, str | None]:
    runs: list[ToolRun] = []
    done_writes: set[str] = set()
    try:
        chat = ctx.llm.start(system_prompt(ctx), all_tools(ctx), LLM_BUDGET_S)
        turn = chat.send_user(message)
        nudged = False
        for round_no in range(1, MAX_ROUNDS + 1):
            if not turn.calls:
                if nudged:   # it had nothing to add: keep the fixed templates, list_notes warns
                    return runs, _templates(runs) or turn.text or "", None
                return runs, turn.text or "Sorry, I couldn't complete that. Please try again.", None
            expanded = expand_calls(turn.calls)
            round_runs = [_run_tool(ctx, call, message, done_writes) for _, call in expanded]
            runs += round_runs
            _log_round(round_no, round_runs)
            needs_model = any(r.name in NEEDS_MODEL and r.ok for r in round_runs)
            left = [] if nudged else unsaved_parts(ctx, message, runs)
            if not _unresolved_errors(runs) and not needs_model and not left:
                # Everything left is a template or a missing field the code asks about itself:
                # no second model call — cheaper, faster, and the numbers can't be reworded.
                return runs, _templates(runs), None
            if round_no == MAX_ROUNDS:
                break   # don't pay for a model answer we would not use
            results = _results_for_model(turn.calls, expanded, round_runs)
            if left:
                # The model added fewer entries than the message lists. Ask once more — with the
                # stronger fallback model, since the main one already skipped them. If they are
                # still missing, list_notes tells the user (never a silent drop).
                nudged = True
                getattr(chat, "escalate", lambda: None)()
                name, last = results[-1]
                results[-1] = (name, {**last, "not_saved_yet": left,
                                      "note": "These parts of the message were not added. Call "
                                              "add_expense once for each of them."})
            turn = chat.send_tool_results(results)
        log.warning("tool loop hit the round limit")
        return runs, _templates(runs) or "Sorry, I couldn't complete that. Please try again.", None
    except LlmError as err:
        log.warning("llm failed", extra={"fields": {"kind": err.kind}})
        wrote = [r for r in runs if r.ok and (r.result.get("expense") or r.result.get("deleted"))]
        if any(r.ok and r.result.get("proposal") for r in runs):
            return runs, _templates(runs), None   # the preview is added by the caller; nothing saved
        if wrote:
            # The save happened; the model only failed to word the reply. Say exactly what was saved.
            return runs, _templates(wrote) + "\n(The change above is saved.)", None
        return runs, LLM_ERROR_TEXT.get(err.kind, LLM_ERROR_TEXT["unknown"]) + STILL_WORKS, err.kind


def _run_tool(ctx: Context, call: ToolCall, message: str, done_writes: set[str]) -> ToolRun:
    key = call.name + json.dumps(call.args, sort_keys=True, default=str)
    if call.name in tools.WRITE_TOOLS and key in done_writes:
        # The model sometimes repeats the same call in one turn; it must not save twice.
        return ToolRun(call.name, call.args, {"skipped": True})
    nested = ctx.session.begin_nested()
    try:
        result = execute_tool(ctx, call.name, call.args, message)
        nested.commit()
    except Exception:  # noqa: BLE001 — a tool bug is reported to the model, not a crash
        nested.rollback()
        log.exception("tool failed", extra={"fields": {"tool": call.name}})
        result = {"error": "The tool failed and nothing was changed. Tell the user to try again."}
    if call.name in tools.WRITE_TOOLS and not result.get("error"):
        done_writes.add(key)
    return ToolRun(call.name, call.args, result)


NEEDS_MODEL = set(meeting_tools.NEEDS_MODEL)


def expand_calls(calls: list[ToolCall]) -> list[tuple[int, ToolCall]]:
    """add_expenses(items=[a, b, c]) -> three add_expense runs, each checked on its own by the
    same gates. Returns (index of the model's call, call) so results go back per call."""
    out: list[tuple[int, ToolCall]] = []
    for i, call in enumerate(calls):
        if call.name != tools.ADD_EXPENSES:
            out.append((i, call))
            continue
        items = call.args.get("items")
        items = [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []
        # An empty list is "add an expense" with nothing in it: the amount gate asks.
        for item in items or [{"amount": 0}]:
            out.append((i, ToolCall(tools.ADD_EXPENSE, dict(item))))
    return out


def _results_for_model(calls: list[ToolCall], expanded: list[tuple[int, ToolCall]],
                       runs: list[ToolRun]) -> list[tuple[str, dict]]:
    """One function response per call the model made, as the API expects."""
    per_call: dict[int, list[ToolRun]] = {}
    for (i, _), run in zip(expanded, runs):
        per_call.setdefault(i, []).append(run)
    out = []
    for i, call in enumerate(calls):
        rs = per_call.get(i, [])
        if call.name == tools.ADD_EXPENSES:
            out.append((call.name, {"items": [_for_model(r.result) for r in rs]}))
        else:
            out.append((call.name, _for_model(rs[0].result) if rs else {}))
    return out


def _log_round(round_no: int, runs: list[ToolRun]) -> None:
    """One log line per model round: which tools, which amount and payer, and what happened.
    Live bug 29-10: "1 entry once, 4 the next time" could only be guessed, never shown."""
    log.info("tool round", extra={"fields": {"round": round_no, "calls": [
        {"tool": r.name, "amount": str(r.args.get("amount", "")),
         "paid_by": str(r.args.get("paid_by", "")),
         "result": "ok" if r.ok else ("skipped" if r.result.get("skipped")
                                      else (r.needs and f"needs_{r.needs}") or "error")}
        for r in runs]}})


def all_tools(ctx: Context):
    return (tools.expense_tools([m.name for m in ctx.people]) + meeting_tools.specs()
            + farm_tools.specs())


def _unresolved_errors(runs: list[ToolRun]) -> list[ToolRun]:
    """Errors the model must see and fix (bad date, unknown payer). A missing amount or item is
    NOT one — code asks the user directly. An error is resolved once a later call of the same
    tool succeeded (the model corrected itself)."""
    ok_names = {r.name for r in runs if r.ok}
    return [r for r in runs
            if r.result.get("error") and not r.needs and r.name not in ok_names]


def _for_model(result: dict) -> dict:
    """What the model gets back: the facts, not our Python objects."""
    return {k: v for k, v in result.items() if k not in ("expense", "text_reply", "outs", "buttons")}


# ── Tools ───────────────────────────────────────────────────────────────────────────────────

def execute_tool(ctx: Context, name: str, args: dict[str, Any], message: str) -> dict:
    if name in meeting_tools.NAMES:
        return meeting_tools.execute(ctx, name, args, message)
    if name in farm_tools.NAMES:
        return farm_tools.execute(ctx, name, args, message)
    if name == tools.ADD_EXPENSE:
        return _add_expense(ctx, args, message)
    if name == tools.GET_BALANCES:
        b = balance.compute(ctx.session)
        return {"text_reply": b.text, "total": b.total, "plan": [
            f"{t.from_name} pays {t.to_name} ₹{inr(t.amount)}" for t in b.plan]}
    if name == tools.GET_RECENT:
        n = _int(args.get("n"), 5, 1, MAX_ROWS)
        rows = expenses.recent(ctx.session, n)
        return {"text_reply": recent_text(rows), "count": len(rows)}
    if name == tools.SEARCH_EXPENSES:
        return _search(ctx, args, message)
    if name == tools.DELETE_LAST_EXPENSE:
        if ctx.confirm_writes:
            target = expenses.newest_own_open(ctx.session, ctx.sender)
            if target is None:
                return {"text_reply": f"No recent unsettled entry by {ctx.sender.name} to undo.",
                        "deleted": None}
            return {"proposal": proposals.undo_proposal(target)}
        undone = expenses.undo_last(ctx.session, ctx.sender)
        if undone is None:
            return {"text_reply": f"No recent unsettled entry by {ctx.sender.name} to undo.",
                    "deleted": None}
        return {"text_reply": undo_text(undone), "deleted": undone.id}
    return {"error": f"Unknown tool: {name}"}


def _add_expense(ctx: Context, args: dict, message: str) -> dict:
    try:
        amount = to_rupees(args.get("amount") or 0)
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    if amount <= 0:
        return {"error": "No amount. Nothing was saved; the system asks the user.",
                "needs_amount": True}
    if guards.ARITHMETIC.search(message):
        return {"error": "The message has a calculation (like 2 x 50), not a total. Nothing was "
                         "saved. Ask the user to send the total with the item, e.g. 100 tea."}
    if not guards.amount_in_text(amount, message):
        # The "tea by sagar" -> ₹10 bug: a valid-looking number that the user never wrote.
        return {"error": f"{amount} is not written in the user's message. Nothing was saved; "
                         "the system asks the user.", "needs_amount": True}
    description = str(args.get("description") or "").strip()
    if not guards.has_item_word(message, ctx.people_words) or not re.search(r"[^\W\d_]", description):
        return {"error": "No item named. Nothing was saved; the system asks the user.",
                "needs_description": True, "amount": amount}

    raw_date = str(args.get("date") or "").strip()
    expense_date = parse_date(raw_date) if raw_date else ctx.today
    if expense_date is None:
        return {"error": f"date '{raw_date}' is not DD-MM-YYYY. Call again with a valid date."}

    payer, why = resolve_payer(ctx, args, message, amount)
    if payer is None:
        return {"error": f"{why} Nothing was saved; the system asks the user who paid.",
                "needs_payer": True, "why": why, "amount": amount, "description": description}

    category = str(args.get("category") or "")
    merchant = str(args.get("merchant") or "")
    joined = _joined_items(ctx, message, amount, description)
    if joined:
        # "500 sevsad, coffee": is ₹500 both, or was coffee's amount forgotten? Money is never
        # guessed — one tap decides.
        item = proposals.expense_item(amount=amount, description=description, paid_by=payer,
                                      category=category, merchant=merchant, on=expense_date)
        return {"proposal": proposals.joined_proposal(item, *joined), "amount": amount,
                "paid_by": payer.name}
    if ctx.confirm_writes:
        return {"proposal": proposals.expenses_proposal([proposals.expense_item(
            amount=amount, description=description, paid_by=payer, category=category,
            merchant=merchant, on=expense_date)], "voice"), "amount": amount, "paid_by": payer.name}
    view = expenses.add(ctx.session, amount=amount, description=description, paid_by=payer,
                        entered_by=ctx.sender, expense_date=expense_date,
                        category=category, merchant=merchant)
    return {"expense": view, "text_reply": added_text(view), "id": view.id, "amount": view.amount,
            "paid_by": view.paid_by, "date": fmt_date(view.date)}


def resolve_payer(ctx: Context, args: dict, message: str,
                  amount: Decimal) -> tuple[Member | None, str]:
    """Who paid, decided from the USER's words. Returns (member, "") or (None, reason).

    The model's paid_by is an enum of members, so it can only ever answer with a member: when
    the named person was missing ("by lalo" before Lalo was a payer) it quietly picked the
    sender. So the code reads the names in the message itself and only falls back to the sender
    when nobody at all was named."""
    names = ", ".join(m.name for m in ctx.people)
    stranger = guards.unknown_payer_word(message, ctx.people_words)
    if stranger:
        return None, f"'{stranger}' does not share expenses (only {names})."
    toks = members.tokens(message)
    named = members.mentioned(ctx.people, message)
    for m in named:
        words = {a.lower() for a in (m.aliases or [])} | set(m.name.lower().split()[:1])
        # "lalo ne 500 aapya" = gave 500 TO Lalo. Who paid is unclear.
        if any(t in words and nxt == "ne" for t, nxt in zip(toks, toks[1:])) or any(
                t == w + "ને" for t in toks for w in words):
            return None, f"'{m.name} ne' could mean money given TO {m.name}."
    if len(named) == 1:
        return named[0], ""
    if len(named) > 1:
        # "tea 150 by sagar, vadapav 250 by biren": the name in this amount's own part decides.
        part = guards.amount_segment(amount, message) or ""
        here = members.mentioned(named, part)
        if len(here) == 1:
            return here[0], ""
        return None, f"More than one person is named ({', '.join(m.name for m in named)})."
    sender = members.match_name(ctx.people, ctx.sender.name)
    by_model = members.match_name(ctx.people, args.get("paid_by"))
    if by_model is not None and by_model is not sender:
        # Nobody is named in the message, yet the model chose someone else: a guess.
        return None, "The message does not say who paid."
    if sender is None:
        return None, f"The sender ({ctx.sender.name}) does not share expenses."
    return sender, ""


def _joined_items(ctx: Context, message: str, amount: Decimal,
                  description: str) -> tuple[list[str], list[str]] | None:
    """(own words, other words) when the model put an amount-less item from the list into this
    entry: "500 sevsad, coffee" -> (["sevsad"], ["coffee"]). None when nothing was joined."""
    loose = guards.amountless_items(message, ctx.people_words)
    if not loose:
        return None
    desc = set(guards.item_words(description, ctx.people_words))
    extra = [w for w in loose if w in desc]
    own = [w for w in guards.item_words(guards.amount_segment(amount, message) or "",
                                        ctx.people_words) if w in desc and w not in extra]
    return (own, extra) if extra and own else None


def unsaved_parts(ctx: Context, message: str, runs: list[ToolRun]) -> list[str]:
    """The comma-parts of a list that have an amount but did not become an entry (or a proposal,
    or a question to the user). Each handled amount covers one part, so "100 tea, 100 coffee"
    with one entry still shows "100 coffee"."""
    parts = guards.segments(message)
    adds = [r for r in runs if r.name == tools.ADD_EXPENSE]
    if len(parts) < 2 or not adds or guards.ARITHMETIC.search(message):
        return []
    handled = [Decimal(str(r.result["amount"])) for r in adds
               if r.result.get("amount") is not None
               and (r.ok or r.result.get("needs_payer") or r.result.get("needs_description"))]
    left = []
    for part in parts:
        nums = guards.numbers_in(part)
        if not nums or re.search(r"\btotal\b", part, re.IGNORECASE):
            continue
        hit = next((h for h in handled if any(abs(h - n) < Decimal("0.005") for n in nums)), None)
        if hit is None:
            left.append(part)
        else:
            handled.remove(hit)
    return left


def _loose_parts(ctx: Context, message: str, runs: list[ToolRun]) -> list[tuple[str, str]]:
    """(item, its comma-part) for items written with no amount that did not end up in any saved
    or proposed entry: "150 tea, coffee by lalo" -> [("coffee", "coffee by lalo")]."""
    if not any(r.name == tools.ADD_EXPENSE for r in runs):
        return []
    taken = set()
    for r in runs:
        if r.name != tools.ADD_EXPENSE or not r.ok:
            continue
        p = r.result.get("proposal") or {}
        descs = [it["description"] for it in p.get("items", [])] + (
            [p["item"]["description"]] if p.get("item") else [])
        if r.result.get("expense"):
            descs.append(r.result["expense"].description)
        for d in descs:
            taken |= set(guards.item_words(d, ctx.people_words))
    out = []
    for part in guards.segments(message):
        if guards.numbers_in(part):
            continue
        for w in guards.item_words(part, ctx.people_words):
            if w not in taken and w in guards.amountless_items(message, ctx.people_words):
                out.append((w, part))
    return out


def amount_question(ctx: Context, message: str, runs: list[ToolRun]) -> str | None:
    """The text to remember for the first item with no amount, so "50" can complete it. None if
    who paid for it would be unclear (two people named, none in its own part)."""
    loose = _loose_parts(ctx, message, runs)
    if not loose:
        return None
    _, part = loose[0]
    if members.mentioned(ctx.people, part):
        return part
    named = members.mentioned(ctx.people, message)
    if len(named) == 1:
        return f"{part} by {named[0].name.split()[0]}"
    return part if not named else None


def list_notes(ctx: Context, message: str, runs: list[ToolRun], asking: str | None = None) -> str:
    """Code-written lines for everything in the message that was not saved: a payer the code
    could not be sure of, a listed amount that never became an entry, an item with no amount."""
    notes = []
    for r in runs:
        if r.name == tools.ADD_EXPENSE and r.result.get("needs_payer"):
            first = ctx.people[0].name.split()[0].lower() if ctx.people else "sagar"
            notes.append(f"❌ Not saved: ₹{inr(r.result['amount'])} {r.result['description']} — "
                         f"who paid? {r.result['why']}\nSend it again with the name, e.g. "
                         f"\"{inr(r.result['amount'])} {r.result['description']} by {first}\".")
    if not any(r.name == tools.ADD_EXPENSE for r in runs):
        return "\n\n".join(notes)
    left = unsaved_parts(ctx, message, runs)
    if left:
        notes.append("⚠️ Not saved: " + ", ".join(f"\"{p}\"" for p in left)
                     + "\nPlease send " + ("it" if len(left) == 1 else "them") + " again.")
    loose = list(dict.fromkeys(w for w, _ in _loose_parts(ctx, message, runs)))
    if loose and asking:
        first, rest = loose[0], loose[1:]
        notes.append(f"📝 No amount for {first}. How much was it?\nSend just the amount, e.g. "
                     "100 (within 2 minutes).")
        if rest:
            notes.append(f"❌ No amount for {', '.join(rest)} either, so not saved yet. Send it "
                         f"with the amount afterwards, e.g. \"{rest[0]} 100\".")
    elif loose:
        notes.append(f"❌ No amount for {', '.join(loose)}, so it was not saved.\nSend it with "
                     f"the amount, e.g. \"{loose[0]} 100\".")
    return "\n\n".join(notes)


def _search(ctx: Context, args: dict, message: str = "") -> dict:
    dates = {}
    for key in ("date_from", "date_to"):
        raw = str(args.get(key) or "").strip()
        if raw:
            d = parse_date(raw)
            if d is None:
                return {"error": f"{key} '{raw}' is not DD-MM-YYYY. Call again with a valid date."}
            dates[key] = d
    if "date_from" in dates and "date_to" in dates and dates["date_from"] > dates["date_to"]:
        dates["date_from"], dates["date_to"] = dates["date_to"], dates["date_from"]

    payer = None
    if args.get("paid_by"):
        payer = members.match_name(ctx.people, args["paid_by"])
        if payer is None:
            return {"error": f"Unknown payer '{args['paid_by']}'. Use one of: "
                             f"{', '.join(m.name for m in ctx.people)}."}
    rows = expenses.search(
        ctx.session, paid_by=payer, category=args.get("category") or None,
        amount_above=_money(args.get("amount_above")), amount_below=_money(args.get("amount_below")),
        text=args.get("text") or None, date_from=dates.get("date_from"), date_to=dates.get("date_to"))
    total = expenses.total(rows)
    text = (args.get("text") or "").strip()
    if not rows and text:
        # Live bug 29-10: "kesar na daag ni dava no kharcho?" -> "No entries found." while a
        # ₹850 copper entry existed. The honest answer names what was searched, and the journal
        # offers the link from the problem to the product.
        reply = f"💰 No expense with \"{text}\" in its description or shop."
        hop = farm_tools.expense_hop(ctx, message)
        if hop:
            return {"text_reply": f"{reply}\n\n{hop[0]}", "buttons": hop[1], "count": 0,
                    "total": total}
        return {"text_reply": reply, "count": 0, "total": total}
    return {"text_reply": search_text(rows, total), "count": len(rows), "total": total}


# ── Replies (fixed templates: same text every time for the same data) ───────────────────────

def added_text(v: ExpenseView) -> str:
    return (f"✅ Logged #{v.id}: ₹{inr(v.amount)} for {v.description}, paid by {v.paid_by} "
            f"[{v.category}] — {fmt_date(v.date)}")


def undo_text(v: ExpenseView) -> str:
    return f"↩️ Undone: #{v.id} — ₹{inr(v.amount)} — {v.description} ({fmt_date(v.date)})"


def recent_text(rows: list[ExpenseView]) -> str:
    if not rows:
        return "No entries yet."
    return f"🕐 Last {entries(len(rows))}:\n\n" + "\n".join(r.line() for r in rows)


def search_text(rows: list[ExpenseView], total: Decimal) -> str:
    if not rows:
        return "No entries found."
    shown = rows[:MAX_ROWS]
    head = (f"📋 {entries(len(rows))}" + (f" (newest {MAX_ROWS} shown)" if len(rows) > MAX_ROWS else "")
            + ":\n\n")
    # Total is computed by code over ALL matches, not just the ones shown.
    return head + "\n".join(r.line() for r in shown) + f"\n\n💵 Total: ₹{inr(total)} ({entries(len(rows))})"


def _templates(runs: list[ToolRun]) -> str:
    # The same answer once: the model calling a tool three times must not show it three times.
    parts = list(dict.fromkeys(r.result["text_reply"] for r in runs
                               if r.ok and r.result.get("text_reply")))
    return "\n\n".join(parts)


# ── "How much was it?" ──────────────────────────────────────────────────────────────────────

def _ask_amount(original: str) -> str:
    return f"📝 \"{original}\" — how much was it?\nSend just the amount, e.g. 100 (within 2 minutes)."


def _follow_up(ctx: Context, waiting: pending.Pending | None, is_slot: bool, user_text: str,
               runs: list[ToolRun], reply: str) -> tuple[str, Buttons | None]:
    """The final reply and its buttons. A farm note with no plant is remembered, so a tapped
    plant button (or a typed "aamba") completes it."""
    s, chat, who = ctx.session, ctx.chat_id, ctx.sender.id
    if any(r.needs == "plant" for r in runs) and not is_slot:
        pending.drop(s, chat, who, pending.AMOUNT)
        pending.drop(s, chat, who, pending.TIME)
        unknown = next((r.result["unknown_plant"] for r in runs if r.result.get("unknown_plant")), "")
        pending.put(s, chat, who, pending.PLANT, {"message": user_text, "tries_left": MAX_TRIES,
                                                  "photo": ctx.photo, "source": ctx.source,
                                                  "unknown": unknown})
        admin = ctx.sender.role == "admin"
        ask = (farm_tools.unknown_plant_text(unknown, admin) if unknown else farm_tools.ASK_PLANT)
        return ("\n\n".join(p for p in (reply, ask) if p),
                farm_tools.plant_buttons(farm.plants(s), unknown if admin else ""))
    text = _follow_up_text(ctx, waiting, is_slot, user_text, runs, reply)
    still = waiting and waiting.kind == pending.PLANT and pending.get(s, chat, who, pending.PLANT)
    if still:
        unknown = still.payload.get("unknown", "") if ctx.sender.role == "admin" else ""
        return text, farm_tools.plant_buttons(farm.plants(s), unknown)
    return text, None


def _follow_up_text(ctx: Context, waiting: pending.Pending | None, is_slot: bool, user_text: str,
                    runs: list[ToolRun], reply: str) -> str:
    """Decides, in code, what happens to a waiting question after this message, and the final
    reply. Rules (same as the old bot): a saved expense clears it; a new request missing a piece
    replaces it; a reply that still does not complete it uses up one of three tries."""
    s, chat, who = ctx.session, ctx.chat_id, ctx.sender.id
    joined = lambda *parts: "\n\n".join(p for p in parts if p)  # noqa: E731
    needs = {r.needs: r for r in runs if r.needs}

    if any(r.name == tools.ADD_EXPENSE and r.ok for r in runs):
        pending.drop(s, chat, who, pending.AMOUNT)
        return reply

    if "amount" in needs and not is_slot:
        pending.drop(s, chat, who, pending.TIME)
        pending.put(s, chat, who, pending.AMOUNT, {"message": user_text, "tries_left": MAX_TRIES})
        # Keep whatever else this message produced (e.g. a balance asked in the same breath) —
        # the old bot replaced it with the question and the balance was lost.
        return joined(reply, _ask_amount(user_text))

    time_need = next((k for k in ("time", "ampm", "new_time") if k in needs), None)
    if time_need and not is_slot:
        pending.drop(s, chat, who, pending.AMOUNT)
        mode = {"time": "add", "ampm": "ampm", "new_time": "replace"}[time_need]
        pending.put(s, chat, who, pending.TIME, {"message": user_text, "tries_left": MAX_TRIES,
                                                 "mode": mode})
        if time_need == "time":
            ask = f"📅 \"{user_text}\" — what time?\nSend just the time, e.g. 5pm or 17:00 (within 2 minutes)."
        elif time_need == "ampm":
            h = needs["ampm"].result["needs_ampm"]
            ask = (f"🕐 \"{user_text}\" — {h} o'clock in the morning or the evening?\n"
                   f"Send \"{h}am\" / \"{h}pm\" or \"savare\" / \"saanje\" (within 2 minutes).")
        else:
            # The old bot asked "pick another time" but could not receive the answer. Now the
            # request is remembered, so a reply like "6pm" moves it.
            ask = (f"❌ {needs['new_time'].result['conflict']}\nSend another time (e.g. 6pm) within "
                   "2 minutes, or cancel that meeting first.")
        return joined(reply, ask)

    if "datetime" in needs:
        pending.drop(s, chat, who, pending.TIME)
        return joined(reply, "📅 When? Send the day and time together, e.g. \"meeting sunday 5pm\" "
                             "or \"kale saanje 6 vagye meeting\".")

    if "description" in needs:
        amt = inr(needs["description"].result["amount"])
        reply = joined(reply, f"❌ Nothing saved: what was ₹{amt} for?\n"
                              f"Send it with the item, e.g. \"{amt} tea\".")

    if not waiting:
        return reply

    noun = {pending.TIME: "time", pending.PLANT: "plant"}.get(waiting.kind, "amount")
    original = waiting.payload["message"]
    tries_left = int(waiting.payload.get("tries_left", MAX_TRIES)) - 1
    if tries_left <= 0:
        pending.drop(s, chat, who, waiting.kind)
        example, what = {
            "time": ("meeting sunday 5pm", "request"),
            "plant": ("aamba ma pan par fodla, neem spray", "note"),
        }.get(noun, (f"50 tea by {ctx.sender.name.split()[0]}", "entry"))
        note = (f"❌ No valid {noun} after {MAX_TRIES} tries, so \"{original}\" was not saved. "
                f"Send the full {what} again, e.g. {example}.")
        return note if is_slot else joined(reply, note)

    pending.put(s, chat, who, waiting.kind, {**waiting.payload, "tries_left": tries_left})
    left = f"{tries_left} {'try' if tries_left == 1 else 'tries'} left"
    example = {"time": "5pm", "plant": "aamba"}.get(noun, "100")
    if is_slot:
        return (f"❌ \"{user_text}\" is not a valid {noun} for \"{original}\".\n"
                f"Send just the {noun}, e.g. {example} ({left}).")
    return joined(reply, f"⏳ Still waiting for the {noun} for \"{original}\" — "
                         f"send just the {noun} ({left}).")


# ── Small parsing helpers ───────────────────────────────────────────────────────────────────

def _int(value: object, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _money(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return to_rupees(value)
    except (InvalidOperation, ValueError):
        return None

