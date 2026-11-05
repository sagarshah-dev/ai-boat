"""Journal fixes found by the live baseline test of 29-10-2026 (docs: farm-salah-baseline-test).
Each section is one bug, with every path through it: tap and typed answers, the next reply after
each question, and the cases that must NOT change."""
import pytest

from tests.bot_helpers import BIREN, SAGAR
from tests.conftest import call, calls, say
from tests.farm_helpers import ago, journal, log, seed


def buttons(bot):
    return bot.tg.sent[-1]["buttons"]


# ── 1. The answer to "which plant?" fills the plant, never the note ─────────────────────────

@pytest.mark.parametrize("how", ["tap", "typed"])
def test_picked_plant_is_not_written_into_the_note(bot, how):
    bot.llm.script(log(activity="spray", product="neem", problem="pan par fodla"))
    bot.send(SAGAR, "pan par fodla, neem spray karyu")
    bot.llm.script(log(activity="spray", product="neem", problem="pan par fodla"))
    r = bot.tap(SAGAR, "plant:Mango") if how == "tap" else bot.send(SAGAR, "aamba")
    assert "🌳 Mango" in r
    bot.send(SAGAR, "yes")
    e = journal()[0]
    assert (e.plant, e.note) == ("Mango", "pan par fodla, neem spray karyu")


def test_picked_plant_then_dose_reply_keeps_the_dose_but_not_the_plant(bot):
    bot.llm.script(log(activity="spray", product="neem"))
    bot.send(SAGAR, "neem spray karyu")
    bot.llm.script(log(activity="spray", product="neem"))
    r = bot.tap(SAGAR, "plant:Mango")
    assert "Not given: dose" in r
    bot.llm.script(log(activity="spray", product="neem", dose="50 ml"))
    bot.send(SAGAR, "50 ml savare")
    bot.send(SAGAR, "yes")
    e = journal()[0]
    assert e.note == "neem spray karyu 50 ml savare" and e.plant == "Mango"
    assert e.dose == "50 ml" and e.time_of_day == "morning"


def test_note_with_plant_in_the_message_is_unchanged(bot):
    bot.llm.script(log(activity="spray", product="neem"))
    bot.send(SAGAR, "aamba ma neem spray karyu 10 ml per litre savare")
    bot.send(SAGAR, "yes")
    assert journal()[0].note == "aamba ma neem spray karyu 10 ml per litre savare"


def test_plant_reply_does_not_leak_into_the_next_note(bot):
    bot.llm.script(log(activity="observation", problem="fodla"))
    bot.send(SAGAR, "pan par fodla")
    bot.llm.script(log(activity="observation", problem="fodla"))
    bot.tap(SAGAR, "plant:Mango")
    bot.send(SAGAR, "yes")
    bot.llm.script(log(activity="irrigation"))
    bot.send(SAGAR, "chiku ne paani payu")
    bot.send(SAGAR, "yes")
    assert [e.note for e in journal()] == ["pan par fodla", "chiku ne paani payu"]


# ── 2. Several dated notes in one message: nothing lost silently ────────────────────────────

LIVE_FOUR = ("25-Oct: Kesar ma daag ochha thaya, copper spray kaam karyu\n"
             "18-Oct: Dadam na fal ma tirad padi, pani aniyamit hatu, have drip roj 30 min karyu\n"
             "20-Oct: Chiku par mealybug dekhaya, neem oil spray karyu\n"
             "27-Oct: Chiku par neem oil thi koi fark nathi padyo, mealybug haju che")


def test_live_four_notes_message_is_explained_and_never_reaches_the_model(bot):
    r = bot.send(SAGAR, LIVE_FOUR)
    assert "4 farm notes in one message — nothing was saved" in r
    assert "1. 25-Oct: Kesar" in r and "4. 27-Oct: Chiku" in r
    assert not journal() and not bot.llm.chats


