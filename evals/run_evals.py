"""Chat evals: the REAL model on real expense messages (evals/cases.jsonl). Not deployed.

Run:  GEMINI_API_KEY=... EVAL_DATABASE_URL=postgresql://... python evals/run_evals.py
Env:  EVAL_RUNS (default 3)    — each case runs this many times; the model is not deterministic
      EVAL_DELAY_S (default 4) — pause between messages, for the free-tier rate limit
      EVAL_ONLY                — comma-separated case ids

Graded in code on OUTCOMES — rows written, rows deleted, tools called, questions left waiting —
never on reply wording. Exit code 1 if any run wrote or deleted a wrong row: that is the one
number that must stay at zero.
"""
import json
import os
import re
import sys
import time
from datetime import date
from decimal import Decimal

import harness

TODAY = date(2026, 10, 16)
SEED = [  # (day, amount, description, paid by, category, settled)
    (date(2026, 9, 20), 4000, "old diesel", "Biren", "Fuel", True),
    (date(2026, 10, 2), 1500, "diesel", "Sagar", "Fuel", False),
    (date(2026, 10, 3), 800, "seeds", "Lalo", "Plant", False),
    (date(2026, 10, 5), 6500, "pump repair", "Biren", "House", False),
    (date(2026, 10, 8), 2200, "diesel", "Sagar", "Fuel", False),
    (date(2026, 10, 10), 300, "tea", "Lalo", "Food", False),
    (date(2026, 10, 12), 5200, "labour", "Biren", "Labor", False),
    (date(2026, 10, 14), 450, "snacks", "Sagar", "Food", False),
    (date(2026, 10, 15), 900, "khatar", "Lalo", "Plant", False),
]


def seed() -> dict:
    from app.db.models import Expense, Settlement
    from app.db.session import session_scope
    ids = harness.reset()
    with session_scope() as s:
        settled = Settlement(settled_on=date(2026, 9, 30), created_by=ids["Sagar"])
        s.add(settled)
        s.flush()
        for day, amount, desc, payer, cat, done in SEED:
            s.add(Expense(expense_date=day, amount=Decimal(amount), description=desc,
                          paid_by=ids[payer], entered_by=ids[payer], category=cat,
                          settlement_id=settled.id if done else None))
    return ids


def same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def run_case(case: dict, client, tg, rec, delay: float) -> tuple[bool, bool, list[str]]:
    """One run of one case -> (passed, wrong_write, problems)."""
    from sqlalchemy import select
    from app.db.models import Expense, PendingAction
    from app.db.session import session_scope
    ids = seed()
    names = {v: k for k, v in ids.items()}
    rec.calls.clear()
    exp, problems, wrong = case["expect"], [], False
    replies = []
    for turn in case["turns"]:
        try:
            replies.append(harness.send(client, tg, turn["from"], turn["text"]))
        except RuntimeError as err:
            return False, False, [str(err)]
        time.sleep(delay)
    if any(re.search(r"\bow(e|es|ed)\b", r, re.IGNORECASE) for r in replies):
        problems.append("reply uses the word 'owe'")
    sender = case["turns"][-1]["from"]

    with session_scope() as s:
        rows = list(s.scalars(select(Expense).order_by(Expense.id)))
        waiting_amount = s.scalar(select(PendingAction.id).where(PendingAction.kind == "amount")) is not None
    new = [r for r in rows if r.id > len(SEED)]
    deleted = [r for r in rows if r.id <= len(SEED) and r.deleted_at is not None] + \
              [r for r in new if r.deleted_at is not None]
    expected = list(exp.get("saved", []))
    # "may_save": either outcome is right (e.g. "500 sevsad, coffee": saving sevsad and asking
    # coffee's amount, or asking whether 500 is for both). Never wrong, never missed.
    optional = list(exp.get("may_save", []))
    for r in (r for r in new if r.deleted_at is None):
        match = next((e for e in expected if same(e["amount"], r.amount)
                      and ("paid_by" not in e or same(e["paid_by"], names[r.paid_by]))), None)
        if match is None:
            extra = next((e for e in optional if same(e["amount"], r.amount)
                          and ("paid_by" not in e or same(e["paid_by"], names[r.paid_by]))), None)
            if extra is not None:
                optional.remove(extra)
                continue
            wrong = True
            problems.append(f"WRONG WRITE: ₹{r.amount} '{r.description}' paid by {names[r.paid_by]}")
            continue
        expected.remove(match)
        if "category" in match and not same(match["category"], r.category):
            problems.append(f"category {r.category}, expected {match['category']}")
        if "date" in match and not same(match["date"], r.expense_date.strftime("%d-%m-%Y")):
            problems.append(f"date {r.expense_date}, expected {match['date']}")
        if "desc_any" in match and not any(w in r.description.lower() for w in match["desc_any"]):
            problems.append(f"description '{r.description}', expected one of {match['desc_any']}")
    problems += [f"missed: expected ₹{e['amount']} {e.get('paid_by', '')} to be saved" for e in expected]

    rule = exp.get("deleted", "none")
    for d in (d for d in deleted if d.id <= len(SEED)):
        if rule == "none" or names[d.entered_by] != sender:
            wrong = True
            problems.append(f"WRONG DELETE: #{d.id} entered by {names[d.entered_by]}")
    if rule == "sender" and not deleted:
        problems.append("expected the sender's last entry to be deleted")

    tools = [c["name"] for c in rec.calls]
    if "tools_any" in exp and not any(n in tools for n in exp["tools_any"]):
        problems.append(f"tools {tools}, expected one of {exp['tools_any']}")
    for tool, want in exp.get("args", {}).items():
        args = [c["args"] for c in rec.calls if c["name"] == tool]
        if not any(all(k in a and same(a[k], v) for k, v in want.items()) for a in args):
            problems.append(f"{tool} args {args}, expected {want}")
    if "awaiting_amount" in exp and waiting_amount != exp["awaiting_amount"]:
        problems.append(f"waiting for an amount: {waiting_amount}, expected {exp['awaiting_amount']}")
    if problems:
        # What the model actually asked for, so a failed run explains itself in the report.
        problems.append("model: " + ("; ".join(_short_call(c) for c in rec.calls) or "no tool call"))
    return not problems, wrong, problems


