"""The four MCP tools, called the way Claude calls them (signed in, JSON-RPC over HTTP).

Data looks like the live bot's: entries start mid-October, one batch is already settled, one
row was deleted, a Farm Salah plan was saved twice, notes are in romanised Gujarati."""
import json
from datetime import date

import pytest
from sqlalchemy import select, update

from app.db.models import Expense, Member
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from app.mcp import tools as mcp_tools
from app.services import balance, rag
from tests.bot_helpers import BIREN, LALO, add_rows
from tests.farm_helpers import ago, seed, seed_knowledge, sync
from tests.mcp_helpers import Claude

D = date(2026, 10, 15)


@pytest.fixture
def claude(bot):
    return Claude(bot.client)


@pytest.fixture
def token(claude):
    return claude.sign_in()["access_token"]


@pytest.fixture
def money(bot):
    """Live-like expenses. Old settled batch, a deleted typo, then the open month."""
    with session_scope() as s:
        s.execute(update(Member).where(Member.name == "Biren").values(aliases=["bako"]))
        s.execute(update(Member).where(Member.name == "Lalo").values(aliases=["jignesh"]))
    add_rows(("Sagar", 900, "old diesel", "Fuel"), when=date(2026, 10, 14))
    add_rows(("Sagar", 999, "typo row"), when=D)
    with session_scope() as s:                        # settle the first, delete the second
        from app.db.models import Settlement
        st = Settlement(settled_on=date(2026, 10, 14),
                        created_by=s.scalar(select(Member.id).where(Member.name == "Sagar")))
        s.add(st)
        s.flush()
        s.execute(update(Expense).where(Expense.description == "old diesel").values(settlement_id=st.id))
        s.execute(update(Expense).where(Expense.description == "typo row").values(
            deleted_at=Expense.created_at))
    add_rows(("Sagar", 3000, "drip pipe", "Plant"), ("Biren", 1500, "diesel tractor", "Fuel"),
             when=date(2026, 10, 20))
    add_rows(("Lalo", 500, "majuri", "Labor"), ("Biren", 500, "diesel pump", "Fuel"),
             when=date(2026, 11, 2))
    add_rows(("Sagar", 120.5, "chaa naasto 50% off", "Food"), when=date(2026, 11, 10))


def _no_owe(payload: dict):
    text = json.dumps(payload).lower()
    assert " owe" not in text and "owes" not in text


# ── The tool list itself ───────────────────────────────────────────────────────────────────

def test_exactly_four_read_only_tools(claude, token):
    r = claude.rpc("tools/list", {}, token)
    listed = r.json()["result"]["tools"]
    assert sorted(t["name"] for t in listed) == ["get_balance", "last_farm_activity",
                                                 "search_expenses", "search_farm_notes"]
    for t in listed:
        assert t["annotations"]["readOnlyHint"] is True
        assert t["annotations"]["destructiveHint"] is False
        assert t["description"].strip()
        for name, prop in t["inputSchema"].get("properties", {}).items():
            assert prop.get("description"), f"{t['name']}.{name} has no description"


def test_unknown_tool_is_an_error_not_a_crash(claude, token):
    r = claude.rpc("tools/call", {"name": "add_expense", "arguments": {}}, token)
    assert r.status_code == 200
    body = r.json()
    assert "error" in body or body["result"]["isError"] is True


# ── get_balance ────────────────────────────────────────────────────────────────────────────

def test_balance_matches_the_bot_exactly(claude, token, money):
    out = claude.tool(token, "get_balance")
    with session_scope() as s:
        b = balance.compute(s)
    assert out["ok"] and not out["_is_error"]
    assert out["text"] == b.text                                     # same words as the bot
    assert out["total_unsettled"] == 5621                            # 3000+1500+500+500+120.5
    assert out["expense_count"] == 5                                 # settled + deleted left out
    by = {m["name"]: m for m in out["members"]}
    # fair share 1873.5: Sagar paid 3120.5 -> gets back 1247, Biren 2000 -> 127, Lalo 500 -> 1374
    assert by["Sagar"] == {"name": "Sagar", "paid": 3121, "status": "gets back", "amount": 1247}
    assert by["Biren"]["status"] == "gets back" and by["Biren"]["amount"] == 127
    assert by["Lalo"] == {"name": "Lalo", "paid": 500, "status": "pending", "amount": 1374}
    assert sum(t["amount"] for t in out["settlement_plan"]) == 1374
    assert all(t["from"] == "Lalo" for t in out["settlement_plan"])
    _no_owe(out)


