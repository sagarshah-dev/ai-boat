"""🌱 Farm Salah (LangGraph): the three examples from the live baseline of 06-11 / 11-11-2026
(claude/farm-salah-baseline-test.md, section 8), every route through the graph, the next reply
after every question the bot asks, and who may answer.

  E1 "Kesar na 100 zhad par mor kala che. Spray plan banao."   journal -> dose -> calc -> approve
  E2 "...kai dava kaam kari, ane e dava chhelle kyare kharidi?" journal -> expense (by the product)
  E3 "Je divase Kesar par copper chhantyu, e divase biju ...?"  journal -> expense (by the date)
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text, update

from app.ai import farm_salah
from app.db.models import JournalEntry, PendingAction
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from tests.bot_helpers import BIREN, LALO, SAGAR, add_rows
from tests.conftest import call, say
from tests.farm_helpers import TODAY, ago, entry, journal, seed, seed_knowledge, sync

E1 = "Kesar na 100 zhad par mor kala che. Spray plan banao."
E2 = "Kesar par mor kala padya tyare kai dava kaam kari, ane e dava chhelle kyare kharidi?"
E3 = "Je divase Kesar par copper chhantyu, e divase biju kai kharch thayo?"
DOSE_TEXT = "Hexaconazole dose: 1 ml per litre water. One big mango tree needs about 5 litres."
JAN10, JAN20, OCT15 = date(2026, 1, 10), date(2026, 1, 20), date(2026, 10, 15)


@pytest.fixture
def farm_data(bot):
    """The owner's live data of 06-11-2026: J-1 sulphur ❌, J-2 hexaconazole ✅ (both 'mor kala'),
    J-3 copper ✅ for black LEAF spots — it shares the word 'kala' and must not be used."""
    ids = seed(
        dict(date=JAN10, variety="Kesar", product="wettable sulphur", problem="mor kala",
             note="Kesar mango: mor kala thaya. Sprayed wettable sulphur.",
             result="❌ Did not work: flowers still black"),
        dict(date=JAN20, variety="Kesar", product="hexaconazole", problem="mor kala",
             note="Kesar mango: mor haju kala. Sprayed hexaconazole.",
             result="✅ Worked: new flowers healthy"),
        dict(date=OCT15, variety="Kesar", product="copper oxychloride", problem="pan par kala daag",
             note="pan par kala daag dekhaya", result="✅ Worked: daag ochha thaya"))
    sync(bot.llm)
    return ids


def graded(*jids) -> object:
    return say(json.dumps({"relevant_ids": [f"J-{j}" for j in jids]}))


def dose_form(ml=1, litres=5, k=1) -> object:
    return say(json.dumps({"ml_per_litre": ml, "litres_per_tree": litres, "advice_id": f"K-{k}"}))


def salah(need, topic="mor kala", **extra):
    return call("farm_salah", need=need, topic=topic, topic_en="black mango flowers", **extra)


def waiting_rows() -> list[PendingAction]:
    with session_scope() as s:
        return list(s.scalars(select(PendingAction).where(PendingAction.kind == "salah")))


def checkpoints() -> int:
    with session_scope() as s:
        return s.execute(text("SELECT count(*) FROM checkpoints")).scalar()


def buttons(bot):
    return bot.tg.sent[-1]["buttons"]


def prompts(bot, rules) -> list[str]:
    return [p for sysmsg, p in bot.llm.completions if sysmsg == rules]


def plan_ready(bot, message=E1, trees=100):
    seed_knowledge("Hexaconazole dose for mango tree", DOSE_TEXT, source="text")
    sync(bot.llm)
    args = {"trees": trees} if trees else {}
    bot.llm.script(salah("plan", **args), graded(1, 2), dose_form())
    return bot.send(SAGAR, message)


# ── E1: the plan ────────────────────────────────────────────────────────────────────────────

def test_e1_plan_uses_what_worked_the_written_dose_and_code_arithmetic(bot, farm_data):
    r = plan_ready(bot)
    assert "🧪 Worked: hexaconazole — 20-01-2026 (J-2: ✅ Worked: new flowers healthy)" in r
    assert "⚠️ Did not work: wettable sulphur — 10-01-2026 (J-1)" in r
    assert "💧 Dose: 1 ml per litre, 5 L per tree (farmer advice K-1)" in r
    assert "🧮 100 trees × 5 L = 500 L water → 500 ml hexaconazole" in r
    assert "copper" not in r
    assert "🔎 How: LangGraph — journal" in r and "farmer advice → calculation (code)" in r
    assert buttons(bot) == farm_salah.PLAN_BUTTONS
    # Nothing saved before ✅; the graph is paused in Postgres, for the asker only.
    assert len(journal()) == 3
    assert [w.payload["waiting"] for w in waiting_rows()] == ["approve"]
    assert checkpoints() > 0


def test_e1_relevance_check_sees_the_notes_and_decides(bot, farm_data):
    plan_ready(bot)
    grade_prompt = prompts(bot, farm_salah.GRADE_RULES)[0]
    assert "[J-1]" in grade_prompt and "[J-2]" in grade_prompt
    assert E1 in grade_prompt


def test_e1_save_writes_one_planned_row_and_clears_the_wait(bot, farm_data):
    plan_ready(bot)
    r = bot.tap(SAGAR, "fs:save")
    assert "✅ Plan saved as J-4 (planned, not done yet)" in r
    assert "hexaconazole · 500 ml · 100 trees · Mango (Kesar)" in r
    assert "In 2 days" in r
    e = entry(4)
    assert (e.activity, e.product, e.plant_qty, e.variety) == ("planned", "hexaconazole", 100, "Kesar")
    assert e.dose == "1 ml per litre, 5 L per tree" and "worked: J-2, dose: K-1" in e.note
    assert waiting_rows() == [] and checkpoints() == 0
    # A second tap finds nothing: no double save.
    assert farm_salah.EXPIRED in bot.tap(SAGAR, "fs:save")
    assert len(journal()) == 4


def test_e1_typed_yes_saves_too(bot, farm_data):
    plan_ready(bot)
    assert "Plan saved" in bot.send(SAGAR, "yes")


@pytest.mark.parametrize("how", ["tap", "typed"])
def test_e1_cancel_saves_nothing(bot, farm_data, how):
    plan_ready(bot)
    r = bot.tap(SAGAR, "fs:no") if how == "tap" else bot.send(SAGAR, "no")
    assert "❌ Plan cancelled — nothing was saved." in r
    assert len(journal()) == 3 and waiting_rows() == [] and checkpoints() == 0


def test_e1_change_trees_goes_back_to_the_arithmetic_only(bot, farm_data):
    plan_ready(bot)
    assert "How many trees?" in bot.tap(SAGAR, "fs:edit")
    r = bot.send(SAGAR, "60 zhad j")
    assert "🧮 60 trees × 5 L = 300 L water → 300 ml hexaconazole" in r
    assert "trees changed → calculation again" in r
    # The notes and the dose were NOT looked up again: one relevance check, one dose read.
    assert len(prompts(bot, farm_salah.GRADE_RULES)) == 1
    assert len(prompts(bot, farm_salah.DOSE_RULES)) == 1
    bot.tap(SAGAR, "fs:save")
    assert entry(4).plant_qty == 60 and "300 ml" in entry(4).note


def test_e1_edit_reply_that_is_not_a_number_is_a_new_message(bot, farm_data):
    plan_ready(bot)
    bot.tap(SAGAR, "fs:edit")
    bot.llm.script(call("add_expenses", items=[{"amount": 100, "description": "tea"}]))
    r = bot.send(SAGAR, "100 tea")
    assert "Logged" in r
    # The plan is still there, still waiting for the number.
    assert waiting_rows()[0].payload["waiting"] == "edit"
    assert "40 trees" in bot.send(SAGAR, "40")


def test_e1_no_trees_given_asks_then_the_next_number_completes(bot, farm_data):
    r = plan_ready(bot, "Kesar par mor kala che. Spray plan banao.", trees=None)
    assert "🌳 How many trees?" in r and farm_salah.PLAN_BUTTONS != bot.tg.sent[-1]["buttons"]
    assert waiting_rows()[0].payload["waiting"] == "trees"
    # Something else in between: handled normally, the question stays.
    bot.send(SAGAR, "balance")
    assert waiting_rows()[0].payload["waiting"] == "trees"
    r = bot.send(SAGAR, "100")
    assert "🧮 100 trees × 5 L = 500 L water → 500 ml" in r


def test_e1_tree_count_the_user_never_wrote_is_ignored(bot, farm_data):
    # The model says 100; the message has no number: the bot asks instead of using 100.
    r = plan_ready(bot, "Kesar par mor kala che. Spray plan banao.", trees=100)
    assert "How many trees?" in r


def test_e1_another_member_cannot_approve(bot, farm_data):
    plan_ready(bot)
    assert farm_salah.EXPIRED in bot.tap(BIREN, "fs:save")
    assert farm_salah.EXPIRED in bot.tap(LALO, "fs:edit")
    assert len(journal()) == 3
    assert "Plan saved" in bot.tap(SAGAR, "fs:save")


def test_e1_plan_older_than_24_hours_cannot_be_saved(bot, farm_data):
    plan_ready(bot)
    with session_scope() as s:
        s.execute(update(PendingAction).where(PendingAction.kind == "salah")
                  .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert farm_salah.EXPIRED in bot.tap(SAGAR, "fs:save")
    assert len(journal()) == 3


def test_e1_a_new_task_replaces_the_waiting_one(bot, farm_data):
    plan_ready(bot)
    first = waiting_rows()[0].payload["thread"]
    bot.llm.script(salah("plan", trees=100), graded(1, 2), dose_form())
    bot.send(SAGAR, E1)
    rows = waiting_rows()
    assert len(rows) == 1 and rows[0].payload["thread"] != first
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
                         {"t": first}).scalar() == 0


def test_e1_no_dose_in_the_advice_means_no_plan(bot, farm_data):
    bot.llm.script(salah("plan", trees=100), graded(1, 2))
    r = bot.send(SAGAR, E1)
    assert "The dose of hexaconazole is not written in the farmer's advice" in r
    assert "🧮" not in r and waiting_rows() == [] and checkpoints() == 0


@pytest.mark.parametrize("form", [dict(ml=2), dict(litres=10), dict(k=7), dict(ml=0)])
def test_e1_dose_number_not_in_the_advice_is_rejected(bot, farm_data, form):
    seed_knowledge("Hexaconazole dose for mango tree", DOSE_TEXT, source="text")
    bot.llm.script(salah("plan", trees=100), graded(1, 2), dose_form(**form))
    r = bot.send(SAGAR, E1)
    assert "not written in the farmer's advice" in r and "🧮" not in r


def test_e1_nothing_worked_means_no_plan(bot):
    seed(dict(date=JAN10, variety="Kesar", product="wettable sulphur", problem="mor kala",
              note="mor kala", result="❌ Did not work: flowers still black"))
    sync(bot.llm)
    bot.llm.script(salah("plan", trees=100), graded(1))
    r = bot.send(SAGAR, E1)
    assert "No treatment for \"mor kala\" is written as worked (✅)" in r
    assert "⚠️ Tried: wettable sulphur — 10-01-2026 (J-1): ❌ Did not work" in r
    assert waiting_rows() == []


def test_e1_check_says_no_note_is_about_it(bot, farm_data):
    bot.llm.script(salah("plan", trees=100), graded())
    r = bot.send(SAGAR, E1)
    assert "No note about \"mor kala\" for Mango (Kesar) in the journal" in r


def test_e1_check_cannot_add_a_note_it_was_not_shown(bot, farm_data):
    seed_knowledge("Hexaconazole dose for mango tree", DOSE_TEXT, source="text")
    sync(bot.llm)
    bot.llm.script(salah("plan", trees=100),
                   say(json.dumps({"relevant_ids": ["J-2", "J-99"]})), dose_form())
    r = bot.send(SAGAR, E1)
    assert "J-99" not in r and "500 ml" in r


def test_model_failure_tells_the_user_and_leaves_nothing_paused(bot, farm_data):
    bot.llm.script(salah("plan", trees=100), LlmError("quota", "429"))
    r = bot.send(SAGAR, E1)
    assert "free AI limit" in r
    assert waiting_rows() == [] and checkpoints() == 0 and len(journal()) == 3


def test_unknown_need_goes_back_to_the_model(bot, farm_data):
    bot.llm.script(call("farm_salah", need="dance", topic="mor"), say("Which one do you mean?"))
    assert "Which one" in bot.send(SAGAR, E1)


# ── E2: which medicine worked, and when was IT bought ──────────────────────────────────────

def test_e2_expense_is_searched_by_the_medicine_found_not_by_the_word_dava(bot, farm_data):
    add_rows(("Biren", 650, "hexaconazole"), when=JAN20)
    add_rows(("Sagar", 400, "wettable sulphur"), when=JAN10)
    bot.llm.script(salah("purchase"), graded(1, 2))
    r = bot.send(SAGAR, E2)
    assert "🧪 Worked: hexaconazole — 20-01-2026" in r
    assert "⚠️ Did not work: wettable sulphur" in r
    assert "💰 Last bought: 20-01-2026 — ₹650 — hexaconazole — Biren (#1)" in r
    assert "dava" not in r.split("💰")[1]
    assert "journal (meaning + exact words, AI-checked) → expenses (SQL)" in r
    assert waiting_rows() == [] and checkpoints() == 0       # nothing to approve: finished


def test_e2_purchase_not_written_is_said(bot, farm_data):
    bot.llm.script(salah("purchase"), graded(1, 2))
    r = bot.send(SAGAR, E2)
    assert "No expense with \"hexaconazole\" in its description — the purchase is not written." in r


def test_e2_two_word_product_bought_under_its_first_word(bot):
    seed(dict(date=JAN10, variety="Kesar", product="wettable sulphur", problem="mor kala",
              note="mor kala", result="✅ Worked: ok"))
    sync(bot.llm)
    add_rows(("Sagar", 400, "sulphur"), when=JAN10)
    bot.llm.script(salah("purchase"), graded(1))
    assert "₹400 — sulphur" in bot.send(SAGAR, E2)


# ── E3: the day of a task, then that day's expenses ─────────────────────────────────────────

def test_e3_expenses_of_the_day_the_note_was_written(bot, farm_data):
    add_rows(("Lalo", 300, "majuri"), when=OCT15)
    add_rows(("Sagar", 999, "diesel"), when=OCT15 + timedelta(days=1))
    bot.llm.script(salah("same_day", topic="copper"), graded(3))
    r = bot.send(SAGAR, E3)
    assert "📒 copper oxychloride — 15-10-2026 (J-3)" in r
    assert "• #1 — ₹300 — majuri — Lalo" in r and "💵 Total: ₹300" in r
    assert "diesel" not in r


def test_e3_no_expense_that_day(bot, farm_data):
    bot.llm.script(salah("same_day", topic="copper"), graded(3))
    assert "No expense written on 15-10-2026" in bot.send(SAGAR, E3)


# ── Small pieces ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,n", [("60", 60), ("60 zhad j", 60), ("fakt 40 trees", 40),
                                    ("100 tea", None), ("60 70", None), ("0", None),
                                    ("aaje 50 diesel", None), ("9999", None)])
def test_trees_from_reply(text, n):
    assert farm_salah.trees_from_reply(text) == n


@pytest.mark.parametrize("result,outcome", [("✅ Worked: ok", "worked"), ("❌ Did not work: x", "not_worked"),
                                            ("➖ Partly: x", "partly"), ("", ""), ("ok", "")])
def test_outcome_is_read_from_the_result_label(result, outcome):
    assert farm_salah.outcome_of(result) == outcome


def test_graph_shape():
    m = farm_salah.mermaid()
    for edge in ("journal -.-> dose", "journal -.-> expense", "journal -.-> answer",
                 "calc --> approve", "approve -.-> calc", "approve -.-> answer"):
        assert edge in m


# ── 2 days later: was the plan done? ───────────────────────────────────────────────────────

def test_plan_reminder_then_done_turns_it_into_a_spray_with_a_7_day_check(bot, farm_data):
    from app.jobs import farm_jobs
    from app.config import get_settings
    plan_ready(bot)
    bot.tap(SAGAR, "fs:save")
    with session_scope() as s:
        s.execute(update(JournalEntry).where(JournalEntry.id == 4).values(entry_date=ago(2)))
    assert farm_jobs.plan_reminders(get_settings(), bot.tg, bot.llm) == "1 due, 1 asked"
    assert "Was this spray done?" in bot.tg.sent[-1]["text"]
    assert bot.tg.sent[-1]["buttons"] == farm_salah.plan_buttons(4)
    # Asked once only.
    assert farm_jobs.plan_reminders(get_settings(), bot.tg, bot.llm) == "0 due, 0 asked"
    r = bot.tap(BIREN, "pl:4:y")
    assert "J-4 is now a spray done on" in r
    e = entry(4)
    assert (e.activity, e.entry_date, e.follow_up) == ("spray", TODAY, "")
    assert "no longer a waiting plan" in bot.tap(BIREN, "pl:4:y")       # a second tap changes nothing


def test_plan_not_done_stays_a_plan(bot, farm_data):
    plan_ready(bot)
    bot.tap(SAGAR, "fs:save")
    r = bot.tap(SAGAR, "pl:4:n")
    assert "stays a plan" in r
    assert (entry(4).activity, entry(4).follow_up) == ("planned", "skipped")


def test_a_plan_is_not_a_spray_for_last_spray_questions(bot, farm_data):
    plan_ready(bot)
    bot.tap(SAGAR, "fs:save")
    from app.services import farm
    with session_scope() as s:
        assert all(e.activity != "planned" for e in farm.query(s, activity="spray"))
        assert farm.followups_due(s, TODAY + timedelta(days=8)) == []


def test_cleanup_removes_only_abandoned_graphs(bot, farm_data):
    from app.jobs import farm_jobs
    from app.config import get_settings
    plan_ready(bot)
    assert farm_jobs.salah_cleanup(get_settings(), bot.tg, bot.llm) == "removed 0 abandoned plans"
    with session_scope() as s:
        s.execute(update(PendingAction).where(PendingAction.kind == "salah")
                  .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert farm_jobs.salah_cleanup(get_settings(), bot.tg, bot.llm) == "removed 1 abandoned plans"
    assert checkpoints() == 0


def test_the_model_cannot_pick_planned_for_a_normal_note():
    from app.ai import farm_tools
    log_spec = next(s for s in farm_tools.specs() if s.name == farm_tools.LOG)
    assert "planned" not in log_spec.parameters["properties"]["activity"]["enum"]
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 0


def test_old_save_button_while_trees_are_asked_does_not_break_the_plan(bot, farm_data):
    plan_ready(bot, "Kesar par mor kala che. Spray plan banao.", trees=None)
    assert "First send the number of trees" in bot.tap(SAGAR, "fs:save")
    assert "500 ml" in bot.send(SAGAR, "100")


# ── 11-11 live: "chhelle kyare paani aapyu? e divase majuri ketli?" ────────────────────────

WATER_Q = "Chhelle kyare paani aapyu? E divase majuri no kharch ketlo thayo?"


@pytest.fixture
def watered(bot):
    day = ago(1)
    seed(dict(date=ago(9), activity="irrigation", note="drip thi paani aapyu"),
         dict(date=day, activity="irrigation", note="Mango ne drip thi paani aapyu, 2 kalak"))
    sync(bot.llm)
    add_rows(("Lalo", 500, "majuri"), when=day)
    add_rows(("Sagar", 200, "diesel"), when=day)
    add_rows(("Biren", 900, "majuri"), when=ago(9))
    return day


def test_same_day_for_watering_counts_only_the_expense_asked_about(bot, watered):
    from app.core.formatting import fmt_date
    bot.llm.script(salah("same_day", topic="paani", expense_words="majuri"), graded(1, 2))
    r = bot.send(SAGAR, WATER_Q)
    d = fmt_date(watered)
    assert f"📒 Watering — {d} (J-2)" in r                    # the newest watering, by name
    assert f"💰 Majuri expenses on {d}:" in r
    assert "• #1 — ₹500 — majuri — Lalo" in r and "💵 Total: ₹500" in r
    assert "diesel" not in r and "₹900" not in r
    assert "(1 other expense that day — ask without the word to see all)" in r


def test_expense_word_the_user_did_not_write_is_dropped(bot, watered):
    # The model adds "diesel"; the user never wrote it -> no filter, every expense of the day.
    bot.llm.script(salah("same_day", topic="paani", expense_words="diesel"), graded(1, 2))
    r = bot.send(SAGAR, "Chhelle kyare paani aapyu? E divase kharch ketlo thayo?")
    assert "₹500 — majuri" in r and "₹200 — diesel" in r and "💵 Total: ₹700" in r


def test_expense_word_with_nothing_that_day_says_so_and_points_to_the_rest(bot, watered):
    bot.llm.script(salah("same_day", topic="paani", expense_words="khatar"), graded(1, 2))
    r = bot.send(SAGAR, "Chhelle kyare paani aapyu? E divase khatar no kharch?")
    assert "💰 No khatar expense written on" in r
    assert "(2 other expenses that day" in r


def test_routing_rules_name_the_observation_and_the_watering_shape():
    from app.ai import farm_tools
    rules = farm_tools.SYSTEM_RULES
    assert "limbu na pan pila padya che" in rules and "never a search" in rules
    assert "e divase majuri ketli thai" in farm_salah.SPEC.description
    assert "expense_words" in farm_salah.SPEC.parameters["properties"]
    acts = next(s for s in farm_tools.specs() if s.name == farm_tools.ACTIVITIES_Q)
    assert "that is farm_salah" in acts.description


def test_lookup_heading_has_no_search_words(bot, watered):
    bot.llm.script(call("get_farm_activities", activity="irrigation", last_only=True))
    r = bot.send(SAGAR, "Chhelle kyare drip thi paani aapyu?")
    first = r.splitlines()[0]
    assert first.startswith("📒 Last Watering") and "J-2" in r
    assert "paani" not in first and "drip" not in first and "/" not in first


def test_lookup_not_found_names_what_was_searched(bot):
    bot.llm.script(call("get_farm_activities", product="termite"))
    r = bot.send(SAGAR, "termite kyare karyu?")
    assert r.startswith("📒 No farm note found") and "for: termite" in r