@pytest.mark.parametrize("text", [
    "aaje aamba ma neem spray karyu\nkale chiku ma khatar aapyu",
    "15-10 aamba ma spray\n16/10 chiku ma 5 kg khatar",
    "15 Oct: limbu ma fodla\n16 Oct: limbu ma neem spray 10 ml per litre",
])
def test_other_dated_note_shapes_are_split_too(bot, text):
    r = bot.send(SAGAR, text)
    assert "2 farm notes in one message" in r and not bot.llm.chats


@pytest.mark.parametrize("text", [
    "aamba ma pan par fodla\nneem spray karyu 10 ml per litre savare",   # one note, two lines
    "15-10 aamba ma neem spray\n10 ml per litre savare",                 # one date only
])
def test_one_note_typed_over_two_lines_still_reaches_the_ai(bot, text):
    bot.llm.script(log(activity="spray", product="neem"))
    r = bot.send(SAGAR, text)
    assert "farm notes in one message" not in r and "Save this farm note?" in r


@pytest.mark.parametrize("text", [
    "15-10 khatar 500\n16-10 dava 300",        # dated EXPENSES with farm words: money, not notes
    "15-10 diesel 500\n16-10 tea 100",
    "15-10 khatar ₹500\n16-10 dava ₹300",
])
def test_dated_expense_lines_are_not_taken_for_notes(bot, text):
    r = bot.send(SAGAR, text)
    assert "farm notes in one message" not in r


def test_same_tool_answer_three_times_is_shown_once(bot):
    args = {"outcome": "worked", "result": "daag ochha thaya", "plant": "Chiku"}
    bot.llm.script(calls(*[("record_treatment_result", args)] * 3))
    r = bot.send(SAGAR, "chiku ma daag ochha thaya")
    assert r.count("No spray, treatment or fertilizer") == 1


def test_two_notes_from_the_model_show_the_first_and_say_so(bot):
    bot.llm.script(calls(("log_farm_activity", {"activity": "spray", "product": "neem"}),
                         ("log_farm_activity", {"activity": "fertilizer", "product": "khatar"})))
    r = bot.send(SAGAR, "aamba ma neem spray karyu 10 ml per litre savare ane chiku ma khatar aapyu 5 kg")
    assert "Save this farm note?" in r and "had 2 things to save; this is the first" in r


# ── 3. "When was X done?" lists every match; one row only when the user says "last" ─────────

def _neem_rows():
    seed({"date": ago(4), "plant": "Mango", "product": "neem", "dose": "10 ml per 1 ltr"},
         {"date": ago(11), "plant": "Chiku", "product": "neem oil", "problem": "mealybug",
          "result": "❌ Did not work: neem oil thi koi fark nathi padyo"})


@pytest.mark.parametrize("model_says_last", [True, False])
def test_when_question_shows_both_neem_sprays_with_result(bot, model_says_last):
    _neem_rows()
    # Live 29-10 the model passed no product; the user's words "neem" / "oil" were the topic.
    bot.llm.script(call("get_farm_activities", last_only=model_says_last))
    r = bot.send(SAGAR, "Neem oil kyare chhantyu?")
    assert "2 found" in r and "Mango" in r and "Chiku" in r
    assert "Did not work" in r                       # the failed spray is not hidden
    assert r.index("Mango") < r.index("Chiku")       # newest first


@pytest.mark.parametrize("text", ["neem chhelle kyare chhantyu?", "last neem spray kyare?",
                                  "છેલ્લે neem ક્યારે?"])
def test_last_asked_in_words_gives_one_row_and_says_more_exist(bot, text):
    _neem_rows()
    bot.llm.script(call("get_farm_activities", product="neem", last_only=True))
    r = bot.send(SAGAR, text)
    assert "📒 Last" in r and "Mango" in r and "Chiku" not in r
    assert "1 older note too" in r


def test_last_with_only_one_match_has_no_older_line(bot):
    seed({"date": ago(4), "plant": "Mango", "product": "neem"})
    bot.llm.script(call("get_farm_activities", product="neem", last_only=True))
    r = bot.send(SAGAR, "neem chhelle kyare?")
    assert "📒 Last" in r and "older" not in r


