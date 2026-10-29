"""The eval runners themselves: their seeding and grading run here with the scripted model, so a
broken runner is caught on every push, not on the first (quota-costing) real-model run."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import harness  # noqa: E402
import run_evals  # noqa: E402
import run_rag_evals  # noqa: E402
from tests.conftest import call  # noqa: E402
from tests.farm_helpers import chunk_ids  # noqa: E402


@pytest.fixture
def ev(llm, monkeypatch):
    from app.main import app
    from app.telegram import commands
    monkeypatch.setattr(commands, "today", commands.today)   # restored after the test
    rec, tg = harness.Recorder(llm), harness.Telegram()
    client = harness.app_client(rec, tg)
    harness.pin_today(run_evals.TODAY)
    yield client, tg, rec, llm
    app.dependency_overrides.clear()


def case(turns, **expect):
    return {"id": "x", "turns": [{"from": w, "text": t} for w, t in turns], "expect": expect}


def run(ev, c):
    client, tg, rec, _ = ev
    return run_evals.run_case(c, client, tg, rec, 0)


def test_right_write_passes(ev):
    ev[3].script(call("add_expense", amount=500, description="diesel", paid_by="Biren", category="Fuel"))
    assert run(ev, case([("Sagar", "500 diesel by biren")],
                        saved=[{"amount": 500, "paid_by": "Biren", "category": "Fuel"}])) == (True, False, [])


def test_wrong_write_is_flagged(ev):
    # No name in the message -> the sender pays; the case expected Biren -> a wrong write.
    # (A wrong paid_by from the model is now corrected by code, so it can't be the example.)
    ev[3].script(call("add_expense", amount=500, description="diesel", paid_by="Sagar", category="Fuel"))
    ok, wrong, problems = run(ev, case([("Sagar", "500 diesel")],
                                       saved=[{"amount": 500, "paid_by": "Biren"}]))
    assert not ok and wrong and "WRONG WRITE" in problems[0]


def test_delete_rule_is_graded(ev):
    # "undo" is a command word (no model): Sagar's own last entry is removed.
    assert run(ev, case([("Sagar", "undo")], saved=[], deleted="sender"))[0]
    ok, wrong, problems = run(ev, case([("Sagar", "undo")], saved=[]))
    assert wrong and "WRONG DELETE" in " ".join(problems)


def test_tools_and_waiting_question_are_graded(ev):
    ev[3].script(call("add_expense", amount=0, description="tea"))
    ok, _, problems = run(ev, case([("Sagar", "tea by sagar")], saved=[], awaiting_amount=True,
                                   tools_any=["add_expense"]))
    assert ok, problems
    ev[3].script(call("get_balances"))
    ok, _, problems = run(ev, case([("Sagar", "hisab aapo")], saved=[], tools_any=["search_expenses"]))
    assert not ok and "expected one of" in problems[0]


def test_rag_seed_keeps_note_ids_and_indexes(ev):
    r = run_rag_evals.seed(ev[2])
    assert r["total"] >= 13 and {"J-6", "K-1#1"} <= chunk_ids()
    from app.db.session import session_scope
    from app.db.models import JournalEntry
    with session_scope() as s:            # the sequence moved past the seeded ids
        e = JournalEntry(entry_date=run_rag_evals.TODAY, plant="Mango", activity="spray", added_by=1)
        s.add(e)
        s.flush()
        assert e.id == 13


def test_may_save_is_neither_wrong_nor_missed(ev):
    ev[3].script(call("add_expenses", items=[
        {"amount": 150, "description": "tea", "paid_by": "Sagar"},
        {"amount": 500, "description": "sevsad", "paid_by": "Sagar"}]))
    c = case([("Sagar", "150 tea, 500 sevsad")], saved=[{"amount": 150, "paid_by": "Sagar"}],
             may_save=[{"amount": 500, "paid_by": "Sagar"}])
    assert run(ev, c) == (True, False, [])


def test_failed_run_lists_what_the_model_called(ev):
    ev[3].script(call("add_expenses", items=[{"amount": 500, "description": "diesel",
                                              "paid_by": "Sagar"}]))
    ok, wrong, problems = run(ev, case([("Sagar", "500 diesel")], saved=[]))
    assert not ok and wrong and problems[-1] == "model: add_expenses[500 diesel/Sagar]"
