"""📋 Farm review — a LangGraph supervisor with two helper agents (multi-agent).

Live baseline 13-11-2026: "Aa mahina nu farm review aapo" ->
"📒 No farm note found (01-11-2026 to 13-11-2026) ... for: review". No money, no work counts.

Live 16-11-2026 (first version), each now a test below:
- the writer joined "₹1,290 agri medicine" to J-2 (both real, the link invented)
- "+8826%" against September, before the bot had data
- the October review showed a plan saved on 13-11
- "Same days of September (01-09 to 30-09)" for a whole month
- salah lines repeated numbers ("Sagar spent 2605 out of 3535"); the reply was too long

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
MONEY, FARM, WRITER = "MONEY helper", "FARM helper", "advice lines for a monthly farm review"
NOV13 = date(2026, 11, 13)
FIRST = TODAY.replace(day=1)
PREV_FIRST = (FIRST - timedelta(days=1)).replace(day=1)


def money_helper(*lines):
    return (calls(("money_totals", {}), ("biggest_expenses", {})), say("\n".join(lines)))


def farm_helper(*lines):
    return (calls(("farm_work", {}), ("treatment_results", {}), ("plans_not_done", {})),
            say("\n".join(lines)))


def month_data():
    """This month: Copper ₹1,500 (names the copper spray), majuri ₹500, tea ₹200.
    Farm: copper ✅, sulphur ❌, neem with no result yet, watering, and a plan not done."""
    add_rows(("Sagar", 1500, "Copper", "Farm Inputs"),
             ("Biren", 500, "majuri", "Labour"), when=FIRST)
    add_rows(("Lalo", 200, "tea", "Misc"), when=TODAY)
    add_rows(("Sagar", 1000, "diesel", "Fuel"), when=PREV_FIRST)       # last month: not shown
    return seed(dict(date=FIRST, product="copper oxychloride", problem="daag",
                     result="✅ Worked: daag ochha"),
                dict(date=FIRST, product="wettable sulphur", problem="mor kala",
                     result="❌ Did not work"),
                dict(date=TODAY, product="neem oil", problem="mili bug"),
                dict(date=TODAY, activity="irrigation"),
                dict(date=PREV_FIRST, activity="planned", product="hexaconazole"))


def chats_with(bot, key):
    return [c for c in bot.llm.chats if key in c.system]


def writer_prompt(bot):
    return next(p for sysm, p in bot.llm.completions if WRITER in sysm)


# ── The whole route, through Telegram ────────────────────────────────────────────────────────

def test_review_is_short_and_joins_money_and_farm_only_through_a_link(bot):
    ids = month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Most money went to Farm Inputs: ₹1,500."))
    bot.llm.script_for(FARM, *farm_helper(f"Copper (J-{ids[0]}) worked; sulphur did not."))
    bot.llm.script_for(WRITER, say(f"- ₹1,500 on Copper (#1) paid for J-{ids[0]} copper "
                                   "oxychloride, which worked."))
    r = bot.send(SAGAR, ASK)
    start, end = FIRST.strftime("%d-%m"), TODAY.strftime("%d-%m")
    assert r == (
        f"📋 {FIRST.strftime('%B %Y')} review ({start} to {end})\n\n"
        "💰 ₹2,200 · 3 entries\n"
        "Farm Inputs ₹1,500 · Labour ₹500 · Misc ₹200\n"
        "Paid: Sagar ₹1,500 · Biren ₹500 · Lalo ₹200\n\n"
        "🌱 Spray 3 · Watering 1\n"
        f"✅ copper oxychloride (J-{ids[0]})\n"
        f"❌ wettable sulphur (J-{ids[1]})\n"
        f"⏳ Result pending: neem oil (J-{ids[2]})\n"
        f"📝 Plan not done: hexaconazole (J-{ids[4]})\n\n"
        "💡 Salah\n"
        f"- ₹1,500 on Copper (#1) paid for J-{ids[0]} copper oxychloride, which worked.\n\n"
        "🔎 money + farm helpers (AI, in parallel) · numbers from the database")
    assert "%" not in r and "owe" not in r.lower() and "diesel" not in r
    # each helper is its own agent: its own prompt and only its own tools
    (m,), (f,) = chats_with(bot, MONEY), chats_with(bot, FARM)
    assert {t.name for t in m.tools} == {"money_totals", "biggest_expenses"}
    assert {t.name for t in f.tools} == {"farm_work", "treatment_results", "plans_not_done"}
    # the writer gets the helpers' notes and code's links, not every fact
    assert f'"note": "J-{ids[0]}"' in writer_prompt(bot) and "by_payer" not in writer_prompt(bot)
    assert bot.llm.turns == [] and all(q == [] for q in bot.llm.keyed.values())


def test_live_invented_link_is_dropped(bot):
    # Live 16-11: '₹1,290 agri medicine went to work that includes note J-2' — no such link.
    add_rows(("Sagar", 1290, "agri medicine", "Plant"), when=FIRST)
    jid = seed(dict(date=FIRST, product="", activity="spray"))[0]
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("₹1,290 on agri medicine."))
    bot.llm.script_for(FARM, *farm_helper(f"Spray J-{jid} has no result."))
    bot.llm.script_for(WRITER, say(f"- The ₹1,290 spent on agri medicine went to J-{jid}."))
    r = bot.send(SAGAR, ASK)
    assert "went to J-" not in r and '"links": []' not in writer_prompt(bot)
    assert "LINKS:\n[]" in writer_prompt(bot)


def test_live_salah_that_only_repeats_numbers_is_not_code_s_job_but_reminders_are(bot):
    """The useful salah now comes from code: a result pending 7+ days, a plan saved twice."""
    old = TODAY - timedelta(days=20)
    ids = seed(dict(date=old, product="neem"),                          # no result, 20 days
               dict(date=TODAY - timedelta(days=3), product="humic"),  # 3 days: not due yet
               dict(date=old, activity="planned", product="hexaconazole"),
               dict(date=TODAY, activity="planned", product="Hexaconazole "))
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(FARM, *farm_helper("Two plans are waiting."))
    bot.llm.script_for(WRITER, say(""))                                 # nothing useful: no line
    r = bot.send(SAGAR, ASK)
    assert f"- Check the result of neem (J-{ids[0]}): done 20 days ago, no result yet." in r
    assert f"- hexaconazole is planned 2 times (J-{ids[2]}, J-{ids[3]}): is one extra?" in r
    assert "humic (J-" not in r.split("💡")[1]


def test_last_month_is_a_whole_month_with_no_comparison(bot):
    add_rows(("Sagar", 1000, "diesel", "Fuel"), when=PREV_FIRST)
    add_rows(("Sagar", 200, "tea"), when=(PREV_FIRST - timedelta(days=1)))   # month before
    bot.llm.script(call("farm_review", period="last_month"))
    bot.llm.script_for(MONEY, *money_helper("Fuel ₹1,000."))
    bot.llm.script_for(WRITER, say(""))
    r = bot.send(SAGAR, "gaya mahina no saar")
    assert r.startswith(f"📋 {PREV_FIRST.strftime('%B %Y')} review (")
    assert "💰 ₹1,000 · 1 entry" in r and "200" not in r and "Same days" not in r


def test_live_a_plan_saved_after_the_period_is_not_in_its_review(bot):
    seed(dict(date=PREV_FIRST, activity="irrigation"),
         dict(date=FIRST, activity="planned", product="hexaconazole"))       # saved this month
    bot.llm.script(call("farm_review", period="last_month"))
    bot.llm.script_for(FARM, *farm_helper("One watering."))
    bot.llm.script_for(WRITER, say(""))
    r = bot.send(SAGAR, "gaya mahina no saar")
    assert "hexaconazole" not in r and "🌱 Watering 1" in r


def test_an_unknown_period_is_this_month(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review", period="next_year"))
    bot.llm.script_for(MONEY, *money_helper("Tea ₹300."))
    bot.llm.script_for(WRITER, say(""))
    assert bot.send(SAGAR, ASK).startswith(f"📋 {FIRST.strftime('%B %Y')} review")


def test_nothing_to_review_asks_no_helper(bot):
    bot.llm.script(call("farm_review"))
    r = bot.send(SAGAR, ASK)
    assert r.startswith("📋 Nothing to review") and len(bot.llm.chats) == 1


def test_only_money_starts_only_the_money_helper(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Tea ₹300."))
    bot.llm.script_for(WRITER, say(""))
    r = bot.send(SAGAR, ASK)
    assert "🌱 No farm notes." in r and not chats_with(bot, FARM)
    assert r.endswith("🔎 money helper (AI, in parallel) · numbers from the database")


def test_only_farm_starts_only_the_farm_helper(bot):
    seed(dict(date=TODAY, activity="irrigation"))
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(FARM, *farm_helper("One watering."))
    bot.llm.script_for(WRITER, say(""))
    r = bot.send(SAGAR, ASK)
    assert "💰 No expenses." in r and "🌱 Watering 1" in r and not chats_with(bot, MONEY)


def test_only_an_old_pending_result_is_still_worth_a_review(bot):
    jid = seed(dict(date=TODAY - timedelta(days=40), product="neem"))[0]
    bot.llm.script(call("farm_review"))
    r = bot.send(SAGAR, ASK)
    assert f"Check the result of neem (J-{jid}): done 40 days ago" in r
    assert len(bot.llm.chats) == 1                     # nothing for a helper to look at


def test_a_guest_can_read_the_review_like_the_balance(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Tea ₹300."))
    bot.llm.script_for(WRITER, say(""))
    assert "💰 ₹300" in bot.send(RAMESH, ASK)


def test_deleted_expenses_are_not_counted(bot):
    add_rows(("Sagar", 300, "tea"), ("Sagar", 900, "wrong"), when=TODAY)
    with session_scope() as s:
        s.execute(update(Expense).where(Expense.amount == 900)
                  .values(deleted_at=datetime.now(timezone.utc)))
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Tea ₹300."))
    bot.llm.script_for(WRITER, say(""))
    r = bot.send(SAGAR, ASK)
    assert "💰 ₹300 · 1 entry" in r and "900" not in r


def test_the_next_message_sees_the_review_in_memory(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Tea ₹300."))
    bot.llm.script_for(WRITER, say(""))
    bot.send(SAGAR, ASK)
    bot.llm.script(say("Tea was ₹300."))
    assert "Tea was ₹300." in bot.send(SAGAR, "aama sauthi motu kharch kayu?")
    main = [c for c in bot.llm.chats if MONEY not in c.system and FARM not in c.system][-1]
    assert main.history[0] == ("user", ASK) and "review" in main.history[1][1]


def test_yes_after_a_review_confirms_nothing(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Tea ₹300."))
    bot.llm.script_for(WRITER, say(""))
    bot.send(SAGAR, ASK)
    assert bot.send(SAGAR, "yes") == "Nothing is waiting for your yes/no right now."


# ── After a check fails ─────────────────────────────────────────────────────────────────────

def test_one_helper_failing_keeps_the_numbers_and_the_other_helper(bot):
    month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, LlmError("quota"))
    bot.llm.script_for(FARM, *farm_helper("Sulphur did not work."))
    bot.llm.script_for(WRITER, say("- Sulphur did not work this month; check the plants."))
    r = bot.send(SAGAR, ASK)
    assert "💰 ₹2,200 · 3 entries" in r and "Sulphur did not work this month" in r
    assert "The AI part could not run just now" in r and "🔎 farm helper (AI" in r


def test_both_helpers_failing_still_gives_the_full_numbers(bot):
    month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, LlmError("quota"))
    bot.llm.script_for(FARM, LlmError("timeout"))
    r = bot.send(SAGAR, ASK)
    assert "₹2,200" in r and "⏳ Result pending" in r and "could not run just now" in r
    assert "🔎" not in r


def test_the_writer_failing_drops_only_its_lines(bot):
    month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Farm Inputs ₹1,500."))
    bot.llm.script_for(FARM, *farm_helper("Copper worked."))
    bot.llm.script_for(WRITER, LlmError("quota"))
    r = bot.send(SAGAR, ASK)
    assert "₹2,200" in r and "💡" not in r


def test_a_made_up_number_or_banned_word_drops_that_line(bot):
    ids = month_data()
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *money_helper("Spray cost ₹9,999.", "Farm Inputs ₹1,500."))
    bot.llm.script_for(FARM, *farm_helper("Copper worked."))
    bot.llm.script_for(WRITER, say(f"- Save ₹700 next month.\n- Biren owes Sagar ₹500.\n"
                                   f"- ₹500 majuri paid for J-{ids[1]}.\n- Copper worked; keep it."))
    r = bot.send(SAGAR, ASK)
    assert "700" not in r and "owes" not in r and "majuri paid" not in r     # unlinked join
    assert "- Copper worked; keep it." in r
    assert "9,999" not in writer_prompt(bot)       # the writer saw only grounded helper notes


def test_a_helper_that_never_stops_calling_tools_is_stopped(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, *[call("biggest_expenses")] * (farm_review.HELPER_ROUNDS + 1))
    r = bot.send(SAGAR, ASK)
    assert "₹300" in r and "💡" not in r            # no notes -> the writer is not asked
    assert not [1 for sysm, _ in bot.llm.completions if WRITER in sysm]


def test_a_helper_asking_for_a_tool_it_does_not_have_gets_an_error_not_a_crash(bot):
    add_rows(("Sagar", 300, "tea"), when=TODAY)
    bot.llm.script(call("farm_review"))
    bot.llm.script_for(MONEY, call("add_expense", amount=5), say("Tea ₹300."))
    bot.llm.script_for(WRITER, say("- Only tea this month."))
    assert "- Only tea this month." in bot.send(SAGAR, ASK)
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
    bot.llm.script_for(MONEY, *money_helper("Farm Inputs ₹1,500."))
    bot.llm.script_for(FARM, *farm_helper("Copper worked."))
    bot.llm.script_for(WRITER, say(""))
    t0 = time.monotonic()
    bot.send(SAGAR, ASK)
    assert time.monotonic() - t0 < 0.75 and len(set(seen.values())) == 2


# ── Code-only parts (no model) ──────────────────────────────────────────────────────────────

def test_month_ranges():
    assert farm_review.month_range(NOV13, "this_month") == (date(2026, 11, 1), NOV13)
    assert farm_review.month_range(date(2027, 1, 5), "last_month") == (
        date(2026, 12, 1), date(2026, 12, 31))
    assert farm_review.month_range(date(2026, 11, 1), "this_month") == (
        date(2026, 11, 1), date(2026, 11, 1))


def test_links_need_the_product_named_not_the_same_day(bot):
    add_rows(("Sagar", 500, "Copper"), ("Sagar", 80, "tea"), ("Sagar", 900, "Neem tel"),
             when=NOV13)
    j = seed(dict(date=NOV13, product="copper oxychloride", result="✅ Worked"),
             dict(date=NOV13, product="neem oil"))
    with session_scope() as s:
        got = farm_review.gather_facts(s, NOV13, "this_month")["links"]
    assert sorted((k["item"], k["note"], k["result"]) for k in got) == [
        ("Copper", f"J-{j[0]}", "worked"), ("Neem tel", f"J-{j[1]}", "pending")]


def test_partly_and_own_words_results_are_not_called_worked(bot):
    ids = seed(dict(date=NOV13, product="humic", result="➖ Partly: thodu saru"),
               dict(date=NOV13, product="neem", result="thodo fark padyo"))
    with session_scope() as s:
        w = farm_review.gather_facts(s, NOV13, "this_month")["farm"]
    assert sorted(t["id"] for t in w["partly"]) == [f"J-{ids[0]}", f"J-{ids[1]}"]
    assert w["worked"] == [] and w["result_pending"] == []


def test_a_done_plan_is_not_pending(bot):
    seed(dict(date=NOV13, activity="spray", product="hexaconazole"))      # the plan, done
    with session_scope() as s:
        assert farm_review.gather_facts(s, NOV13, "this_month")["farm"]["planned_not_done"] == []


def test_reminders_are_capped_and_oldest_first(bot):
    ids = seed(*[dict(date=NOV13 - timedelta(days=14 - i), product=f"p{i}") for i in range(5)])
    with session_scope() as s:
        got = farm_review.gather_facts(s, NOV13, "this_month")["reminders"]
    assert len(got) == farm_review.MAX_REMINDERS and f"(J-{ids[0]})" in got[0]


def test_a_result_pending_for_months_is_not_a_reminder_any_more(bot):
    # same 90 days as farm.open_treatments: older ones were most likely never followed up
    seed(dict(date=NOV13 - timedelta(days=91), product="neem"))
    with session_scope() as s:
        assert farm_review.gather_facts(s, NOV13, "this_month")["reminders"] == []


def test_grounding_reads_numbers_the_way_people_write_them():
    facts = {"total": 12500, "period": "01-11-2026 to 13-11-2026", "id": "J-7",
             "links": [{"note": "J-7", "amount": 500}], "x": 25}
    ok = ["Spend ₹12,500 so far.", "Since 1 Nov.", "J-7 worked.", "₹500 paid for J-7.",
          "No numbers here."]
    bad = ["Spend ₹12,600.", "Up 26 times.", "₹12,500 went to J-7."]
    assert farm_review.grounded(ok + bad, facts) == ok


def test_helper_tools_read_only_the_facts():
    facts = {"period": "p", "month": "m",
             "money": {"total": 2, "count": 1, "by_category": {}, "by_payer": {}, "biggest": []},
             "farm": {"notes": 0, "work": {}, "worked": [], "partly": [], "did_not_work": [],
                      "result_pending": [], "planned_not_done": []}}
    assert farm_review.helper_tool(facts, "money_totals", {})["total"] == 2
    assert farm_review.helper_tool(facts, "drop_table", {}) == {"error": "Unknown tool drop_table"}