def _short_call(c: dict) -> str:
    args = c["args"]
    if c["name"] == "add_expenses":
        items = args.get("items") or []
        return "add_expenses[" + ", ".join(
            f"{i.get('amount')} {i.get('description', '')}/{i.get('paid_by', '')}"
            for i in items if isinstance(i, dict)) + "]"
    return c["name"] + (f"({json.dumps(args, ensure_ascii=False)[:60]})" if args else "()")


def main() -> None:
    harness.configure()
    from app.config import get_settings
    from app.integrations.gemini import GeminiLLM
    s = get_settings()
    rec = harness.Recorder(GeminiLLM(s.gemini_api_key, s.gemini_model, s.gemini_fallback_model))
    tg = harness.Telegram()
    client = harness.app_client(rec, tg)
    harness.pin_today(TODAY)

    runs = int(os.getenv("EVAL_RUNS", "3"))
    delay = float(os.getenv("EVAL_DELAY_S", "4"))
    only = {x.strip() for x in os.getenv("EVAL_ONLY", "").split(",") if x.strip()}
    cases = [json.loads(line) for line in (harness.HERE / "cases.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    cases = [c for c in cases if not only or c["id"] in only]

    results, total, wrong_runs, error_runs = [], 0, 0, 0
    for case in cases:
        passes, notes = 0, []
        for i in range(runs):
            ok, wrong, problems = run_case(case, client, tg, rec, delay)
            total += 1
            passes += ok
            wrong_runs += wrong
            error_runs += bool(problems and problems[0].startswith("HTTP "))
            if problems:
                notes.append(f"run {i + 1}: " + "; ".join(problems))
        results.append((case["id"], passes, notes))
        print(f"{'PASS' if passes == runs else 'FAIL'} {case['id']}: {passes}/{runs}", flush=True)
        for n in notes:
            print("      " + n, flush=True)

    full = sum(1 for _, p, _ in results if p == runs)
    lines = ["# Chat eval report", "", f"Model: `{s.gemini_model}` · {len(cases)} cases × {runs} runs", "",
             "| Metric | Value |", "|---|---|",
             f"| 🔴 Runs with a WRONG write/delete (must be 0) | **{wrong_runs}** / {total} |",
             f"| Cases passing every run | {full} / {len(cases)} |",
             f"| Runs that hit an API error | {error_runs} / {total} |", "",
             "| Case | Passed | Notes |", "|---|---|---|"]
    lines += [f"| {'✅' if p == runs else '❌'} `{cid}` | {p}/{runs} | {'<br>'.join(n)[:600]} |"
              for cid, p, n in results]
    harness.write_report("report.md", lines)
    print(f"\nWrong-write runs: {wrong_runs}/{total} · cases fully passing: {full}/{len(cases)}")
    if wrong_runs:
        sys.exit(1)
    if total and error_runs == total:
        sys.exit("Every run hit an API error — check GEMINI_API_KEY / model name, not the bot.")


if __name__ == "__main__":
    main()
