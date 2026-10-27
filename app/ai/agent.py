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
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.ai import guards, proposals, tools
from app.core.formatting import fmt_date, inr, parse_date, to_rupees
from app.core.replies import Buttons
from app.db.models import Member
from app.integrations.gemini import LLM, LlmError, ToolCall
from app.services import balance, expenses, members, pending
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
        """'amount' / 'description' when a field is missing — answered by code, not the model."""
        if self.result.get("needs_amount"):
            return "amount"
        if self.result.get("needs_description"):
            return "description"
        return None


@dataclass
class Outcome:
    reply: str
    added: list[ExpenseView] = field(default_factory=list)
    runs: list[ToolRun] = field(default_factory=list)
    buttons: Buttons | None = None


# ── Entry point ─────────────────────────────────────────────────────────────────────────────

def handle(ctx: Context, text: str, *, batch: bool = False) -> Outcome:
    """One message. batch=True is one line of a multi-line message: no follow-up question is
    remembered, because several lines could not all wait for an answer at once."""
    user_text = text.strip()
    waiting = None if batch else pending.get(
        ctx.session, ctx.chat_id, ctx.sender.id, pending.AMOUNT)

    if not batch and not waiting and (guards.YES.match(user_text) or guards.NO.match(user_text)):
        # Never sent to the model: a bare "yes" with nothing to confirm has no safe meaning.
        return Outcome(NOTHING_WAITING)

    is_slot = bool(waiting) and bool(
        guards.amount_only_pattern(ctx.people_words).match(user_text))
    # "tea by sagar" (waiting) + "100" -> "tea by sagar 100", then the normal path: every gate
    # applies to the glued text exactly as if it had been typed in one go.
    message = f"{waiting.payload['message']} {user_text}" if is_slot else user_text

    runs, reply = _run_model(ctx, message)
    added = [r.result["expense"] for r in runs if r.name == tools.ADD_EXPENSE and r.ok
             and "expense" in r.result]
    if batch:
        return Outcome(reply, added, runs)

    proposal = _merge_proposals([r.result["proposal"] for r in runs if r.ok and r.result.get("proposal")])
    if proposal:
        proposals.store(ctx.session, ctx.chat_id, ctx.sender, proposal)
        # The preview must be shown word for word: the user is about to say "yes" to exactly it.
        reply = "\n\n".join(p for p in (reply, proposals.preview(proposal)) if p)
        return Outcome(reply, added, runs, proposals.buttons(proposal))
    reply = _follow_up(ctx, waiting, is_slot, user_text, runs, reply)
    return Outcome(reply, added, runs)


def _merge_proposals(found: list[dict]) -> dict | None:
    """One "yes" confirms one thing. Several expenses heard in one voice note become one list;
    otherwise the first proposal is kept and the rest are left for the user to send again."""
    if not found:
        return None
    items = [it for p in found if p["kind"] == "expenses" for it in p["items"]]
    if items and all(p["kind"] == "expenses" for p in found):
        return proposals.expenses_proposal(items, found[0].get("source", "voice"))
    return found[0]


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
        "no amount in digits, call add_expense with amount 0 — the system will ask for it. "
        "If a tool returns an error, explain it simply and ask the user what is needed."
    )


def _run_model(ctx: Context, message: str) -> tuple[list[ToolRun], str]:
    runs: list[ToolRun] = []
    done_writes: set[str] = set()
    try:
        chat = ctx.llm.start(system_prompt(ctx),
                             tools.expense_tools([m.name for m in ctx.people]), LLM_BUDGET_S)
        turn = chat.send_user(message)
        for round_no in range(1, MAX_ROUNDS + 1):
            if not turn.calls:
                return runs, turn.text or "Sorry, I couldn't complete that. Please try again."
            round_runs = [_run_tool(ctx, call, message, done_writes) for call in turn.calls]
            runs += round_runs
            if not _unresolved_errors(runs):
                # Everything left is a template or a missing field the code asks about itself:
                # no second model call — cheaper, faster, and the numbers can't be reworded.
                return runs, _templates(runs)
            if round_no == MAX_ROUNDS:
                break   # don't pay for a model answer we would not use
            turn = chat.send_tool_results([(r.name, _for_model(r.result)) for r in round_runs])
        log.warning("tool loop hit the round limit")
        return runs, _templates(runs) or "Sorry, I couldn't complete that. Please try again."
    except LlmError as err:
        log.warning("llm failed", extra={"fields": {"kind": err.kind}})
        wrote = [r for r in runs if r.ok and (r.result.get("expense") or r.result.get("deleted"))]
        if any(r.ok and r.result.get("proposal") for r in runs):
            return runs, _templates(runs)   # the preview is added by the caller; nothing was saved
        if wrote:
            # The save happened; the model only failed to word the reply. Say exactly what was saved.
            return runs, _templates(wrote) + "\n(The change above is saved.)"
        return runs, LLM_ERROR_TEXT.get(err.kind, LLM_ERROR_TEXT["unknown"]) + STILL_WORKS


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


