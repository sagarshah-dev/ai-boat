"""📋 Farm review: a supervisor and two helper agents (multi-agent), built with LangGraph.

Live baseline 13-11-2026: "Aa mahina nu farm review aapo" -> 'No farm note found ... for: review'.
One agent picks one tool, so a question that needs money AND farm work could not be answered.

    gather (code) ──Send──► money helper (agent) ──┐
                  └─Send──► farm helper  (agent) ──┴► write (AI) ──► answer (code)

- gather: code reads the database ONCE, in this thread. These are the FACTS, and they include
  what code can decide alone: reminders (a result pending 7+ days, a plan saved twice) and
  LINKS (an expense whose item names a treatment's product: "Copper" ₹500 ↔ J-3 copper).
- helpers: each is an agent — its own prompt, its own read-only tools, its own loop. `Send` runs
  both at the same time. Their tools read only the facts dict, never the database: one
  SQLAlchemy session must not be used by two threads.
- write: at most 2 AI lines. A line may join money and farm only through a LINK code found.
- answer: everything on screen except those AI lines is code's.

Live 16-11-2026 (first version): the writer joined "₹1,290 agri medicine" to J-2 — both real,
the link invented; "+8826%" against a month with no data yet; an October review showed a
November plan; lines repeated numbers ("Sagar spent 2605 out of 3535"). The reply was also too
long. Now: no % or comparison, reminders by code, joins only through LINKS, a short reply.

Nothing is written: no checkpointer, no ✅, no pending row.
"""
from __future__ import annotations

import json
import logging
import operator
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from app.core.formatting import entries, fmt_date, inr
from app.core.text import norm, words
from app.integrations.gemini import LlmError, ToolSpec
from app.ai.farm_salah import outcome_of
from app.services import expenses, farm

if TYPE_CHECKING:
    from app.ai.agent import Context

log = logging.getLogger(__name__)

NAME = "farm_review"
PERIODS = ("this_month", "last_month")
HELPER_ROUNDS = 4                 # a helper that keeps calling tools is stopped here
HELPER_BUDGET_S = 40
MAX_NOTES = 2                     # lines per helper
MAX_AI_LINES = 2                  # the writer's lines, after code's reminders
MAX_REMINDERS = 3
RESULT_DUE_DAYS = 7               # same as the 7-day follow-up of a treatment

SPEC = ToolSpec(
    NAME,
    "A REVIEW / SUMMARY of a whole month that combines money AND farm work: totals, what "
    "worked, what is still pending. Use for 'aa mahina nu farm review "
    "aapo', 'gaya mahina no saar', 'monthly summary of the farm'. NOT for one lookup, the balance "
    "('hisab'), or a list of expenses.",
    {"type": "OBJECT", "properties": {
        "period": {"type": "STRING", "enum": list(PERIODS),
                   "description": "this_month (default) or last_month"}}},
)


RULES = (
    "FARM REVIEW: for a review / summary of a whole month (money and farm work together) call "
    "farm_review once; it writes its own reply. 'gaya mahina' / 'last month' = last_month. "
)


# ── The facts (code, one thread, one read of the database) ──────────────────────────────────

def month_range(today: date, period: str) -> tuple[date, date]:
    first = today.replace(day=1)
    if period == "last_month":
        end = first - timedelta(days=1)
        return end.replace(day=1), end
    return first, today


def _money(rows) -> dict:
    by_cat: dict[str, Decimal] = {}
    by_payer: dict[str, Decimal] = {}
    for v in rows:
        by_cat[v.category] = by_cat.get(v.category, Decimal(0)) + v.amount
        by_payer[v.paid_by] = by_payer.get(v.paid_by, Decimal(0)) + v.amount
    return {"total": int(sum((v.amount for v in rows), Decimal(0))), "count": len(rows),
            "by_category": {c: int(a) for c, a in sorted(by_cat.items(), key=lambda x: -x[1])},
            "by_payer": {p: int(a) for p, a in sorted(by_payer.items(), key=lambda x: -x[1])},
            "biggest": [{"id": v.id, "amount": int(v.amount), "item": v.description}
                        for v in sorted(rows, key=lambda v: (-v.amount, -v.id))[:3]]}