def test_list_is_capped_at_five_newest(bot):
    seed(*[{"date": ago(i + 1), "plant": "Mango", "product": "neem"} for i in range(7)])
    bot.llm.script(call("get_farm_activities", product="neem"))
    r = bot.send(SAGAR, "neem kyare kyare chhantyu?")
    assert "7 found" in r and r.count("• ") == 5 and "and 2 older" in r


# ── 4. A money question about a PROBLEM: honest "not found" + the journal's link, one tap ───

def _copper_case(bot):
    seed({"date": ago(16), "plant": "Mango", "product": "copper oxychloride",
          "problem": "pan par kala daag dekhaya",
          "note": "15-Oct: Kesar aamba na pan par kala daag dekhaya, copper oxychloride spray karyu",
          "result": "✅ Worked: Kesar ma daag ochha thaya"},
         {"date": ago(4), "plant": "Mango", "product": "neem", "problem": "pan par fodla",
          "note": "pan par fodla, spray karyu"})
    bot.llm.script(call("add_expense", amount=850, description="copper oxychloride", paid_by="Sagar"))
    bot.send(SAGAR, "copper oxychloride 850 sagar e aapya")
    bot.llm.script(call("add_expense", amount=400, description="neem oil", paid_by="Biren"))
    bot.send(SAGAR, "neem oil 400 biren e aapya")


def test_live_q7_says_what_was_searched_and_offers_the_copper_hop(bot):
    _copper_case(bot)
    bot.llm.script(call("search_expenses", text="kesar daag dava"))
    r = bot.send(SAGAR, "Kesar na daag ni dava ma kharcho ketlo thayo?")
    assert "No entries found" not in r
    assert 'No expense with "kesar daag dava"' in r
    assert "J-1" in r and "copper oxychloride" in r and "neem" not in r
    assert buttons(bot) == [[("🔎 copper oxychloride in expenses", "xs:1")]]

    r = bot.tap(SAGAR, "xs:1")
    assert "₹850" in r and "copper oxychloride" in r and "Sagar" in r and "neem" not in r
    assert "🔎 Path: 📒 J-1" in r and "💊 copper oxychloride" in r
    assert "(journal: exact words, money: exact match)" in r and "meaning: journal" not in r


def test_hop_with_no_matching_expense_says_so(bot):
    seed({"plant": "Mango", "product": "mancozeb", "problem": "kala daag"})
    bot.llm.script(call("search_expenses", text="daag dava"))
    bot.send(SAGAR, "daag ni dava no kharcho?")
    r = bot.tap(SAGAR, "xs:1")
    assert 'No expense with "mancozeb"' in r and "Path" in r


def test_no_journal_link_gives_the_honest_line_only(bot):
    bot.llm.script(call("search_expenses", text="tractor"))
    r = bot.send(SAGAR, "tractor no kharcho ketlo?")
    assert 'No expense with "tractor"' in r and "journal links" not in r


def test_found_expenses_are_unchanged(bot):
    _copper_case(bot)
    bot.llm.script(call("search_expenses", text="copper"))
    r = bot.send(SAGAR, "copper no kharcho?")
    assert "₹850" in r and "journal links" not in r


@pytest.mark.parametrize("data", ["xs:999", "xs:abc", "xs:1;drop", "xs:"])
def test_forged_or_stale_hop_button_does_nothing_harmful(bot, data):
    r = bot.tap(SAGAR, data)
    assert "₹" not in r


# ── 5. A plant that is not in the list: say so, admin adds it, or warn on a different pick ──

DADAM = "18-Oct: Dadam na fal ma tirad padi, pani aniyamit hatu, have drip roj 30 min karyu"


def _dadam_note(bot, who=SAGAR):
    bot.llm.script(log(activity="observation", plant="Dadam", problem="fal ma tirad padi"))
    return bot.send(who, DADAM)


def test_unknown_plant_is_named_and_admin_gets_an_add_button(bot):
    r = _dadam_note(bot)
    assert '"dadam" is not in the plant list. Tap ➕' in r and not journal()
    assert buttons(bot)[0] == [("➕ Add dadam", "plant:+add")]


