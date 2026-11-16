"""The four read-only MCP tools: what they accept, what they return, and how they fail.

Every tool calls the same services the Telegram bot uses, so Claude and the bot can never show
different numbers for the same question. No business logic lives here — only input checks and
output shape.

Error contract (the same for every tool, so a client handles errors in one place):
    success: {"ok": true, ...data..., "warnings": [...]}
    failure: {"ok": false, "error": {"code": ..., "message": ..., "hint": ...}}
Codes: INVALID_INPUT (fix the arguments and call again), INTERNAL (our fault; retrying later
may help). Nothing found is NOT an error: it is ok with an empty list, so the model says
"no record" instead of "the tool broke". SEARCH_DEGRADED is a warning, not an error: results
came back, but only from exact-word search.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.config import Settings
from app.core.formatting import fmt_date, round_rupees
from app.db.models import ACTIVITIES
from app.integrations.gemini import LLM
from app.services import balance, expenses, farm, members, rag

VERSION = "1.0.0"
MAX_ROWS = 50
MAX_QUESTION = 300


class InvalidInput(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message, self.hint = message, hint


@dataclass
class Deps:
    session: Session
    llm: LLM
    settings: Settings
    today: date


def error(code: str, message: str, hint: str = "") -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


# ── Input checks (the model's arguments are never trusted) ──────────────────────────────────

def _limit(value: object, default: int) -> int:
    try:
        n = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        raise InvalidInput("limit must be a whole number", f"Use 1 to {MAX_ROWS}.")
    if not 1 <= n <= MAX_ROWS:
        raise InvalidInput(f"limit must be between 1 and {MAX_ROWS}")
    return n


def _period(deps: Deps, date_from: str, date_to: str) -> tuple[date | None, date | None, str]:
    df, dt, note, err = rag.resolve_dates(date_from or None, date_to or None, deps.today)
    if err:
        raise InvalidInput("dates must be DD-MM-YYYY", "Example: 01-10-2026. Leave empty for all dates.")
    return df, dt, note


def _plant(deps: Deps, name: str) -> tuple[str, str]:
    """(plant, variety) from a name or alias ('aamba', 'Kesar'); unknown -> list the real ones."""
    if not name.strip():
        return "", ""
    known = farm.plants(deps.session)
    match = farm.resolve_plants(known, name)
    plant = farm.single_plant(match)
    if not plant:
        raise InvalidInput(f"unknown plant '{name}'",
                           "Known plants: " + ", ".join(farm.plant_names(known)))
    variety = match.variety if match.variety not in ("", farm.ALL) else ""
    return plant, variety


def _amount(value: object, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise InvalidInput(f"{field} must be a number of rupees")
    if not d.is_finite() or d < 0:
        raise InvalidInput(f"{field} must be zero or more")
    return d


def _rupees(d: Decimal) -> int:
    return int(round_rupees(d))


# ── Tools ────────────────────────────────────────────────────────────────────────────────────

def search_farm_notes(deps: Deps, question: str, question_english: str = "", plant: str = "",
                      date_from: str = "", date_to: str = "") -> dict:
    """Same search as the bot's farm questions (meaning + exact words, score gate). Returns the
    notes themselves; the client's model writes the answer, so no second model call here."""
    q = (question or "").strip()
    if not q:
        raise InvalidInput("question is empty")
    if len(q) > MAX_QUESTION:
        raise InvalidInput(f"question is longer than {MAX_QUESTION} characters")
    f = rag.Filters()
    f.plant, f.variety = _plant(deps, plant)
    f.date_from, f.date_to, note = _period(deps, date_from, date_to)
    found = rag.retrieve(deps.session, deps.llm, q, f, deps.settings.rag_min_score,
                         alt=(question_english or "").strip()[:MAX_QUESTION])
    warnings = []
    if found.mode == "basic":
        warnings.append({"code": "SEARCH_DEGRADED",
                         "message": "Meaning search is unavailable; exact-word matches only."})
    if found.relaxed:
        warnings.append({"code": "FILTER_RELAXED", "message": found.relaxed})
    if note:
        warnings.append({"code": "DATE_ASSUMED", "message": note})
    notes = [{"id": h.id, "kind": "farmer_advice" if h.kind == "knowledge" else "journal",
              "date": h.meta.get("date_str", ""), "text": rag.original_text(h.text),
              "result": h.meta.get("result", "")} for h in found.hits]
    return {"ok": True, "notes": notes, "count": len(notes), "warnings": warnings}