def test_balance_with_nothing_open(claude, token):
    out = claude.tool(token, "get_balance")
    assert out["ok"] and out["total_unsettled"] == 0 and out["settlement_plan"] == []


def test_balance_does_not_settle_or_lock_anything(claude, token, money):
    before = _expense_rows()
    claude.tool(token, "get_balance")
    claude.tool(token, "get_balance")
    assert _expense_rows() == before


def _expense_rows():
    with session_scope() as s:
        return [(e.id, e.amount, e.settlement_id, e.deleted_at) for e in
                s.scalars(select(Expense).order_by(Expense.id))]


# ── search_expenses ────────────────────────────────────────────────────────────────────────

def test_expenses_all_open_and_settled_newest_first(claude, token, money):
    out = claude.tool(token, "search_expenses")
    assert out["ok"]
    descs = [e["description"] for e in out["expenses"]]
    assert descs[0] == "chaa naasto 50% off" and "typo row" not in descs
    assert "old diesel" in descs                                       # settled rows are history
    assert [e["settled"] for e in out["expenses"] if e["description"] == "old diesel"] == [True]
    assert out["total_matching"] == 6
    assert out["expenses"][0]["date"] == "10-11-2026"                  # dd-MM-yyyy


@pytest.mark.parametrize("args, descs, total", [
    ({"category": "fuel"}, ["diesel pump", "diesel tractor", "old diesel"], 2900),
    ({"category": "Fuel", "paid_by": "bako"}, ["diesel pump", "diesel tractor"], 2000),
    ({"paid_by": "JIGNESH"}, ["majuri"], 500),
    ({"paid_by": " sagar "}, ["chaa naasto 50% off", "drip pipe", "old diesel"], 4020.5),
    ({"text": "diesel"}, ["diesel pump", "diesel tractor", "old diesel"], 2900),
    ({"text": "diesel tractor"}, ["diesel tractor"], 1500),
    ({"text": "50%"}, ["chaa naasto 50% off"], 120.5),
    ({"text": "%"}, ["chaa naasto 50% off"], 120.5),                   # wildcard is a character
    ({"date_from": "01-11-2026", "date_to": "30-11-2026"},
     ["chaa naasto 50% off", "diesel pump", "majuri"], 1120.5),
    ({"date_from": "20-10-2026", "date_to": "20-10-2026"}, ["diesel tractor", "drip pipe"], 4500),
    ({"amount_min": 500, "amount_max": 500}, ["diesel pump", "majuri"], 1000),   # inclusive
    ({"amount_min": 1500}, ["diesel tractor", "drip pipe"], 4500),
    ({"amount_max": 120.5}, ["chaa naasto 50% off"], 120.5),
    ({"text": "cement"}, [], 0),                                       # nothing is not an error
])
def test_expense_filters(claude, token, money, args, descs, total):
    out = claude.tool(token, "search_expenses", **args)
    assert out["ok"] and not out["_is_error"], out
    assert sorted(e["description"] for e in out["expenses"]) == sorted(descs)
    assert out["total_amount"] == total


def test_total_covers_every_match_even_past_the_limit(claude, token, money):
    out = claude.tool(token, "search_expenses", limit=2)
    assert out["count"] == 2 and out["total_matching"] == 6
    assert out["total_amount"] == 6520.5                               # all 6, not the first 2
    assert [w["code"] for w in out["warnings"]] == ["TRUNCATED"]


@pytest.mark.parametrize("args, says", [
    ({"category": "Fuell"}, "unknown category"),
    ({"paid_by": "Ramesh"}, "unknown payer"),                         # a guest is never a payer
    ({"paid_by": "Mahesh"}, "unknown payer"),
    ({"date_from": "2026-10-01"}, "DD-MM-YYYY"),
    ({"date_to": "31-02-2026"}, "DD-MM-YYYY"),
    ({"amount_min": -5}, "zero or more"),
    ({"amount_min": 900, "amount_max": 100}, "larger than"),
    ({"limit": 0}, "between 1 and 50"),
    ({"limit": 51}, "between 1 and 50"),
])
def test_bad_expense_arguments_come_back_as_data(claude, token, money, args, says):
    out = claude.tool(token, "search_expenses", **args)
    assert out["_is_error"] is True and out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert says in out["error"]["message"]


