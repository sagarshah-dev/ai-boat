"""📋 Farm review — a LangGraph supervisor with two helper agents (multi-agent).

Live baseline 13-11-2026: "Aa mahina nu farm review aapo" ->
"📒 No farm note found (01-11-2026 to 13-11-2026) ... for: review". No money, no work counts.

The helpers run at the same time, so each has its own scripted queue (llm.script_for), keyed by a
phrase of its own system prompt; the main agent and the writer use theirs.
"""
import threading
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import update

from app.ai import farm_review
from app.db.models import Expense
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from tests.bot_helpers import RAMESH, SAGAR, add_rows, expenses_in_db
from tests.conftest import call, calls, say
from tests.farm_helpers import TODAY, seed

ASK = "Aa mahina nu farm review aapo"
MONEY, FARM, WRITER = "MONEY helper", "FARM helper", "'salah' (advice) part"
NOV13 = date(2026, 11, 13)
FIRST = TODAY.replace(day=1)
PREV_FIRST = (FIRST - timedelta(days=1)).replace(day=1)


def money_helper(*lines):
    return (calls(("money_totals", {}), ("money_totals", {"which": "previous"})),
            say("\n".join(lines)))


def farm_helper(*lines):
    return (calls(("farm_work", {}), ("treatment_results", {}), ("plans_not_done", {})),
            say("\n".join(lines)))


def month_data():
    """This month: 3 expenses (₹1,500 / ₹500 / ₹200); the same days of last month: ₹1,000.
    Farm: copper ✅, sulphur ❌, neem waiting, watering, and a plan not done (dated last month:
    a plan stays pending until it is done, whatever month it was saved in)."""
    add_rows(("Sagar", 1500, "copper oxychloride", "Farm Inputs"),
             ("Biren", 500, "majuri", "Labour"), when=FIRST)
    add_rows(("Lalo", 200, "tea", "Misc"), when=TODAY)
    add_rows(("Sagar", 1000, "diesel", "Fuel"), when=PREV_FIRST)
    return seed(dict(date=FIRST, product="copper oxychloride", problem="daag",
                     result="✅ Worked: daag ochha"),
                dict(date=FIRST, product="wettable sulphur", problem="mor kala",
                     result="❌ Did not work"),
                dict(date=TODAY, product="neem oil", problem="mili bug"),
                dict(date=TODAY, activity="irrigation"),
                dict(date=PREV_FIRST, activity="planned", product="hexaconazole"))


def chats_with(bot, key):
    return [c for c in bot.llm.chats if key in c.system]


# ── The whole route, through Telegram ────────────────────────────────────────────────────────

def test_review_joins_money_and_farm_from_two_helpers(bot):
    ids = month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Total spend is ₹2,200, up 120% from ₹1,000."))
    bot.llm.script_for(FARM, *farm_helper(f"Copper (J-{ids[0]}) worked; sulphur did not."))
    bot.llm.script_for(WRITER, say("- Copper worked: keep ₹1,500 of Farm Inputs for it.\n"
                                   "- Check neem oil result this week."))
    r = bot.send(SAGAR, ASK)
    assert r.startswith(f"📋 Farm review — {FIRST.strftime('%B %Y')}")
    assert "💰 Money: ₹2,200 in 3 entries" in r and "₹1,000 (+120%)" in r
    assert "Farm Inputs ₹1,500 (68%)" in r and "Paid by: Sagar ₹1,500 · Biren ₹500 · Lalo ₹200" in r
    assert "Biggest: #1 ₹1,500 copper oxychloride · #2 ₹500 majuri · #3 ₹200 tea" in r
    assert "🌱 Farm work: Spray 3 · Watering 1" in r
    assert f"✅ Worked: copper oxychloride (J-{ids[0]})" in r
    assert f"❌ Did not work: wettable sulphur (J-{ids[1]})" in r
    assert f"⏳ Waiting for result: neem oil (J-{ids[2]})" in r
    assert f"📝 Planned, not done: hexaconazole (J-{ids[4]})" in r
    assert "💡 Salah:\n- Copper worked: keep ₹1,500 of Farm Inputs for it." in r
    assert "money helper + farm helper" in r and "LangGraph supervisor" in r
    # each helper is its own agent: its own prompt and only its own tools
    (m,), (f,) = chats_with(bot, MONEY), chats_with(bot, FARM)
    assert {t.name for t in m.tools} == {"money_totals", "biggest_expenses"}
    assert {t.name for t in f.tools} == {"farm_work", "treatment_results", "plans_not_done"}
    assert m.tool_results[0][1][1]["total"] == 1000           # "previous" = same days before
    assert bot.llm.turns == [] and all(q == [] for q in bot.llm.keyed.values())
    assert "owe" not in r.lower()


