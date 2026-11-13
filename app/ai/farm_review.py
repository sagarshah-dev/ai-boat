"""📋 Farm review: a supervisor and two helper agents (multi-agent), built with LangGraph.

Live baseline 13-11-2026: "Aa mahina nu farm review aapo" -> 'No farm note found ... for: review'.
One agent picks one tool, so a question that needs money AND farm work AND a comparison could
not be answered at all.

    gather (code) ──Send──► money helper (agent) ──┐
                  └─Send──► farm helper  (agent) ──┴► write (AI) ──► answer (code)

- gather: code reads the database ONCE, in this thread: the period's expenses (and the previous
  month's) and journal rows. These are the FACTS.
- helpers: each is an agent — its own prompt, its own read-only tools, its own tool loop — that
  decides what to look at and returns at most two short notes. `Send` runs both at the same time
  (LangGraph runs them in a thread pool). Their tools read only the facts dict, never the
  database: one SQLAlchemy session must not be used by two threads.
- write: one more model call turns the helpers' notes into at most three "salah" lines.
- answer: the numbers on screen are code's (from the facts). Every model line must use only
  numbers that are in the facts, or it is dropped. A failed model call drops its lines, never the
  review.

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
MAX_SALAH = 3

SPEC = ToolSpec(
    NAME,
    "A REVIEW / SUMMARY of a whole month that combines money AND farm work: totals, comparison "
    "with the month before, what worked, what is still pending. Use for 'aa mahina nu farm review "
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

def month_range(today: date, period: str) -> tuple[date, date, date, date]:
    """(start, end, compare-from, compare-to).

    The comparison covers the SAME days of the month before: on the 13th, 1–13 November is
    compared with 1–13 October, not with all of October (13 days against 31 would always look
    like a big drop). For a whole month the comparison is the whole month before."""
    first = today.replace(day=1)
    if period == "last_month":
        end = first - timedelta(days=1)
        start = end.replace(day=1)
    else:
        start, end = first, today
    p_last = start - timedelta(days=1)
    p_start = p_last.replace(day=1)
    if period == "last_month":
        return start, end, p_start, p_last
    return start, end, p_start, min(p_last, p_start + (end - start))


def _money(rows) -> dict:
    total = sum((v.amount for v in rows), Decimal(0))
    by_cat: dict[str, Decimal] = {}
    by_payer: dict[str, Decimal] = {}
    for v in rows:
        by_cat[v.category] = by_cat.get(v.category, Decimal(0)) + v.amount
        by_payer[v.paid_by] = by_payer.get(v.paid_by, Decimal(0)) + v.amount
    share = {c: round(a * 100 / total) for c, a in by_cat.items()} if total else {}
    order = sorted(by_cat, key=lambda c: -by_cat[c])
    return {"total": int(total), "count": len(rows),
            "by_category": {c: int(by_cat[c]) for c in order},
            "share_pct": {c: share[c] for c in order},
            "by_payer": {p: int(a) for p, a in sorted(by_payer.items(), key=lambda x: -x[1])},
            "biggest": [{"id": v.id, "amount": int(v.amount), "item": v.description,
                         "date": fmt_date(v.date)}
                        for v in sorted(rows, key=lambda v: (-v.amount, -v.id))[:3]]}


def _farm(rows, planned) -> dict:
    work: dict[str, int] = {}
    buckets: dict[str, list] = {"worked": [], "partly": [], "did_not_work": [],
                                "waiting_for_result": []}
    for e in rows:
        if e.activity == "planned":
            continue
        label = farm.ACTIVITY_LABEL.get(e.activity, e.activity)
        work[label] = work.get(label, 0) + 1
        if e.activity in farm.TREATMENTS:
            item = {"id": f"J-{e.id}", "product": e.product.strip() or label,
                    "problem": e.problem.strip(), "date": fmt_date(e.entry_date)}
            outcome = outcome_of(e.result)
            if not e.result.strip():
                buckets["waiting_for_result"].append(item)
            elif outcome == "worked":
                buckets["worked"].append(item)
            elif outcome == "not_worked":
                buckets["did_not_work"].append(item)
            else:                       # "partly", or a result written in the owner's own words
                buckets["partly"].append(item)
    work = dict(sorted(work.items(), key=lambda kv: (-kv[1], kv[0])))     # most done first
    return {"notes": sum(work.values()), "work": work, **buckets,
            "planned_not_done": [{"id": f"J-{e.id}", "product": e.product.strip() or "plan",
                                  "date": fmt_date(e.entry_date)} for e in planned]}


def _pct(now: int, before: int) -> int | None:
    """No percentage against zero: "+∞%" says nothing."""
    return round((now - before) * 100 / before) if before else None


def _sum(rows) -> int:
    return int(sum((v.amount for v in rows), Decimal(0)))


def gather_facts(session, today: date, period: str) -> dict:
    start, end, c_start, c_end = month_range(today, period)
    now = expenses.search(session, date_from=start, date_to=end)
    before = expenses.search(session, date_from=c_start, date_to=c_end)
    notes = farm.query(session, date_from=start, date_to=end)
    planned = farm.query(session, activity="planned")
    month_before = before if period == "last_month" else expenses.search(
        session, date_from=c_start, date_to=start - timedelta(days=1))
    return {"period": f"{fmt_date(start)} to {fmt_date(end)}",
            "month": start.strftime("%B %Y"), "previous_month": c_start.strftime("%B %Y"),
            "money": _money(now),
            "compared_with": {"period": f"{fmt_date(c_start)} to {fmt_date(c_end)}",
                              "total": _sum(before), "count": len(before),
                              "change_pct": _pct(_sum(now), _sum(before))},
            "previous_month_total": _sum(month_before),
            "farm": _farm(notes, planned)}


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


def grounded(lines: list[str], facts: dict) -> list[str]:
    allowed = _numbers(json.dumps(facts, default=str))
    kept = [ln for ln in lines if _numbers(ln) <= allowed and not _BANNED.search(ln)]
    if len(kept) < len(lines):
        log.info("review line dropped: number not in the facts, or a banned word",
                 extra={"fields": {"dropped": len(lines) - len(kept)}})
    return kept


def _lines(text: str, limit: int) -> list[str]:
    out = [re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", ln).strip() for ln in (text or "").splitlines()]
    return [ln[:200] for ln in out if ln][:limit]


# ── The helpers: two agents, each with its own prompt and read-only tools ───────────────────

_NO_ARGS = {"type": "OBJECT", "properties": {}}
_PERIOD_ARG = {"type": "OBJECT", "properties": {"which": {
    "type": "STRING", "enum": ["this", "previous"], "description": "this period or the month before"}}}

HELPERS: dict[str, dict[str, Any]] = {
    "money": {
        "system": (
            "You are the MONEY helper of a monthly farm review for three co-owners. Use your tools "
            "to look at the month's expenses, then reply with at most 2 short lines the owners "
            "would want to know (a big change, where most money went). Copy every number from a "
            "tool result; never compute a new number. Plain text, one fact per line, English. "
            "Never use the words owe / owes."),
        "tools": [
            ToolSpec("money_totals", "Total, count, per category (with % share) and per payer.",
                     _PERIOD_ARG),
            ToolSpec("biggest_expenses", "The three biggest expenses of the period.", _NO_ARGS),
        ],
    },
    "farm": {
        "system": (
            "You are the FARM helper of a monthly farm review. Use your tools to look at the "
            "month's farm work, then reply with at most 2 short lines the owners would want to "
            "know (what worked, what is still pending). Copy every number and note id (J-…) from "
            "a tool result. Never suggest a medicine or a dose. Plain text, one fact per line, "
            "English."),
        "tools": [
            ToolSpec("farm_work", "How many notes of each kind of work this period.", _NO_ARGS),
            ToolSpec("treatment_results", "Treatments this period: worked, partly, did not "
                                          "work, waiting for a result.", _NO_ARGS),
            ToolSpec("plans_not_done", "Saved spray plans that are not done yet.", _NO_ARGS),
        ],
    },
}


def helper_tool(facts: dict, name: str, args: dict) -> dict:
    """Read-only, from the facts gathered by code. No database here: this runs in a worker
    thread while the other helper runs in another."""
    m, f = facts["money"], facts["farm"]
    if name == "money_totals":
        if str(args.get("which") or "this") == "previous":
            return {**facts["compared_with"], "whole_previous_month": facts["previous_month_total"]}
        return {"period": facts["period"], **{k: m[k] for k in
                                              ("total", "count", "by_category", "share_pct", "by_payer")}}
    if name == "biggest_expenses":
        return {"biggest": m["biggest"]}
    if name == "farm_work":
        return {"period": facts["period"], "notes": f["notes"], "work": f["work"]}
    if name == "treatment_results":
        return {k: f[k] for k in ("worked", "partly", "did_not_work", "waiting_for_result")}
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
    if f["money"]["count"] or f["compared_with"]["count"]:
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
    "You write the 'salah' (advice) part of a monthly farm review for three co-owners. You get "
    "FACTS (all numbers) and NOTES from a money helper and a farm helper. Write at most 3 short "
    "lines, each starting with '- '. Join money and farm where it helps (the cost of work that "
    "worked, money spent on something still pending). Use only numbers that appear in FACTS. "
    "Never suggest a medicine or a dose. Never use the words owe / owes. English.")


def write(state: Review, runtime: Runtime[Deps]) -> dict:
    notes = [ln for n in state.get("notes", []) for ln in n["lines"]]
    if not notes:
        return {"salah": [], "path": ["write (nothing to join)"]}
    prompt = (f"FACTS:\n{json.dumps(state['facts'], ensure_ascii=False, default=str)}\n\n"
              "NOTES:\n" + "\n".join(f"- {ln}" for ln in notes))
    try:
        text = runtime.context.ctx.llm.complete(WRITER, prompt, temperature=0.2, budget_s=30)
    except LlmError as err:
        log.warning("review writer failed", extra={"fields": {"kind": err.kind}})
        return {"salah": [], "path": ["write (failed)"]}
    return {"salah": grounded(_lines(text, MAX_SALAH), state["facts"]), "path": ["write"]}


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

def _change(pct: int | None) -> str:
    return "" if pct is None else f" ({'+' if pct >= 0 else ''}{pct}%)"


def _items(rows: list[dict]) -> str:
    return ", ".join(f"{t['product']} ({t['id']})" for t in rows)


def _money_lines(f: dict) -> list[str]:
    m, c = f["money"], f["compared_with"]
    if not (m["count"] or c["count"]):
        return ["💰 Money: no expenses in this period or the one before."]
    out = [f"💰 Money: ₹{inr(m['total'])} in {entries(m['count'])}",
           f"• Same days of {f['previous_month']} ({c['period']}): ₹{inr(c['total'])}"
           f"{_change(c['change_pct'])}"]
    if f["previous_month_total"] != c["total"]:
        out.append(f"• Whole {f['previous_month']}: ₹{inr(f['previous_month_total'])}")
    if m["count"]:
        out.append("• By category: " + " · ".join(
            f"{k} ₹{inr(a)} ({m['share_pct'][k]}%)" for k, a in m["by_category"].items()))
        out.append("• Paid by: " + " · ".join(f"{p} ₹{inr(a)}" for p, a in m["by_payer"].items()))
        out.append("• Biggest: " + " · ".join(
            f"#{b['id']} ₹{inr(b['amount'])} {b['item']}" for b in m["biggest"]))
    return out


def _farm_lines(w: dict) -> list[str]:
    if not (w["notes"] or w["planned_not_done"]):
        return ["🌱 Farm: no farm notes in this period."]
    out = ["🌱 Farm work: " + (" · ".join(f"{k} {n}" for k, n in w["work"].items())
                              or "no notes in this period")]
    for key, label in (("worked", "✅ Worked"), ("partly", "➖ Partly"),
                       ("did_not_work", "❌ Did not work"),
                       ("waiting_for_result", "⏳ Waiting for result"),
                       ("planned_not_done", "📝 Planned, not done")):
        if w[key]:
            out.append(f"• {label}: {_items(w[key])}")
    return out


def review_text(state: Review) -> str:
    f = state["facts"]
    out = [f"📋 Farm review — {f['month']} ({f['period']})", "", *_money_lines(f), "",
           *_farm_lines(f["farm"])]
    if state.get("salah"):
        out += ["", "💡 Salah:", *[f"- {s}" for s in state["salah"]]]
    if state.get("failed"):
        out += ["", "ℹ️ The AI part could not run just now; the numbers above are complete."]
    helpers = [p for p in state.get("path", []) if "helper" in p]
    if helpers:
        out += ["", f"🔎 How: LangGraph supervisor → {' + '.join(helpers)} (AI agents with "
                    "read-only tools, in parallel) → writer (AI). Every number is from the "
                    "database; an AI line with any other number is dropped."]
    return "\n".join(out)


def run(ctx: "Context", args: dict) -> dict:
    """Called by the agent as a tool. Read-only. Returns a ready reply (template)."""
    period = str(args.get("period") or "this_month")
    period = period if period in PERIODS else "this_month"
    state = GRAPH.invoke({"period": period}, context=Deps(ctx))
    f = state["facts"]
    if not (f["money"]["count"] or f["compared_with"]["count"] or f["farm"]["notes"]
            or f["farm"]["planned_not_done"]):
        return {"text_reply": f"📋 Nothing to review for {f['month']}: no expenses and no farm "
                              "notes yet."}
    return {"text_reply": review_text(state), "review": {"path": state.get("path", [])}}