def _item(e) -> dict:
    return {"id": f"J-{e.id}", "product": e.product.strip() or farm.ACTIVITY_LABEL.get(
        e.activity, e.activity), "date": fmt_date(e.entry_date)}


def _farm(rows, planned) -> dict:
    work: dict[str, int] = {}
    buckets: dict[str, list] = {"worked": [], "partly": [], "did_not_work": [],
                                "result_pending": []}
    for e in rows:
        if e.activity == "planned":
            continue
        label = farm.ACTIVITY_LABEL.get(e.activity, e.activity)
        work[label] = work.get(label, 0) + 1
        if e.activity in farm.TREATMENTS:
            outcome = outcome_of(e.result)
            key = ("result_pending" if not e.result.strip() else "worked" if outcome == "worked"
                   else "did_not_work" if outcome == "not_worked"
                   else "partly")          # "partly", or a result in the owner's own words
            buckets[key].append(_item(e))
    work = dict(sorted(work.items(), key=lambda kv: (-kv[1], kv[0])))     # most done first
    return {"notes": sum(work.values()), "work": work, **buckets,
            "planned_not_done": [_item(e) for e in planned]}


def _key_words(text: str) -> set[str]:
    """Words that can name a product: 'copper oxychloride' -> {copper, oxychloride}."""
    return {w for w in words(text) if len(w) >= 4}


def links(spent, treatments) -> list[dict]:
    """An expense and a treatment are linked when the expense's item names the product.

    Code decides links; the writer may only use these. Live 16-11-2026 it joined '₹1,290 agri
    medicine' to J-2 by itself: both real, the link invented. A shared date is NOT a link —
    tea is bought on spray days too."""
    out = []
    for v in spent:
        names = _key_words(v.description)
        for e in treatments:
            if e.product.strip() and names & _key_words(e.product):
                out.append({"expense": f"#{v.id}", "amount": int(v.amount), "item": v.description,
                            "note": f"J-{e.id}", "product": e.product.strip(),
                            "result": outcome_of(e.result) or ("pending" if not e.result.strip()
                                                               else "partly")})
    return out


def reminders(today: date, open_treatments, planned) -> list[str]:
    """What the owners should do, decided by code: rules, so they are right every time."""
    out = []
    due = sorted((e for e in open_treatments
                  if (today - e.entry_date).days >= RESULT_DUE_DAYS), key=lambda e: e.entry_date)
    for e in due:
        out.append(f"Check the result of {_item(e)['product']} (J-{e.id}): done "
                   f"{(today - e.entry_date).days} days ago, no result yet.")
    same: dict[str, list] = {}
    for e in planned:
        same.setdefault(norm(e.product) or "plan", []).append(e)
    for group in same.values():
        if len(group) > 1:
            group = sorted(group, key=lambda e: e.id)
            ids = ", ".join(f"J-{e.id}" for e in group)
            out.append(f"{_item(group[0])['product']} is planned {len(group)} times ({ids}): "
                       "is one extra?")
    return out[:MAX_REMINDERS]


def gather_facts(session, today: date, period: str) -> dict:
    start, end = month_range(today, period)
    spent = expenses.search(session, date_from=start, date_to=end)
    notes = farm.query(session, date_from=start, date_to=end)
    # A plan stays pending until it is done, whatever month it was saved in — but a review of
    # October does not show a plan saved in November.
    planned = farm.query(session, activity="planned", date_to=end)
    treatments = [e for e in farm.query(session, date_to=end) if e.activity in farm.TREATMENTS]
    open_ones = [e for e in treatments if not e.result.strip()
                 and e.entry_date >= today - timedelta(days=90)]
    return {"period": f"{fmt_date(start)} to {fmt_date(end)}", "month": start.strftime("%B %Y"),
            "money": _money(spent), "farm": _farm(notes, planned),
            "reminders": reminders(today, open_ones, planned),
            "links": links(spent, treatments)}