def _unresolved_errors(runs: list[ToolRun]) -> list[ToolRun]:
    """Errors the model must see and fix (bad date, unknown payer). A missing amount or item is
    NOT one — code asks the user directly. An error is resolved once a later call of the same
    tool succeeded (the model corrected itself)."""
    ok_names = {r.name for r in runs if r.ok}
    return [r for r in runs
            if r.result.get("error") and not r.needs and r.name not in ok_names]


def _for_model(result: dict) -> dict:
    """What the model gets back: the facts, not our Python objects."""
    return {k: v for k, v in result.items() if k not in ("expense", "text_reply")}


# ── Tools ───────────────────────────────────────────────────────────────────────────────────

def execute_tool(ctx: Context, name: str, args: dict[str, Any], message: str) -> dict:
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
        return _search(ctx, args)
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

    payer = (members.match_name(ctx.people, args.get("paid_by"))
             or members.match_name(ctx.people, ctx.sender.name))
    if payer is None:
        names = ", ".join(m.name for m in ctx.people)
        return {"error": f"Unknown payer. Nothing was saved. Ask the user who paid: {names}."}

    category = str(args.get("category") or "")
    merchant = str(args.get("merchant") or "")
    if ctx.confirm_writes:
        return {"proposal": proposals.expenses_proposal([proposals.expense_item(
            amount=amount, description=description, paid_by=payer, category=category,
            merchant=merchant, on=expense_date)], "voice"), "amount": amount, "paid_by": payer.name}
    view = expenses.add(ctx.session, amount=amount, description=description, paid_by=payer,
                        entered_by=ctx.sender, expense_date=expense_date,
                        category=category, merchant=merchant)
    return {"expense": view, "text_reply": added_text(view), "id": view.id, "amount": view.amount,
            "paid_by": view.paid_by, "date": fmt_date(view.date)}


def _search(ctx: Context, args: dict) -> dict:
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
    return f"🕐 Last {len(rows)} entries:\n\n" + "\n".join(r.line() for r in rows)


def search_text(rows: list[ExpenseView], total: Decimal) -> str:
    if not rows:
        return "No entries found."
    shown = rows[:MAX_ROWS]
    head = (f"📋 {len(rows)} entries" + (f" (newest {MAX_ROWS} shown)" if len(rows) > MAX_ROWS else "")
            + ":\n\n")
    # Total is computed by code over ALL matches, not just the ones shown.
    return head + "\n".join(r.line() for r in shown) + f"\n\n💵 Total: ₹{inr(total)} ({len(rows)} entries)"


def _templates(runs: list[ToolRun]) -> str:
    parts = [r.result["text_reply"] for r in runs if r.ok and r.result.get("text_reply")]
    return "\n\n".join(parts)


# ── "How much was it?" ──────────────────────────────────────────────────────────────────────

def _ask_amount(original: str) -> str:
    return f"📝 \"{original}\" — how much was it?\nSend just the amount, e.g. 100 (within 2 minutes)."


def _follow_up(ctx: Context, waiting: pending.Pending | None, is_slot: bool, user_text: str,
               runs: list[ToolRun], reply: str) -> str:
    """Decides, in code, what happens to a waiting entry after this message, and the final reply.
    Rules (same as the old bot): a saved expense clears it; a new entry missing its amount
    replaces it; a reply that still has no valid amount uses up one of three tries."""
    s, chat, who = ctx.session, ctx.chat_id, ctx.sender.id
    joined = lambda *parts: "\n\n".join(p for p in parts if p)  # noqa: E731

    if any(r.name == tools.ADD_EXPENSE and r.ok for r in runs):
        pending.drop(s, chat, who, pending.AMOUNT)
        return reply

    if any(r.needs == "amount" for r in runs) and not is_slot:
        pending.put(s, chat, who, pending.AMOUNT, {"message": user_text, "tries_left": MAX_TRIES})
        # Keep whatever else this message produced (e.g. a balance asked in the same breath) —
        # the old bot replaced it with the question and the balance was lost.
        return joined(reply, _ask_amount(user_text))

    item_missing = next((r.result for r in runs if r.needs == "description"), None)
    if item_missing:
        amt = inr(item_missing["amount"])
        reply = joined(reply, f"❌ Nothing saved: what was ₹{amt} for?\n"
                              f"Send it with the item, e.g. \"{amt} tea\".")

    if not waiting:
        return reply

    original = waiting.payload["message"]
    tries_left = int(waiting.payload.get("tries_left", MAX_TRIES)) - 1
    if tries_left <= 0:
        pending.drop(s, chat, who, pending.AMOUNT)
        note = (f"❌ No valid amount after {MAX_TRIES} tries, so \"{original}\" was not saved. "
                f"Send the full entry again, e.g. 50 tea by {ctx.sender.name.split()[0]}.")
        return note if is_slot else joined(reply, note)

    pending.put(s, chat, who, pending.AMOUNT, {"message": original, "tries_left": tries_left})
    left = f"{tries_left} {'try' if tries_left == 1 else 'tries'} left"
    if is_slot:
        return (f"❌ \"{user_text}\" is not a valid amount for \"{original}\".\n"
                f"Send just the amount, e.g. 100 ({left}).")
    return joined(reply, f"⏳ Still waiting for the amount for \"{original}\" — "
                         f"send just the amount ({left}).")


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

