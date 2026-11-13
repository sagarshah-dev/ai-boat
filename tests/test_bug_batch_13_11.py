"""Live bugs of 06-11 and 13-11-2026, fixed in one batch. Each test names the live case.

Farm Salah: a plan with no problem named asks for it (and every next reply is handled); a dose the
user writes is reported, not silently replaced; a plan answer can be saved as a farm note.
Expenses: the one amount the user wrote is offered for a tap when the model gives none; "sorry"
is not an item. Journal: the expense hop and "last" need the whole problem, not one shared word;
a note whose indexing failed is found on the next search; stop words; How lines.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.ai import agent, farm_salah, farm_tools
from app.db.models import JournalEntry, PendingAction, RagChunk
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from app.services import pending, rag, rag_index
from tests.bot_helpers import BIREN, SAGAR, add_rows, expenses_in_db
from tests.conftest import call, say
from tests.farm_helpers import ago, entry, journal, log, seed, seed_knowledge, sync
from tests.test_farm_salah import (DOSE_TEXT, E1, OCT15, checkpoints, dose_form, farm_data,  # noqa: F401
                                   graded, salah, waiting_rows)

NO_PROBLEM = "Kesar na 100 zhad mate plan banao"


def buttons(bot):
    return bot.tg.sent[-1]["buttons"]


def _advice(bot):
    seed_knowledge("Hexaconazole dose for mango tree", DOSE_TEXT, source="text")
    sync(bot.llm)


def _ask_problem(bot, message=NO_PROBLEM):
    _advice(bot)
    bot.llm.script(salah("plan", topic="kesar", trees=100))
    return bot.send(SAGAR, message)


# ── 1. A plan with no problem named: ask, then every next reply ─────────────────────────────

def test_plan_without_a_problem_asks_which_problem_instead_of_no_note(bot, farm_data):
    r = _ask_problem(bot)
    assert "No note about" not in r
    assert "🌱 For which problem?" in r
    # Buttons: the problems already in the journal for Kesar, newest first, by note id.
    assert buttons(bot) == [[("pan par kala daag", "fs:t:3")], [("mor kala", "fs:t:2")]]
    assert [w.payload["waiting"] for w in waiting_rows()] == ["topic"]
    assert checkpoints() > 0
    assert not bot.llm.completions or all(sysmsg != farm_salah.GRADE_RULES
                                          for sysmsg, _ in bot.llm.completions)


def test_typed_problem_completes_the_plan_and_the_check_sees_it(bot, farm_data):
    _ask_problem(bot)
    bot.llm.script(graded(1, 2), dose_form())
    r = bot.send(SAGAR, "mor kala")
    assert "🧮 100 trees × 5 L = 500 L water → 500 ml hexaconazole" in r
    grade_prompt = next(p for sysmsg, p in bot.llm.completions if sysmsg == farm_salah.GRADE_RULES)
    assert f"{NO_PROBLEM} — mor kala" in grade_prompt
    assert [w.payload["waiting"] for w in waiting_rows()] == ["approve"]


def test_tapped_problem_uses_the_notes_own_words(bot, farm_data):
    _ask_problem(bot)
    bot.llm.script(graded(1, 2), dose_form())
    r = bot.tap(SAGAR, "fs:t:2")
    assert "🌱 Spray plan — Mango (Kesar) · mor kala · 100 trees" in r


def test_while_the_problem_is_asked_other_messages_are_handled_normally(bot, farm_data):
    _ask_problem(bot)
    assert "Balance" in bot.send(SAGAR, "balance")
    bot.llm.script(call("add_expense", amount=100, description="tea", paid_by="Sagar"))
    assert "Logged" in bot.send(SAGAR, "100 tea")
    # "yes" is not a problem, and not a ✅ for a plan that does not exist yet.
    assert "500 ml" not in bot.send(SAGAR, "yes")
    assert [w.payload["waiting"] for w in waiting_rows()] == ["topic"]
    assert "First send the problem" in bot.tap(SAGAR, "fs:save")
    # Words the journal does not contain are not taken as the problem.
    bot.llm.script(say("Kem cho?"))
    bot.send(SAGAR, "tractor")
    assert [w.payload["waiting"] for w in waiting_rows()] == ["topic"]
    bot.llm.script(graded(1, 2), dose_form())
    assert "500 ml" in bot.send(SAGAR, "mor kala")


def test_another_member_cannot_answer_the_problem_question(bot, farm_data):
    _ask_problem(bot)
    assert farm_salah.EXPIRED in bot.tap(BIREN, "fs:t:2")
    bot.llm.script(say("ok"))
    bot.send(BIREN, "mor kala")                      # Biren's own message, handled normally
    assert [w.payload["waiting"] for w in waiting_rows()] == ["topic"]


def test_problem_question_expires_like_a_plan(bot, farm_data):
    _ask_problem(bot)
    with session_scope() as s:
        s.execute(update(PendingAction).where(PendingAction.kind == "salah")
                  .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert farm_salah.EXPIRED in bot.tap(SAGAR, "fs:t:2")


def test_problem_named_is_not_asked(bot, farm_data):
    _advice(bot)
    bot.llm.script(salah("plan", trees=100), graded(1, 2), dose_form())
    assert "500 ml" in bot.send(SAGAR, E1)


def test_same_day_with_no_work_named_asks_which_work(bot, farm_data):
    # Live 13-11 M1: "E divase biju kai kharch thayo?" -> 'No note about "kharch"'.
    add_rows(("Lalo", 300, "majuri"), when=OCT15)
    bot.llm.script(salah("same_day", topic="kharch"))
    r = bot.send(SAGAR, "E divase biju kai kharch thayo?")
    assert "No note about" not in r and "📅 Which farm work do you mean?" in r
    assert ("copper oxychloride", "fs:t:3") in [b for row in buttons(bot) for b in row]
    bot.llm.script(graded(3))
    r = bot.send(SAGAR, "copper")
    assert "📒 copper oxychloride — 15-10-2026 (J-3)" in r and "₹300 — majuri — Lalo" in r


def test_has_subject_rules():
    from app.db.session import session_scope as sc
    from app.services import farm
    with sc() as s:
        known = farm.plants(s)
        sub = lambda need, topic, act="": farm_salah.has_subject(  # noqa: E731
            s, known, {"need": need, "topic": topic, "activity": act})
        assert sub("plan", "mor kala") and sub("purchase", "copper")
        assert not sub("plan", "kesar") and not sub("plan", "plan banao") and not sub("plan", "spray")
        assert sub("same_day", "paani") and sub("same_day", "", "irrigation")
        assert not sub("same_day", "kharch") and not sub("same_day", "biju kharch thayo")


# ── 2. The user's own dose is reported, never silently replaced ─────────────────────────────

def test_users_dose_that_differs_from_the_advice_is_said_out_loud(bot, farm_data):
    # Live 13-11: "2 ml thi" -> a 1 ml plan with no word about the 2 ml.
    _advice(bot)
    bot.llm.script(salah("plan", trees=100), graded(1, 2), dose_form())
    r = bot.send(SAGAR, "Kesar na 100 zhad par mor kala che, 2 ml thi spray plan banao")
    assert "500 ml hexaconazole" in r
    assert "ℹ️ You wrote 2 ml; the farmer's advice K-1 says 1 ml per litre, so the plan uses 1 ml." in r


def test_users_dose_equal_to_the_advice_adds_nothing(bot, farm_data):
    _advice(bot)
    bot.llm.script(salah("plan", trees=100), graded(1, 2), dose_form())
    r = bot.send(SAGAR, "Kesar na 100 zhad par mor kala che, 1 ml thi spray plan banao")
    assert "You wrote" not in r


def test_users_dose_with_no_advice_dose_says_how_to_use_it(bot, farm_data):
    bot.llm.script(salah("plan", trees=100), graded(1, 2))
    r = bot.send(SAGAR, "Kesar na 100 zhad par mor kala che, 2 ml thi spray plan banao")
    assert "not written in the farmer's advice" in r and "ℹ️ You wrote 2 ml." in r
    assert "🧮" not in r


# ── 3. 📝 Save as a farm note, from a plan answer ───────────────────────────────────────────

def test_plan_answer_can_be_saved_as_an_observation_and_the_plan_keeps_waiting(bot, farm_data):
    # Owner 13-11: "Kesar par mor kala che" -> a plan is right, but saving must be one tap away.
    _advice(bot)
    bot.llm.script(salah("plan"), graded(1, 2), dose_form())
    r = bot.send(SAGAR, "Kesar par mor kala che")
    assert "How many trees?" in r and buttons(bot) == [[farm_salah.NOTE_BUTTON]]
    r = bot.tap(SAGAR, "fs:note")
    assert "🌱 Save this farm note?" in r and "Mango (Kesar)" in r and "mor kala" in r
    r = bot.send(SAGAR, "yes")
    assert r.startswith("✅ Saved farm note J-4")
    e = entry(4)
    assert (e.activity, e.variety, e.problem) == ("observation", "Kesar", "mor kala")
    # The plan question is still there: "100" completes it.
    assert [w.payload["waiting"] for w in waiting_rows()] == ["trees"]
    assert "500 ml" in bot.send(SAGAR, "100")


def test_note_button_is_also_under_no_notes_and_belongs_to_the_asker(bot):
    bot.llm.script(salah("plan", topic="mor kala", trees=100))     # no notes: no check is needed
    r = bot.send(SAGAR, "Kesar na 100 zhad par mor kala che. Spray plan banao.")
    assert "No note about" in r and buttons(bot) == [[farm_salah.NOTE_BUTTON]]
    assert "Nothing waiting to be saved" in bot.tap(BIREN, "fs:note")
    assert "Save this farm note?" in bot.tap(SAGAR, "fs:note")
    assert "Nothing waiting to be saved" in bot.tap(SAGAR, "fs:note")   # one preview per tap


def test_no_note_button_after_save_or_for_expense_questions(bot, farm_data):
    _advice(bot)
    bot.llm.script(salah("plan", trees=100), graded(1, 2), dose_form())
    bot.send(SAGAR, E1)
    bot.tap(SAGAR, "fs:save")
    assert not buttons(bot)
    bot.llm.script(salah("purchase"), graded(2))
    bot.send(SAGAR, "Kesar par mor kala padya tyare kai dava kaam kari, ane e dava chhelle kyare kharidi?")
    assert not buttons(bot)


# ── 4. Expenses: the amount the user wrote; "sorry" is not an item ──────────────────────────

def test_model_without_amount_gets_the_written_one_offered_for_a_tap(bot):
    # Live 13-11: "Biren e 100 tea lidhi" -> "how much was it?"
    bot.llm.script(call("add_expense", amount=0, description="tea", paid_by="Biren"))
    r = bot.send(SAGAR, "Biren e 100 tea lidhi")
    assert "how much was it?" not in r
    assert "❓ Is this right?" in r and "₹100 — tea — Biren" in r
    assert expenses_in_db() == []
    assert "Logged" in bot.send(SAGAR, "yes")
    assert [(int(e.amount), e.description) for e in expenses_in_db()] == [(100, "tea")]


def test_offered_amount_is_never_saved_on_no(bot):
    bot.llm.script(call("add_expense", amount=0, description="tea", paid_by="Biren"))
    bot.send(SAGAR, "Biren e 2 tea lidhi")          # "2" may be two cups: one tap decides
    bot.send(SAGAR, "no")
    assert expenses_in_db() == []


def test_no_number_written_still_asks(bot):
    bot.llm.script(call("add_expense", amount=0, description="tea", paid_by="Biren"))
    assert "how much was it?" in bot.send(SAGAR, "Biren e tea lidhi")


def test_sorry_is_not_an_expense(bot):
    # Live 13-11: "sorry 150" was saved as "₹150 for sorry".
    bot.llm.script(call("add_expense", amount=150, description="sorry", paid_by="Sagar"))
    r = bot.send(SAGAR, "sorry 150")
    assert expenses_in_db() == [] and "what was ₹150 for?" in r


def test_not_an_item_words():
    from app.ai.guards import not_an_item
    assert not_an_item("sorry") and not_an_item("Sorry ok") and not_an_item("galti")
    assert not not_an_item("tea") and not not_an_item("sorry chips") and not not_an_item("")


# ── 5. The journal: whole problem, not one shared word ──────────────────────────────────────

def _kala_notes():
    seed(dict(date=ago(30), variety="Kesar", product="hexaconazole", problem="mor kala",
              note="Kesar mor kala, hexaconazole", result="✅ Worked: new flowers healthy"),
         dict(date=ago(10), variety="Kesar", product="copper oxychloride",
              problem="pan par kala daag", note="pan par kala daag, copper",
              result="✅ Worked: daag ochha thaya"))


def test_expense_hop_keeps_only_notes_the_check_calls_relevant(bot):
    # Live 06-11: "mor kala ... dava" offered J-3 copper (black spots on LEAVES).
    _kala_notes()
    bot.llm.script(call("search_expenses", text="dava"), graded(1))
    r = bot.send(SAGAR, "Kesar par mor kala ni dava no kharch ketlo?")
    assert "J-1" in r and "hexaconazole" in r and "copper" not in r
    assert buttons(bot) == [[("🔎 hexaconazole in expenses", "xs:1")]]
    prompt = next(p for sysmsg, p in bot.llm.completions if sysmsg == farm_salah.GRADE_RULES)
    assert "[J-1]" in prompt and "[J-2]" in prompt


def test_expense_hop_with_the_check_down_offers_nothing_but_says_what_was_searched(bot):
    _kala_notes()
    bot.llm.script(call("search_expenses", text="dava"), LlmError("quota"))
    r = bot.send(SAGAR, "Kesar par mor kala ni dava no kharch ketlo?")
    assert 'No expense with "dava"' in r and "journal links" not in r
    assert r.endswith(agent.HOW_SEARCH)


def test_last_picks_the_note_about_the_whole_problem_not_the_newest_shared_word(bot):
    # Live 06-11: "last" showed the newest note that shared a word.
    _kala_notes()
    bot.llm.script(call("get_farm_activities", last_only=True))
    r = bot.send(SAGAR, "Kesar par mor kala ma chhelle shu chhantyu?")
    assert "hexaconazole" in r and "copper" not in r


def test_a_list_still_shows_every_note_that_shares_a_word(bot):
    _kala_notes()
    bot.llm.script(call("get_farm_activities"))
    r = bot.send(SAGAR, "Kesar par kala ma shu chhantyu?")
    assert "hexaconazole" in r and "copper" in r


def test_new_stop_words_leave_only_what_the_question_is_about():
    with session_scope() as s:
        from app.services import farm
        known, stop = farm.plants(s), rag_index.stopwords(s)
        assert farm_tools.topic_words(
            known, "Je divase Kesar par copper chhantyu, e divase biju kai kharch thayo?", stop) == ["copper"]
        assert farm_tools.topic_words(
            known, "Mor kalo che, plan yaad che. Khali dava kaho ne last kyare lidhi ti?", stop) == ["mor", "kalo"]


# ── 6. A note whose indexing failed: said, and found on the next search ─────────────────────

def test_index_failure_is_said_and_the_next_search_indexes_the_note(bot):
    bot.llm.embed_error = LlmError("quota")
    bot.llm.script(log(activity="spray", plant="aamba", product="neem", dose="50 ml"))
    bot.send(SAGAR, "aamba ma neem 50 ml savare")
    r = bot.send(SAGAR, "yes")
    assert r.startswith("✅ Saved farm note J-1") and farm_tools.INDEX_LATE in r
    assert journal()[0].indexed_at is None
    bot.llm.embed_error = None
    with session_scope() as s:
        found = rag.retrieve(s, bot.llm, "neem", rag.Filters(), 0.3)
    assert journal()[0].indexed_at is not None
    assert any(h.id == "J-1" for h in found.hits)
    with session_scope() as s:
        assert s.scalar(select(RagChunk.id).where(RagChunk.row_id == 1)) is not None


def test_saved_note_with_indexing_working_says_nothing_extra(bot):
    bot.llm.script(log(activity="spray", plant="aamba", product="neem", dose="50 ml"))
    bot.send(SAGAR, "aamba ma neem 50 ml savare")
    assert farm_tools.INDEX_LATE not in bot.send(SAGAR, "yes")


def test_catch_up_does_nothing_when_everything_is_indexed(bot):
    seed(dict(note="neem spray"))
    sync(bot.llm)
    before = len(bot.llm.embedded)
    with session_scope() as s:
        assert rag_index.catch_up(s, bot.llm) == 0
    assert len(bot.llm.embedded) == before


# ── 7. Replies: How lines on money answers, a blank line before every How line ──────────────

def test_money_answers_say_how_they_were_found(bot):
    add_rows(("Sagar", 300, "diesel", "Fuel"))
    bot.llm.script(call("search_expenses", text="diesel"))
    assert bot.send(SAGAR, "diesel no kharch").endswith("\n\n" + agent.HOW_SEARCH)
    bot.llm.script(call("get_recent"))
    assert bot.send(SAGAR, "chhella kharch batav").endswith("\n\n" + agent.HOW_RECENT)


def test_farm_answers_have_a_blank_line_before_the_how_line(bot):
    seed(dict(product="neem", note="neem spray"))
    bot.llm.script(call("get_farm_activities"))
    r = bot.send(SAGAR, "neem kyare chhantyu?")
    assert "\n\n📒 How:" in r


def test_old_problem_button_does_nothing_once_the_plan_waits_for_approval(bot, farm_data):
    _ask_problem(bot)
    bot.llm.script(graded(1, 2), dose_form())
    bot.send(SAGAR, "mor kala")
    assert farm_salah.EXPIRED in bot.tap(SAGAR, "fs:t:2")
    assert [w.payload["waiting"] for w in waiting_rows()] == ["approve"]
    assert "Plan saved" in bot.tap(SAGAR, "fs:save")


def test_expense_hop_puts_the_treatment_that_worked_first(bot):
    seed(dict(date=ago(30), variety="Kesar", product="hexaconazole", problem="mor kala",
              note="mor kala, hexaconazole", result="✅ Worked: new flowers healthy"),
         dict(date=ago(5), variety="Kesar", product="wettable sulphur", problem="mor kala",
              note="mor kala, sulphur", result="❌ Did not work: flowers still black"))
    bot.llm.script(call("search_expenses", text="dava"), graded(1, 2))
    bot.send(SAGAR, "Kesar par mor kala ni dava no kharch ketlo?")
    assert buttons(bot) == [[("🔎 hexaconazole in expenses", "xs:1")],
                            [("🔎 wettable sulphur in expenses", "xs:2")]]