# ── Grounding: a model line may only use numbers that are in the facts ──────────────────────

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """"12,500" and "12500" are the same number; so are "01" (in a date) and "1"."""
    out = set()
    for n in _NUM.findall(text):
        n = n.replace(",", "").rstrip(".")
        whole, _, frac = n.partition(".")
        out.add((whole.lstrip("0") or "0") + (f".{frac}" if frac.strip("0") else ""))
    return out


_BANNED = re.compile(r"\bowe[sd]?\b|\bowing\b", re.I)    # house rule: "pending" / "gets back"


_NOTE_ID = re.compile(r"\bJ-?\s?(\d+)\b", re.I)
_RUPEES = re.compile(r"(?:₹|rs\.?\s?|inr\s?)(\d[\d,]*)", re.I)


def _linked(line: str, facts: dict) -> bool:
    """A line that names a note AND an amount joins money to farm work: only a LINK code found
    may do that."""
    notes = {f"J-{n}" for n in _NOTE_ID.findall(line)}
    amounts = {int(a.replace(",", "")) for a in _RUPEES.findall(line)}
    if not (notes and amounts):
        return True
    return any(k["note"] in notes and k["amount"] in amounts for k in facts.get("links", []))


def grounded(lines: list[str], facts: dict) -> list[str]:
    allowed = _numbers(json.dumps(facts, default=str))
    kept = [ln for ln in lines if _numbers(ln) <= allowed and not _BANNED.search(ln)
            and _linked(ln, facts)]
    if len(kept) < len(lines):
        log.info("review line dropped: unknown number, invented link or banned word",
                 extra={"fields": {"dropped": len(lines) - len(kept)}})
    return kept