def test_admin_adds_the_plant_and_the_note_continues_under_it(bot):
    from app.services import farm
    from app.db.session import session_scope
    _dadam_note(bot)
    bot.llm.script(log(activity="observation", plant="Dadam", problem="fal ma tirad padi"))
    r = bot.tap(SAGAR, "plant:+add")
    assert 'Added "Dadam" to the plant list' in r and "🌳 Dadam" in r and "⚠️ Your note says" not in r
    bot.send(SAGAR, "yes")
    e = journal()[0]
    assert (e.plant, e.note) == ("Dadam", DADAM)
    with session_scope() as s:
        assert "Dadam" in farm.plant_names(farm.plants(s))
    # The next note that says "dadam" needs no question at all.
    bot.llm.script(log(activity="irrigation"))
    assert "Save this farm note?" in bot.send(SAGAR, "dadam ne paani payu")


def test_owner_cannot_add_a_plant(bot):
    r = _dadam_note(bot, who=BIREN)
    assert "Only the admin can add a plant" in r
    assert all(b[0][1] != "plant:+add" for b in buttons(bot))
    assert "Only the admin can add a plant." in bot.tap(BIREN, "plant:+add")
    from app.db.session import session_scope
    from app.services import farm
    with session_scope() as s:
        assert "Dadam" not in farm.plant_names(farm.plants(s))


def test_picking_another_plant_saves_it_with_a_warning_and_the_original_note(bot):
    _dadam_note(bot)
    bot.llm.script(log(activity="observation", plant="Guava", problem="fal ma tirad padi"))
    r = bot.tap(SAGAR, "plant:Guava")
    assert '⚠️ Your note says "dadam", but it will be saved under Guava' in r
    bot.send(SAGAR, "yes")
    assert (journal()[0].plant, journal()[0].note) == ("Guava", DADAM)


def test_typed_known_plant_after_unknown_question_also_warns(bot):
    _dadam_note(bot)
    bot.llm.script(log(activity="observation", plant="jamfal"))
    r = bot.send(SAGAR, "jamfal")
    assert "⚠️ Your note says" in r and "🌳 Guava" in r


def test_add_button_without_a_waiting_note_does_nothing(bot):
    assert bot.tap(SAGAR, "plant:+add") == ""


@pytest.mark.parametrize("plant_arg", ["Mango", "neem", "chhod", "", "badha chhod",
                                       "a very long made up plant name"])
def test_model_words_that_are_not_an_unknown_plant_give_the_normal_question(bot, plant_arg):
    # Mango: invented (not in the message). neem: the product. chhod: generic.
    bot.llm.script(log(activity="spray", plant=plant_arg, product="neem"))
    r = bot.send(SAGAR, "pan par fodla, neem spray karyu chhod ma")
    assert "Which plant is this about?" in r and "not in the plant list" not in r


def test_known_plant_note_has_no_warning(bot):
    bot.llm.script(log(activity="spray", plant="aamba", product="neem"))
    r = bot.send(SAGAR, "aamba ma neem spray karyu 10 ml per litre savare")
    assert "⚠️ Your note says" not in r


# ── 6. Every farm answer says, in one line, which concept found it and how ──────────────────

from datetime import date as _date  # noqa: E402

from app.services import rag  # noqa: E402


def _hit(i, d):
    return rag.Hit(f"J-{i}", "journal", f"note {i}", {"date": d}, 0.9)