def test_bad_argument_hint_lists_the_valid_values(claude, token, money):
    cat = claude.tool(token, "search_expenses", category="Fuell")["error"]["hint"]
    assert "Fuel" in cat and "Labor" in cat
    payer = claude.tool(token, "search_expenses", paid_by="Mahesh")["error"]["hint"]
    assert "Sagar" in payer and "Biren" in payer and "Ramesh" not in payer


def test_wrong_type_is_refused_by_the_schema(claude, token, money):
    r = claude.rpc("tools/call", {"name": "search_expenses", "arguments": {"limit": "lots"}}, token)
    body = r.json()
    assert "error" in body or body["result"]["isError"] is True


# ── last_farm_activity ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def farm_rows(bot):
    return seed(
        {"date": date(2026, 10, 16), "product": "copper oxychloride", "problem": "daag",
         "note": "kesar par daag, copper chhantyu", "variety": "Kesar",
         "result": "✅ Worked: 10 divas ma daag band"},
        {"date": date(2026, 10, 25), "plant": "Chiku", "activity": "treatment",
         "product": "chlorpyrifos", "problem": "udhai", "by": LALO},
        {"date": date(2026, 11, 5), "activity": "irrigation", "note": "drip 2 kalak", "by": BIREN},
        # Farm Salah saved the same plan twice (live data has this)
        {"date": date(2026, 11, 12), "activity": "planned", "product": "neem", "note": "neem chhantvu"},
        {"date": date(2026, 11, 12), "activity": "planned", "product": "neem", "note": "neem chhantvu"},
    )


def test_last_activity_newest_first_with_plans_labelled(claude, token, farm_rows):
    out = claude.tool(token, "last_farm_activity", limit=10)
    assert out["ok"] and out["total_matching"] == 5
    assert [e["date"] for e in out["entries"]] == ["12-11-2026", "12-11-2026", "05-11-2026",
                                                   "25-10-2026", "16-10-2026"]
    plans = [e for e in out["entries"] if e["activity"] == "planned"]
    assert len(plans) == 2 and all(p["label"] == "Planned (not done yet)" for p in plans)


@pytest.mark.parametrize("args, ids", [
    ({"plant": "aamba"}, [5, 4, 3, 1]),          # Gujarati alias -> mango; plan rows included
    # Kesar: its own row AND work done on all mangoes (no variety) — same rule as the bot
    ({"plant": "Kesar"}, [5, 4, 3, 1]),
    ({"plant": "chiku"}, [2]),
    ({"activity": "spray"}, [1]),                # a plan is NOT a spray
    ({"activity": "SPRAY", "plant": "mango"}, [1]),
    ({"plant": "chiku", "activity": "spray"}, []),
])
def test_last_activity_filters(claude, token, farm_rows, args, ids):
    out = claude.tool(token, "last_farm_activity", limit=50, **args)
    assert out["ok"], out
    assert [e["id"] for e in out["entries"]] == [f"J-{farm_rows[i - 1]}" for i in ids]


def test_last_activity_default_limit_and_result_shown(claude, token, farm_rows):
    out = claude.tool(token, "last_farm_activity", plant="kesar", activity="spray", limit=1)
    e = out["entries"][0]
    assert e["product"] == "copper oxychloride" and e["result"].startswith("✅ Worked")
    assert claude.tool(token, "last_farm_activity")["count"] == 5


@pytest.mark.parametrize("args, says", [
    ({"activity": "dance"}, "unknown activity"),
    ({"plant": "banana"}, "unknown plant"),
    ({"limit": 100}, "between 1 and 50"),
])
def test_last_activity_bad_arguments(claude, token, farm_rows, args, says):
    out = claude.tool(token, "last_farm_activity", **args)
    assert out["error"]["code"] == "INVALID_INPUT" and says in out["error"]["message"]


def test_unknown_plant_hint_lists_the_farm(claude, token, farm_rows):
    hint = claude.tool(token, "last_farm_activity", plant="banana")["error"]["hint"]
    assert "Mango" in hint and "Chiku" in hint


# ── search_farm_notes ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def notes(bot):
    ids = seed(
        {"date": ago(3), "product": "neem", "problem": "pan par fodla", "note": "pan par fodla, neem spray karyu"},
        {"date": ago(20), "product": "copper oxychloride", "problem": "daag vadhi gaya",
         "result": "✅ Worked: 10 divas ma daag band", "note": "daag vadhi gaya, copper spray"},
        {"date": ago(10), "plant": "Chiku", "activity": "treatment", "product": "chlorpyrifos",
         "problem": "udhai", "note": "north chiku ma udhai aavi", "by": LALO})
    kid = seed_knowledge("Farmer voice note", "Udhai mate chhod ni aaspaas neem khol naakhvo.",
                         when=ago(25))
    sync(bot.llm)
    return ids, kid