def _lines(text: str, limit: int) -> list[str]:
    out = [re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", ln).strip() for ln in (text or "").splitlines()]
    return [ln[:200] for ln in out if ln][:limit]


# ── The helpers: two agents, each with its own prompt and read-only tools ───────────────────

_NO_ARGS = {"type": "OBJECT", "properties": {}}

HELPERS: dict[str, dict[str, Any]] = {
    "money": {
        "system": (
            "You are the MONEY helper of a monthly farm review for three co-owners. Use your tools "
            "to look at the month's expenses, then reply with at most 2 short lines the owners "
            "would want to know (where most money went, a single big expense). Copy every number "
            "from a tool result; never compute a new number or a percentage. Write amounts as "
            "₹1,785. Plain text, one fact per line, English. Never use the words owe / owes."),
        "tools": [
            ToolSpec("money_totals", "Total, count, per category and per payer.", _NO_ARGS),
            ToolSpec("biggest_expenses", "The three biggest expenses of the period.", _NO_ARGS),
        ],
    },
    "farm": {
        "system": (
            "You are the FARM helper of a monthly farm review. Use your tools to look at the "
            "month's farm work, then reply with at most 2 short lines the owners would want to "
            "know (what worked, what did not). Copy every number and note id (J-…) from a tool "
            "result. Never suggest a medicine or a dose. Plain text, one fact per line, English."),
        "tools": [
            ToolSpec("farm_work", "How many notes of each kind of work this period.", _NO_ARGS),
            ToolSpec("treatment_results", "Treatments this period: worked, partly, did not "
                                          "work, result pending.", _NO_ARGS),
            ToolSpec("plans_not_done", "Saved spray plans that are not done yet.", _NO_ARGS),
        ],
    },
}


def helper_tool(facts: dict, name: str, args: dict) -> dict:
    """Read-only, from the facts gathered by code. No database here: this runs in a worker
    thread while the other helper runs in another."""
    m, f = facts["money"], facts["farm"]
    if name == "money_totals":
        return {"period": facts["period"], **{k: m[k] for k in
                                              ("total", "count", "by_category", "by_payer")}}
    if name == "biggest_expenses":
        return {"biggest": m["biggest"]}
    if name == "farm_work":
        return {"period": facts["period"], "notes": f["notes"], "work": f["work"]}
    if name == "treatment_results":
        return {k: f[k] for k in ("worked", "partly", "did_not_work", "result_pending")}
    if name == "plans_not_done":
        return {"planned_not_done": f["planned_not_done"]}
    return {"error": f"Unknown tool {name}"}


def run_helper(llm, who: str, facts: dict) -> list[str]:
    """One agent: its own chat, its own tools, its own loop. Returns its notes (grounded)."""
    spec = HELPERS[who]
    chat = llm.start(spec["system"], spec["tools"], HELPER_BUDGET_S)
    turn = chat.send_user(f"Review for {facts['month']} ({facts['period']}).")
    for _ in range(HELPER_ROUNDS):
        if not turn.calls:
            return grounded(_lines(turn.text, MAX_NOTES), facts)
        turn = chat.send_tool_results([(c.name, helper_tool(facts, c.name, c.args or {}))
                                       for c in turn.calls])
    log.warning("review helper stopped after max rounds", extra={"fields": {"helper": who}})
    return []


# ── The graph ────────────────────────────────────────────────────────────────────────────────

class Review(TypedDict, total=False):
    period: str
    facts: dict
    # Both helpers write here in the same step: a reducer joins their lists instead of the
    # second one overwriting the first.
    notes: Annotated[list, operator.add]
    failed: Annotated[list, operator.add]          # helpers whose model call failed
    salah: list[str]
    path: Annotated[list, operator.add]


class HelperInput(TypedDict):
    who: str
    facts: dict


@dataclass
class Deps:
    ctx: "Context"


def gather(state: Review, runtime: Runtime[Deps]) -> dict:
    ctx = runtime.context.ctx
    return {"facts": gather_facts(ctx.session, ctx.today, state["period"]), "path": ["gather"]}


def fan_out(state: Review) -> list[Send] | str:
    """Only helpers with something to look at are started: no data, no model call."""
    f = state["facts"]
    sends = []
    if f["money"]["count"]:
        sends.append(Send("helper", {"who": "money", "facts": f}))
    if f["farm"]["notes"] or f["farm"]["planned_not_done"]:
        sends.append(Send("helper", {"who": "farm", "facts": f}))
    return sends or "answer"


def helper(state: HelperInput, runtime: Runtime[Deps]) -> dict:
    who = state["who"]
    try:
        lines = run_helper(runtime.context.ctx.llm, who, state["facts"])
    except LlmError as err:
        log.warning("review helper failed", extra={"fields": {"helper": who, "kind": err.kind}})
        return {"failed": [who], "path": [f"{who} helper (failed)"]}
    return {"notes": [{"who": who, "lines": lines}], "path": [f"{who} helper"]}


WRITER = (
    "You write at most 2 short advice lines for a monthly farm review for three co-owners, each "
    "starting with '- '. You get NOTES from a money helper and a farm helper, LINKS (an expense "
    "and the treatment it paid for, found by code) and REMINDERS (already shown; do not repeat "
    "them). Join money and farm ONLY through a LINK (e.g. '₹500 on Copper (#57) paid for J-3 "
    "copper oxychloride, which worked'). Do not repeat a total or a category amount; say "
    "something the owners can act on or would not see at a glance. If there is nothing useful, "
    "write nothing. Write amounts as ₹1,785. Never suggest a medicine or a dose. Never use the "
    "words owe / owes. English.")


def write(state: Review, runtime: Runtime[Deps]) -> dict:
    notes = [ln for n in state.get("notes", []) for ln in n["lines"]]
    if not notes:
        return {"salah": [], "path": ["write (nothing to join)"]}
    f = state["facts"]
    prompt = ("NOTES:\n" + "\n".join(f"- {ln}" for ln in notes)
              + f"\n\nLINKS:\n{json.dumps(f['links'], ensure_ascii=False)}"
              + "\n\nREMINDERS:\n" + "\n".join(f"- {r}" for r in f["reminders"]))
    try:
        text = runtime.context.ctx.llm.complete(WRITER, prompt, temperature=0.2, budget_s=30)
    except LlmError as err:
        log.warning("review writer failed", extra={"fields": {"kind": err.kind}})
        return {"salah": [], "path": ["write (failed)"]}
    # Checked first, then capped: a dropped line must not use up a place.
    return {"salah": grounded(_lines(text, 6), f)[:MAX_AI_LINES], "path": ["write"]}


def answer(state: Review) -> dict:
    return {"path": ["answer"]}


def build():
    g = StateGraph(Review, context_schema=Deps)
    g.add_node("gather", gather)
    g.add_node("helper", helper)
    g.add_node("write", write)
    g.add_node("answer", answer)
    g.add_edge(START, "gather")
    g.add_conditional_edges("gather", fan_out, ["helper", "answer"])
    g.add_edge("helper", "write")       # waits for every helper started by fan_out
    g.add_edge("write", "answer")
    g.add_edge("answer", END)
    return g


GRAPH = build().compile()               # no checkpointer: nothing pauses, nothing is saved


# ── The reply (code) ─────────────────────────────────────────────────────────────────────────

def _items(rows: list[dict]) -> str:
    return ", ".join(f"{t['product']} ({t['id']})" for t in rows)


def review_text(state: Review) -> str:
    """Short on purpose (live 16-11-2026: the first version was too long to read on a phone)."""
    f = state["facts"]
    m, w = f["money"], f["farm"]
    start, _, end = f["period"].partition(" to ")
    out = [f"📋 {f['month']} review ({start[:5]} to {end[:5]})", ""]
    if m["count"]:
        out += [f"💰 ₹{inr(m['total'])} · {entries(m['count'])}",
                " · ".join(f"{k} ₹{inr(a)}" for k, a in m["by_category"].items()),
                "Paid: " + " · ".join(f"{p} ₹{inr(a)}" for p, a in m["by_payer"].items())]
    else:
        out.append("💰 No expenses.")
    out.append("")
    farm_lines = [" · ".join(f"{k} {n}" for k, n in w["work"].items())] if w["work"] else []
    for key, label in (("worked", "✅"), ("partly", "➖"), ("did_not_work", "❌"),
                       ("result_pending", "⏳ Result pending:"),
                       ("planned_not_done", "📝 Plan not done:")):
        if w[key]:
            farm_lines.append(f"{label} {_items(w[key])}")
    out += [f"🌱 {farm_lines[0]}", *farm_lines[1:]] if farm_lines else ["🌱 No farm notes."]
    salah = f["reminders"] + state.get("salah", [])
    if salah:
        out += ["", "💡 Salah", *[f"- {s}" for s in salah]]
    if state.get("failed"):
        out += ["", "ℹ️ The AI part could not run just now; the numbers above are complete."]
    helpers = [p.split()[0] for p in state.get("path", []) if p.endswith("helper")]
    if helpers:
        out += ["", f"🔎 {' + '.join(helpers)} helper{'s' if len(helpers) > 1 else ''} "
                    "(AI, in parallel) · numbers from the database"]
    return "\n".join(out)


def run(ctx: "Context", args: dict) -> dict:
    """Called by the agent as a tool. Read-only. Returns a ready reply (template)."""
    period = str(args.get("period") or "this_month")
    period = period if period in PERIODS else "this_month"
    state = GRAPH.invoke({"period": period}, context=Deps(ctx))
    f = state["facts"]
    if not (f["money"]["count"] or f["farm"]["notes"] or f["farm"]["planned_not_done"]
            or f["reminders"]):
        return {"text_reply": f"📋 Nothing to review for {f['month']}: no expenses and no farm "
                              "notes yet."}
    return {"text_reply": review_text(state), "review": {"path": state.get("path", [])}}