def last_farm_activity(deps: Deps, plant: str = "", activity: str = "", limit: int = 5) -> dict:
    """Newest farm journal entries, filtered in code. 'planned' rows are Farm Salah plans that
    were not done yet; they are labelled so a plan is never reported as work done."""
    n = _limit(limit, 5)
    act = (activity or "").strip().lower()
    if act and act not in ACTIVITIES:
        raise InvalidInput(f"unknown activity '{activity}'", "Use one of: " + ", ".join(ACTIVITIES))
    p, variety = _plant(deps, plant)
    rows = farm.query(deps.session, plant=p or None, variety=variety or None, activity=act or None)
    entries = [{"id": f"J-{e.id}", "date": fmt_date(e.entry_date),
                "date_to": fmt_date(e.date_to) if e.date_to else "",
                "plants": farm.plant_label(farm.plants_of(e), e.variety),
                "activity": e.activity, "label": farm.ACTIVITY_LABEL.get(e.activity, e.activity),
                "product": e.product, "dose": e.dose, "problem": e.problem, "result": e.result,
                "note": e.note} for e in rows[:n]]
    return {"ok": True, "entries": entries, "count": len(entries), "total_matching": len(rows),
            "warnings": []}


def search_expenses(deps: Deps, text: str = "", category: str = "", paid_by: str = "",
                    date_from: str = "", date_to: str = "", amount_min: object = None,
                    amount_max: object = None, limit: int = MAX_ROWS) -> dict:
    """Expenses with every filter combined (AND). The total covers ALL matches, not only the
    rows returned, so 'how much on fuel this month' is right even past the row limit."""
    n = _limit(limit, MAX_ROWS)
    cat = (category or "").strip()
    if cat and cat.lower() not in {c.lower() for c in expenses.CATEGORIES}:
        # normalize_category would turn a typo into Misc and silently answer the wrong question
        raise InvalidInput(f"unknown category '{category}'",
                           "Use one of: " + ", ".join(expenses.CATEGORIES))
    payer = None
    if (paid_by or "").strip():
        people = members.split_members(deps.session)
        payer = members.match_name(people, paid_by)
        if payer is None:
            raise InvalidInput(f"unknown payer '{paid_by}'",
                               "Payers: " + ", ".join(m.name for m in people))
    lo, hi = _amount(amount_min, "amount_min"), _amount(amount_max, "amount_max")
    if lo is not None and hi is not None and lo > hi:
        raise InvalidInput("amount_min is larger than amount_max")
    df, dt, note = _period(deps, date_from, date_to)
    rows = expenses.search(deps.session, paid_by=payer, category=cat or None,
                           # search() is strict (> / <); the tool promises inclusive bounds
                           amount_above=lo - Decimal("0.01") if lo is not None else None,
                           amount_below=hi + Decimal("0.01") if hi is not None else None,
                           text=(text or "").strip() or None, date_from=df, date_to=dt)
    out = [{"id": e.id, "date": fmt_date(e.date), "amount": float(e.amount),
            "description": e.description, "category": e.category, "merchant": e.merchant,
            "paid_by": e.paid_by, "settled": e.settled} for e in rows[:n]]
    warnings = [{"code": "DATE_ASSUMED", "message": note}] if note else []
    if len(rows) > n:
        warnings.append({"code": "TRUNCATED",
                         "message": f"{len(rows)} matches, first {n} returned; total covers all."})
    return {"ok": True, "expenses": out, "count": len(out), "total_matching": len(rows),
            "total_amount": float(expenses.total(rows)), "warnings": warnings}


def get_balance(deps: Deps) -> dict:
    """The same snapshot as the bot's 'balance' command: unsettled expenses only."""
    b = balance.compute(deps.session)
    people = []
    for s in b.shares:
        net = _rupees(s.net)
        status = "gets back" if net > 0 else "pending" if net < 0 else "settled"
        people.append({"name": s.name, "paid": _rupees(s.paid), "status": status,
                       "amount": abs(net)})
    plan = [{"from": t.from_name, "to": t.to_name, "amount": t.amount} for t in b.plan]
    return {"ok": True, "total_unsettled": _rupees(b.total), "fair_share_each": _rupees(b.fair_share),
            "members": people, "settlement_plan": plan, "expense_count": len(b.expense_ids),
            "text": b.text, "warnings": []}