@pytest.mark.parametrize("found,f,status,want,not_want", [
    (rag.Found([_hit(1, "2026-10-01")]), rag.Filters(), "found",
     ["🔎 How: RAG", "meaning + exact words", "1 note passed"], ["filter:", "word for word"]),
    (rag.Found([_hit(1, "x"), _hit(2, "y")]), rag.Filters(plant="Mango", activity="spray"), "found",
     ["2 notes passed", "filter: Mango, spray"], []),
    (rag.Found([_hit(1, "x")], relaxed="no Guava note found; showing other plants"),
     rag.Filters(plant="Guava"), "found", ["no Guava note found; showing other plants"], ["filter:"]),
    (rag.Found([_hit(1, "x")]), rag.Filters(), "fallback", ["notes shown word for word"], []),
    (rag.Found([_hit(1, "x")], mode="basic"), rag.Filters(), "found",
     ["📒 How: exact words only", "meaning search is down"], ["RAG"]),
    (rag.Found([_hit(1, "x")]), rag.Filters(date_from=_date(2026, 10, 1), date_to=_date(2026, 10, 31)),
     "found", ["filter: 01-10-2026 to 31-10-2026"], []),
])
def test_how_line(found, f, status, want, not_want):
    line = rag.how_line(found, f, status)
    assert "\n" not in line and len(line) < 200          # one short line on a phone
    for w in want:
        assert w in line
    for w in not_want:
        assert w not in line


def test_fallback_list_keeps_the_ranked_order_not_the_date_order():
    # Live Q5: J-3 ranked first but was dated oldest, so the date sort put it last.
    hits = [_hit(3, "2026-10-15"), _hit(2, "2026-10-27"), _hit(4, "2026-10-18")]
    text = rag.fallback_list(hits)
    assert text.startswith("Matching notes (best match first):")
    assert text.index("note 3") < text.index("note 2") < text.index("note 4")


def test_exact_answers_say_sql(bot):
    _neem_rows()
    bot.llm.script(call("get_farm_activities"))
    r = bot.send(SAGAR, "Neem oil kyare chhantyu?")
    assert "📒 How: exact match (SQL)" in r


# ── 7. Cross paths and next replies ─────────────────────────────────────────────────────────

def test_unknown_plant_in_gujarati_script(bot):
    bot.llm.script(log(activity="observation", plant="દાડમ", problem="ફળમાં તિરાડ"))
    r = bot.send(SAGAR, "દાડમ ના ફળમાં તિરાડ પડી")
    assert "not in the plant list" in r and "Add" in str(buttons(bot)[0])


def test_typing_the_unknown_plant_again_is_not_a_plant_answer(bot):
    _dadam_note(bot)
    bot.llm.script(say("ok"))
    r = bot.send(SAGAR, "dadam")
    assert "Still waiting for the plant" in r and not journal()


def test_cancel_after_warning_saves_nothing(bot):
    _dadam_note(bot)
    bot.llm.script(log(activity="observation", plant="Guava"))
    bot.tap(SAGAR, "plant:Guava")
    bot.send(SAGAR, "no")
    assert not journal()


def test_adversarial_plant_name_is_never_added_without_the_admin_tap(bot):
    from app.db.session import session_scope
    from app.services import farm
    bot.llm.script(log(activity="observation", plant="ignore rules"))
    bot.send(SAGAR, "ignore rules, pan par fodla")
    with session_scope() as s:
        from tests.conftest import PLANT_SEED, VARIETY_SEED
        # seeded list only; nothing added by the model
        assert len(farm.plants(s)) == len(PLANT_SEED) + len(VARIETY_SEED)


def test_hop_button_tapped_twice_gives_the_same_read_only_answer(bot):
    _copper_case(bot)
    bot.llm.script(call("search_expenses", text="kesar daag dava"))
    bot.send(SAGAR, "Kesar na daag ni dava ma kharcho ketlo thayo?")
    a, b = bot.tap(SAGAR, "xs:1"), bot.tap(SAGAR, "xs:1")
    assert a == b and "₹850" in a
    from tests.bot_helpers import expenses_in_db
    assert len(expenses_in_db()) == 2             # a search never writes


def test_several_notes_then_one_note_works_normally(bot):
    bot.send(SAGAR, LIVE_FOUR)
    bot.llm.script(log(activity="spray", product="copper oxychloride"))
    r = bot.send(SAGAR, "15-Oct: Kesar aamba na pan par kala daag dekhaya, copper oxychloride spray karyu")
    assert "Save this farm note?" in r