def test_last_month_is_a_whole_month_against_the_month_before(bot):
    add_rows(("Sagar", 1000, "diesel", "Fuel"), when=PREV_FIRST)
    bot.llm.script(call("farm_review", period="last_month"))
    bot.llm.script_for(MONEY, *money_helper("Spend was ₹1,000."))
    bot.llm.script_for(WRITER, say("- Spend was ₹1,000."))
    r = bot.send(SAGAR, "gaya mahina no saar")
    assert f"Farm review — {PREV_FIRST.strftime('%B %Y')}" in r and "₹1,000 in 1 entry" in r
    assert "%)" not in r.split("Same days")[1].splitlines()[0]   # month before was ₹0: no %


def test_an_unknown_period_is_this_month(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review", period="next_year"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹300."))
    bot.llm.script_for(WRITER, say("- ok"))
    assert f"Farm review — {FIRST.strftime('%B %Y')}" in bot.send(SAGAR, ASK)


def test_nothing_to_review_asks_no_helper(bot):
    bot.llm.script(call("farm_review"))
    r = bot.send(SAGAR, ASK)
    assert r.startswith("📋 Nothing to review") and len(bot.llm.chats) == 1


def test_only_money_starts_only_the_money_helper(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹300."))
    bot.llm.script_for(WRITER, say("- Only tea: ₹300."))
    r = bot.send(SAGAR, ASK)
    assert "🌱 Farm: no farm notes in this period." in r and not chats_with(bot, FARM)
    assert "How: LangGraph supervisor → money helper (" in r


def test_only_farm_starts_only_the_farm_helper(bot):
    seed(dict(date=TODAY, activity="irrigation"))
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(FARM, *farm_helper("One watering."))
    bot.llm.script_for(WRITER, say("- Watering 1 time."))
    r = bot.send(SAGAR, ASK)
    assert "💰 Money: no expenses in this period or the one before." in r
    assert "Watering 1" in r and not chats_with(bot, MONEY)


def test_a_guest_can_read_the_review_like_the_balance(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹300."))
    bot.llm.script_for(WRITER, say("- ok"))
    assert "💰 Money: ₹300" in bot.send(RAMESH, ASK)


def test_deleted_expenses_are_not_counted(bot):
    add_rows(("Sagar", 300, "tea"), ("Sagar", 900, "wrong"), when=TODAY)
    with session_scope() as s:
        s.execute(update(Expense).where(Expense.amount == 900)
                  .values(deleted_at=datetime.now(timezone.utc)))
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹300."))
    bot.llm.script_for(WRITER, say("- ok"))
    r = bot.send(SAGAR, ASK)
    assert "₹300 in 1 entry" in r and "900" not in r


def test_the_next_message_sees_the_review_in_memory(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹300."))
    bot.llm.script_for(WRITER, say("- ok"))
    bot.send(SAGAR, ASK)
    bot.llm.script(say("Tea was ₹300."))
    assert "Tea was ₹300." in bot.send(SAGAR, "aama sauthi motu kharch kayu?")
    main = [c for c in bot.llm.chats if MONEY not in c.system and FARM not in c.system][-1]
    assert main.history[0] == ("user", ASK) and "Farm review" in main.history[1][1]


def test_yes_after_a_review_confirms_nothing(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹300."))
    bot.llm.script_for(WRITER, say("- ok"))
    bot.send(SAGAR, ASK)
    assert bot.send(SAGAR, "yes") == "Nothing is waiting for your yes/no right now."


# ── After a check fails ─────────────────────────────────────────────────────────────────────

def test_one_helper_failing_keeps_the_numbers_and_the_other_helper(bot):
    month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, LlmError("quota"))
    bot.llm.script_for(FARM, *farm_helper("Sulphur did not work."))
    bot.llm.script_for(WRITER, say("- Sulphur did not work; try what worked."))
    r = bot.send(SAGAR, ASK)
    assert "💰 Money: ₹2,200 in 3 entries" in r and "Sulphur did not work" in r
    assert "The AI part could not run just now" in r and "money helper (failed)" in r


def test_both_helpers_failing_still_gives_the_full_numbers(bot):
    month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, LlmError("quota"))
    bot.llm.script_for(FARM, LlmError("timeout"))
    r = bot.send(SAGAR, ASK)
    assert "₹2,200" in r and "⏳ Waiting for result" in r and "💡 Salah" not in r
    assert "could not run just now" in r


def test_the_writer_failing_drops_only_the_salah(bot):
    month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹2,200."))
    bot.llm.script_for(FARM, *farm_helper("Copper worked."))
    bot.llm.script_for(WRITER, LlmError("quota"))
    r = bot.send(SAGAR, ASK)
    assert "₹2,200" in r and "💡 Salah" not in r


def test_a_made_up_number_or_banned_word_drops_that_line(bot):
    month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spray cost ₹9,999.", "Spend is ₹2,200."))
    bot.llm.script_for(FARM, *farm_helper("Copper worked."))
    bot.llm.script_for(WRITER, say("- Save ₹700 next month.\n- Biren owes Sagar ₹500.\n"
                                   "- Spend is ₹2,200; copper worked.\n- 4th line"))
    r = bot.send(SAGAR, ASK)
    assert "9,999" not in r and "₹700" not in r and "owes" not in r
    assert "- Spend is ₹2,200; copper worked." in r and "4th line" not in r
    # the writer saw only the grounded helper notes
    writer_prompt = next(p for sysm, p in bot.llm.completions if WRITER in sysm)
    assert "9,999" not in writer_prompt.split("NOTES:")[1]


def test_a_helper_that_never_stops_calling_tools_is_stopped(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *[call("biggest_expenses")] * (farm_review.HELPER_ROUNDS + 1))
    r = bot.send(SAGAR, ASK)
    assert "₹300" in r and "💡 Salah" not in r      # no notes -> the writer is not asked
    assert not [1 for sysm, _ in bot.llm.completions if WRITER in sysm]


def test_a_helper_asking_for_a_tool_it_does_not_have_gets_an_error_not_a_crash(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, call("add_expense", amount=5), say("Spend is ₹300."))
    bot.llm.script_for(WRITER, say("- Spend is ₹300."))
    assert "- Spend is ₹300." in bot.send(SAGAR, ASK)
    (m,) = chats_with(bot, MONEY)
    assert m.tool_results[0][0][1] == {"error": "Unknown tool add_expense"}
    assert len(expenses_in_db()) == 1                          # nothing was written


# ── Parallel ────────────────────────────────────────────────────────────────────────────────

def test_the_helpers_run_at_the_same_time_on_two_threads(bot, monkeypatch):
    month_data()
    seen, real = {}, farm_review.run_helper

    def slow(llm, who, facts):
        seen[who] = threading.get_ident()
        time.sleep(0.4)
        return real(llm, who, facts)
    monkeypatch.setattr(farm_review, "run_helper", slow)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spend is ₹2,200."))
    bot.llm.script_for(FARM, *farm_helper("Copper worked."))
    bot.llm.script_for(WRITER, say("- ok"))
    t0 = time.monotonic()
    bot.send(SAGAR, ASK)
    assert time.monotonic() - t0 < 0.75 and len(set(seen.values())) == 2


# ── Code-only parts (no model) ──────────────────────────────────────────────────────────────

def test_compare_with_the_same_days_of_the_month_before():
    assert farm_review.month_range(NOV13, "this_month") == (
        date(2026, 11, 1), NOV13, date(2026, 10, 1), date(2026, 10, 13))
    # 31 March: February has only 28 days -> the whole of February
    assert farm_review.month_range(date(2027, 3, 31), "this_month")[2:] == (
        date(2027, 2, 1), date(2027, 2, 28))
    # January: the month before is December of the year before
    assert farm_review.month_range(date(2027, 1, 5), "last_month") == (
        date(2026, 12, 1), date(2026, 12, 31), date(2026, 11, 1), date(2026, 11, 30))
    assert farm_review.month_range(date(2026, 11, 1), "this_month")[2:] == (
        date(2026, 10, 1), date(2026, 10, 1))


def test_same_days_total_and_whole_month_total(bot):
    add_rows(("Sagar", 100, "tea"), when=date(2026, 10, 5))
    add_rows(("Sagar", 900, "diesel"), when=date(2026, 10, 25))      # after the 13th
    add_rows(("Sagar", 300, "tea"), when=date(2026, 11, 2))
    with session_scope() as s:
        f = farm_review.gather_facts(s, NOV13, "this_month")
    assert f["compared_with"]["total"] == 100 and f["compared_with"]["change_pct"] == 200
    assert f["previous_month_total"] == 1000
    text = farm_review.review_text({"facts": f, "path": ["gather"]})
    assert "Same days of October 2026 (01-10-2026 to 13-10-2026): ₹100 (+200%)" in text
    assert "Whole October 2026: ₹1,000" in text and "How:" not in text


def test_partly_and_own_words_results_are_not_called_worked(bot):
    ids = seed(dict(date=NOV13, product="humic", result="➖ Partly: thodu saru"),
               dict(date=NOV13, product="neem", result="thodo fark padyo"))
    with session_scope() as s:
        w = farm_review.gather_facts(s, NOV13, "this_month")["farm"]
    assert sorted(t["id"] for t in w["partly"]) == [f"J-{ids[0]}", f"J-{ids[1]}"]
    assert w["worked"] == [] and w["waiting_for_result"] == []


def test_a_done_plan_is_not_pending(bot):
    seed(dict(date=NOV13, activity="spray", product="hexaconazole"))      # the plan, done
    with session_scope() as s:
        assert farm_review.gather_facts(s, NOV13, "this_month")["farm"]["planned_not_done"] == []


def test_grounding_reads_numbers_the_way_people_write_them():
    facts = {"total": 12500, "period": "01-11-2026 to 13-11-2026", "pct": 25, "id": "J-7"}
    ok = ["Spend ₹12,500 so far.", "Since 1 Nov, up 25%.", "J-7 worked.", "No numbers here."]
    bad = ["Spend ₹12,600.", "Up 26%.", "Saved 2.5 lakh."]
    assert farm_review.grounded(ok + bad, facts) == ok


def test_helper_tools_read_only_the_facts():
    facts = {"period": "p", "month": "m", "previous_month": "pm", "previous_month_total": 5,
             "compared_with": {"total": 1, "count": 1, "period": "x", "change_pct": None},
             "money": {"total": 2, "count": 1, "by_category": {}, "share_pct": {},
                       "by_payer": {}, "biggest": []},
             "farm": {"notes": 0, "work": {}, "worked": [], "partly": [], "did_not_work": [],
                      "waiting_for_result": [], "planned_not_done": []}}
    assert farm_review.helper_tool(facts, "money_totals", {})["total"] == 2
    assert farm_review.helper_tool(facts, "money_totals", {"which": "previous"})[
        "whole_previous_month"] == 5
    assert farm_review.helper_tool(facts, "drop_table", {}) == {"error": "Unknown tool drop_table"}