def test_farm_search_returns_notes_and_no_model_answer(claude, token, notes, bot):
    ids, _ = notes
    bot.llm.completions.clear()
    out = claude.tool(token, "search_farm_notes", question="aamba par fodla", question_english="spots on mango")
    assert out["ok"] and out["count"] >= 1
    # Ranking is the bot's own RAG (tested in test_farm_search); here: the note is there, intact.
    n = next(x for x in out["notes"] if x["id"] == f"J-{ids[0]}")
    assert n["kind"] == "journal" and n["date"] == ago(3).strftime("%d-%m-%Y")
    assert "⟦English⟧" not in n["text"]                              # the user's words only
    # Claude writes the answer itself: no second (Gemini) answer call on our side.
    assert not [p for sys, p in bot.llm.completions if sys == rag.ANSWER_RULES]


def test_farm_search_finds_farmer_advice(claude, token, notes):
    _, kid = notes
    out = claude.tool(token, "search_farm_notes", question="udhai mate shu karvu")
    kinds = {n["id"]: n["kind"] for n in out["notes"]}
    assert kinds.get(f"K-{kid}#1") == "farmer_advice"


def test_farm_search_result_is_included(claude, token, notes):
    ids, _ = notes
    out = claude.tool(token, "search_farm_notes", question="daag mate copper")
    hit = next(n for n in out["notes"] if n["id"] == f"J-{ids[1]}")
    assert hit["result"].startswith("✅ Worked")


def test_farm_search_with_plant_filter(claude, token, notes):
    ids, _ = notes
    out = claude.tool(token, "search_farm_notes", question="udhai", plant="chiku")
    assert [n["id"] for n in out["notes"]][:1] == [f"J-{ids[2]}"]


def test_farm_search_nothing_found_is_ok_and_empty(claude, token, notes):
    out = claude.tool(token, "search_farm_notes", question="tractor service kyare karavi")
    assert out["ok"] is True and out["_is_error"] is False
    assert out["notes"] == [] and out["count"] == 0


def test_farm_search_when_gemini_embedding_is_down(claude, token, notes, bot):
    bot.llm.embed_error = LlmError("quota")
    out = claude.tool(token, "search_farm_notes", question="fodla neem")
    assert out["ok"] and out["count"] >= 1                         # exact words still work
    assert "SEARCH_DEGRADED" in [w["code"] for w in out["warnings"]]


@pytest.mark.parametrize("args, says", [
    ({"question": ""}, "question is empty"),
    ({"question": "   "}, "question is empty"),
    ({"question": "x" * 301}, "longer than 300"),
    ({"question": "fodla", "plant": "banana"}, "unknown plant"),
    ({"question": "fodla", "date_from": "1/10/2026"}, "DD-MM-YYYY"),
])
def test_farm_search_bad_arguments(claude, token, notes, args, says):
    out = claude.tool(token, "search_farm_notes", **args)
    assert out["error"]["code"] == "INVALID_INPUT" and says in out["error"]["message"]


def test_farm_search_missing_question_is_refused_by_the_schema(claude, token, notes):
    r = claude.rpc("tools/call", {"name": "search_farm_notes", "arguments": {}}, token)
    body = r.json()
    assert "error" in body or body["result"]["isError"] is True


# ── Our own failure ────────────────────────────────────────────────────────────────────────

def test_internal_failure_is_a_plain_error_without_details(claude, token, money, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("password=hunter2 connection to 10.0.0.5 failed")
    monkeypatch.setattr(mcp_tools.balance, "compute", boom)
    out = claude.tool(token, "get_balance")
    assert out["_is_error"] is True and out["error"]["code"] == "INTERNAL"
    assert "hunter2" not in json.dumps(out) and "10.0.0.5" not in json.dumps(out)
    # and the next call works again
    monkeypatch.undo()
    assert claude.tool(token, "get_balance")["ok"] is True


def test_every_success_has_the_same_envelope(claude, token, money, farm_rows):
    for name, args in (("get_balance", {}), ("search_expenses", {}), ("last_farm_activity", {})):
        out = claude.tool(token, name, **args)
        assert out["ok"] is True and isinstance(out["warnings"], list) and out["_is_error"] is False
        _no_owe(out)
