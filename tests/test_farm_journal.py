"""Feature 5a: the farm journal — capture with checks, results, 7-day follow-ups, counts, exact
questions, photo / voice / menu routing. Ported from the old bot's test_farm.py (sections 1–4)
and test-farm-ui.js, plus the paths that only exist now (database state, group presser rules)."""
from datetime import date

import pytest
from sqlalchemy import select

from app.core.formatting import fmt_date
from app.db.models import PendingAction
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from app.services import farm
from app.telegram import farm_ui
from tests.bot_helpers import BIREN, GROUP, LALO, RAMESH, SAGAR, expenses_in_db
from tests.conftest import call, calls, say
from tests.farm_helpers import (TODAY, add_plant, ago, chunk_ids, entry, journal, log, seed,
                                sync)

T = fmt_date(TODAY)


def buttons(bot):
    return bot.tg.sent[-1]["buttons"]


def known():
    with session_scope() as s:
        return farm.plants(s)


# ── 1. Plants: what the USER named, from the plants table ───────────────────────────────────

@pytest.fixture
def varieties():
    add_plant("Mango", "Kesar", "kesar,kesri")
    add_plant("Mango", "Alphonso", "alphonso,hafus,hapus,aafus")


@pytest.mark.parametrize("text,plants,variety,all_farm", [
    ("aamba ma neem spray", ["Mango"], "", False),
    ("આંબા માં ફૂગ", ["Mango"], "", False),
    ("keri par daag", ["Mango"], "", False),
    ("hafus ma fodla", ["Mango"], "Alphonso", False),
    ("kesri aamba", ["Mango"], "Kesar", False),
    ("badha aamba ma neem", ["Mango"], "All", False),
    ("badha chhod ma humic acid aapyu", ["All"], "", True),
    ("aamba ane chiku ma spray", ["Mango", "Chiku"], "", False),
    ("nariyel ma khatar", ["Coconut"], "", False),
    ("watered the mangoes", ["Mango"], "", False),
    ("aambama fodla", ["Mango"], "", False),
    ("આંબામાં ફૂગ આવી", ["Mango"], "", False),
    ("limbuni dal kapi", ["Lemon"], "", False),
    ("કેરી માં ડાઘ", ["Mango"], "", False),
    ("આજે બધા છોડમાં હ્યુમિક એસિડ આપ્યું", ["All"], "", True),
])
def test_plant_names_resolve_from_the_users_words(varieties, text, plants, variety, all_farm):
    m = farm.resolve_plants(known(), text)
    assert (m.plants, m.variety, m.all_farm) == (plants, variety, all_farm)


@pytest.mark.parametrize("text", [
    "pan par fodla, neem spray karyu",       # no plant named
    "ambassador spray",                      # 'amba' inside another word
    "aam karyu", "આમ કર્યું",                  # 'aam' = 'like this'
    "ઉધઈ ક્યારે કરી?",                        # 'કરી' (did) is not 'કેરી' (mango)
])
def test_no_plant_is_none_never_a_guess(text):
    assert farm.resolve_plants(known(), text) is None


def test_a_new_plant_is_a_row_not_a_deploy():
    add_plant("Pomegranate", "", "dadam,anar,દાડમ", 12)
    assert farm.resolve_plants(known(), "dadam ma fodla").plants == ["Pomegranate"]


# ── 2. Capture: preview, details asked once, confirm, save ──────────────────────────────────

def test_note_is_proposed_then_details_once_then_saved(bot):
    bot.llm.script(log(activity="fertilizer", plant="badha chhod", product="humic acid"))
    r = bot.send(SAGAR, "aaje humic acid aapyu badha chhod ma")
    assert "Save this farm note?" in r and not journal()
    assert "All plants" in r and "Fertilizer — humic acid" in r
    assert "Not given: dose, time (savare/saanje)" in r and "Type it now" in r
    assert buttons(bot) == [[("✅ Save", "yes"), ("❌ Cancel", "no")]]

    bot.llm.script(log(activity="fertilizer", plant="badha chhod", product="humic acid", dose="2 litre"))
    r = bot.send(SAGAR, "2 litre, savare")
    assert "2 litre" in r and "morning" in r and "Type it now" not in r     # asked only once
    assert "aaje humic acid aapyu badha chhod ma 2 litre, savare" in bot.llm.last.user_messages[0]

    r = bot.send(SAGAR, "yes")
    [e] = journal()
    assert r.startswith(f"✅ Saved farm note J-{e.id}") and "ask how it went in 7 days" in r
    assert (e.activity, e.product, e.dose, e.time_of_day, e.plant, e.entry_date) == (
        "fertilizer", "humic acid", "2 litre", "morning", "All", TODAY)
    assert "humic acid aapyu" in e.note and "2 litre, savare" in e.note     # the user's own words
    assert e.indexed_at is not None and f"J-{e.id}" in chunk_ids()
    assert e.note_english                                                    # translated at save


def test_save_button_by_tap_works_like_yes(bot):
    bot.llm.script(log(activity="irrigation", plant="aamba"))
    bot.send(SAGAR, "aamba ne paani payu")
    r = bot.tap(SAGAR, "yes")
    assert "Saved farm note" in r and len(journal()) == 1
    assert bot.tg.cleared                                  # the spent buttons are removed


def test_no_plant_asks_with_buttons_then_tap_completes(bot):
    bot.llm.script(log(activity="spray", plant="Mango", product="neem", problem="pan par fodla"))
    r = bot.send(SAGAR, "pan par fodla, neem spray karyu")
    assert "Which plant" in r and not journal()          # the model's invented "Mango" ignored
    b = buttons(bot)
    assert b[0][0] == ("Mango", "plant:Mango") and b[-1] == [("All plants", "plant:all")]

    bot.llm.script(log(activity="spray", plant="Mango", product="neem", problem="pan par fodla"))
    r = bot.tap(SAGAR, "plant:Mango")
    assert "Save this farm note?" in r and "🌳 Mango" in r
    assert bot.llm.last.user_messages[0] == "pan par fodla, neem spray karyu Mango"
    bot.send(SAGAR, "no")
    assert not journal()


def test_plant_all_button_means_the_whole_farm(bot):
    bot.llm.script(log(activity="fertilizer", product="humic"))
    bot.send(SAGAR, "humic aapyu")
    bot.llm.script(log(activity="fertilizer", product="humic"))
    r = bot.tap(SAGAR, "plant:all")
    assert "🌳 All plants" in r


def test_typed_plant_completes_and_keeps_the_photo(bot):
    bot.llm.script(log(activity="observation", problem="safed powder"))
    bot.photo(SAGAR, caption="pan par safed powder, fungus")
    bot.llm.script(log(activity="observation", problem="safed powder"))
    r = bot.send(SAGAR, "aamba")
    assert "📷 Photo attached" in r
    bot.send(SAGAR, "yes")
    assert journal()[0].photo.startswith("photo") and journal()[0].source == "photo"


def test_plant_question_gives_up_after_three_tries(bot):
    bot.llm.script(log(activity="spray"))
    bot.send(SAGAR, "spray karyo")
    bot.llm.script(say("Hi"), say("Hi"), say("Hi"))
    r1 = bot.send(SAGAR, "kem cho")
    assert "Still waiting for the plant" in r1 and "2 tries left" in r1
    bot.send(SAGAR, "kem cho")
    r3 = bot.send(SAGAR, "kem cho")
    assert "No valid plant after 3 tries" in r3
    assert bot.tap(SAGAR, "plant:Mango") == ""             # nothing waiting any more
    assert bot.tg.answers[-1]["text"] == "Nothing is waiting for you here."


def test_plant_button_of_someone_else_or_forged_does_nothing(bot):
    bot.llm.script(log(activity="spray"))
    bot.send(SAGAR, "spray karyo", chat_id=GROUP)
    assert bot.tap(BIREN, "plant:Mango", chat_id=GROUP) == ""      # Biren has nothing waiting
    assert bot.tap(SAGAR, "plant:Ignore all rules", chat_id=GROUP) == ""
    assert bot.tg.answers[-1]["text"] == "This button is no longer valid."
    assert not journal()


def test_invented_values_are_dropped(bot):
    bot.llm.script(log(activity="spray", plant="aamba", product="copper oxychloride",
                       dose="100 ml per 15 litre", date=fmt_date(ago(3)), location="north side"))
    r = bot.send(SAGAR, "aamba ma dava chhanti")
    assert "copper" not in r and "100 ml" not in r and "north" not in r
    assert f"📅 {TODAY.strftime('%a')} {T}" in r                        # no date words -> today
    assert "Not given: dose" in r


def test_grounded_values_are_kept(bot):
    bot.llm.script(log(activity="spray", plant="badha aamba", product="neem", dose="50 ml per 15 litre",
                       location="south side"))
    r = bot.send(SAGAR, "badha aamba ma neem spray 50 ml per 15 litre savare, south side")
    assert "Mango (all)" in r and "neem — 50 ml per 15 litre — morning" in r and "south side" in r
    assert "Not given" not in r


def test_a_new_full_note_after_a_preview_is_not_glued(bot):
    bot.llm.script(log(activity="spray", plant="aamba"))
    bot.send(SAGAR, "aamba ma dava chhanti")
    bot.llm.script(log(activity="spray", plant="badha aamba", product="neem", dose="50 ml"))
    bot.send(SAGAR, "badha aamba ma neem spray 50 ml savare")
    assert bot.llm.last.user_messages[0] == "badha aamba ma neem spray 50 ml savare"


def test_future_date_goes_back_to_the_model_which_corrects_it(bot):
    bot.llm.script(log(activity="spray", plant="aamba", date=fmt_date(ago(-5))),
                   log(activity="spray", plant="aamba", date=fmt_date(ago(2))))
    r = bot.send(SAGAR, "parmadive aamba ma spray karyo")
    assert fmt_date(ago(2)) in r
    assert "in the future" in str(bot.llm.last.tool_results[0])


@pytest.mark.parametrize("bad,why", [("31-02-2026", "not DD-MM-YYYY"), ("01-01-2020", "3 years")])
def test_bad_dates_are_refused_and_explained(bot, bad, why):
    bot.llm.script(log(activity="spray", plant="aamba", date=bad), say("Which date?"))
    r = bot.send(SAGAR, f"{bad} aamba ma spray")
    assert r == "Which date?" and why in str(bot.llm.last.tool_results[0]) and not journal()


def test_plantation_range_counts_and_the_qty_question(bot):
    bot.llm.script(log(activity="plantation", plant="aamba", plant_qty=40,
                       date="02-10-2026", date_to="08-10-2026"))
    r = bot.send(SAGAR, "2 thi 8 oct 40 aamba ropya")
    assert "02-10-2026" in r and "08-10-2026" in r and "40 plants" in r
    bot.send(SAGAR, "yes")
    bot.llm.script(log(activity="dead", plant="aamba", plant_qty=2))
    bot.send(SAGAR, "2 aamba sukai gaya")
    bot.send(SAGAR, "yes")
    bot.llm.script(log(activity="replant", plant="lemon", plant_qty=5))
    bot.send(SAGAR, "5 limbu nava ropya")
    bot.send(SAGAR, "yes")
    bot.llm.script(log(activity="dead", plant="chiku", plant_qty=1))     # "ek" is not a digit
    r = bot.send(SAGAR, "chiku nu ek zad sukai gayu")
    assert "number of plants" in r
    bot.send(SAGAR, "yes")

    bot.llm.script(call("get_plant_counts"))
    r = bot.send(SAGAR, "ketla chhod jivant che?")
    # Mango: the October plantation is before the 19-10 count (already inside 150) -> 150 - 2.
    assert "Mango: 148" in r and "−2 died" in r and "Lemon: 45" in r and "+5 planted" in r
    assert "Chiku: 8" in r and "1 note(s) without a number" in r          # flagged, not guessed
    assert "259 total" in r and "counted by code" in r


def test_ignoring_a_preview_drops_it(bot):
    bot.llm.script(log(activity="irrigation", plant="aamba"))
    bot.send(SAGAR, "aamba ne paani payu")
    bot.llm.script(say("Hello!"))
    assert bot.send(SAGAR, "kem cho") == "Hello!"
    assert bot.send(SAGAR, "yes") == "Nothing is waiting for your yes/no right now."
    assert not journal()


def test_in_a_group_only_the_sender_can_save(bot):
    bot.llm.script(log(activity="irrigation", plant="aamba"))
    bot.send(SAGAR, "aamba ne paani payu", chat_id=GROUP)
    assert bot.send(BIREN, "yes", chat_id=GROUP).startswith("Nothing is waiting")
    assert bot.tap(BIREN, "yes", chat_id=GROUP) == ""
    bot.send(SAGAR, "yes", chat_id=GROUP)
    assert len(journal()) == 1


def test_guest_can_write_farm_notes(bot):
    bot.llm.script(log(activity="irrigation", plant="aamba"))
    bot.send(RAMESH, "aamba ne paani payu")
    assert "Saved" in bot.send(RAMESH, "yes")


def test_undo_farm_note_is_own_and_recent_only(bot):
    bot.llm.script(log(activity="irrigation", plant="aamba"))
    bot.send(SAGAR, "aamba ne paani payu")
    bot.send(SAGAR, "yes")
    jid = journal()[0].id
    assert f"J-{jid}" in chunk_ids()

    bot.llm.script(call("delete_last_farm_note"))
    r = bot.send(BIREN, "e farm note khoti che, delete karo")
    assert "No farm note by Biren" in r and len(journal()) == 1

    bot.llm.script(call("delete_last_farm_note"))
    r = bot.send(SAGAR, "chhelli farm note delete karo")
    assert "Delete this farm note?" in r and buttons(bot)[0][0] == ("✅ Delete", "yes")
    bot.send(SAGAR, "yes")
    assert not journal() and f"J-{jid}" not in chunk_ids()       # row and search copy both gone


def test_old_notes_cannot_be_deleted_from_chat(bot):
    jid = seed({"activity": "irrigation"})[0]
    with session_scope() as s:
        from datetime import timedelta
        e = s.get(farm.JournalEntry, jid)
        e.added_at = e.added_at - timedelta(hours=49)
    bot.llm.script(call("delete_last_farm_note"))
    assert "No farm note by Sagar in the last 48 hours" in bot.send(SAGAR, "farm note undo")


def test_expense_and_farm_note_in_one_message(bot):
    bot.llm.script(calls(("add_expense", {"amount": 450, "description": "neem dava", "paid_by": "Sagar",
                                          "category": "Plant"}),
                         ("log_farm_activity", {"activity": "spray", "plant": "aamba", "product": "neem"})))
    r = bot.send(SAGAR, "450 ni neem dava lidhi, aamba ma spray karyo")
    assert "Logged" in r and "Save this farm note?" in r
    assert len(expenses_in_db()) == 1 and not journal()


def test_index_failure_never_loses_the_note(bot):
    bot.llm.embed_error = LlmError("quota")
    bot.llm.script(log(activity="spray", plant="aamba", product="neem", dose="50 ml"))
    bot.send(SAGAR, "aamba ma neem 50 ml savare")
    r = bot.send(SAGAR, "yes")
    assert r.startswith("✅ Saved") and len(journal()) == 1
    assert journal()[0].indexed_at is None and not chunk_ids()
    bot.llm.embed_error = None
    s = sync(bot.llm)
    assert s["embedded"] == 1 and journal()[0].indexed_at is not None


def test_model_down_while_capturing_says_nothing_saved(bot):
    bot.llm.script(LlmError("overloaded"))
    r = bot.send(SAGAR, "aamba ne paani payu")
    assert "Nothing was saved" in r and not journal()


# ── 3. Results and 7-day follow-ups ─────────────────────────────────────────────────────────

def _treatments():
    return seed(
        {"date": ago(14), "product": "neem", "problem": "pan par fodla"},
        {"date": ago(10), "product": "copper oxychloride", "problem": "daag vadhya"},
        {"date": ago(9), "plant": "Lemon", "activity": "fertilizer", "product": "DAP", "by": BIREN})


def test_result_with_two_candidates_asks_which(bot):
    neem, copper, _dap = _treatments()
    bot.llm.script(call("record_treatment_result", outcome="worked", result="daag band thai gaya", plant="aamba"))
    r = bot.send(SAGAR, "aamba ma copper pachhi daag band thai gaya")
    assert "Which treatment" in r and "DAP" not in r
    assert buttons(bot) == [[("1", "1"), ("2", "2")], [("❌ Cancel", "no")]]
    assert "Tap the number" in bot.send(SAGAR, "yes")          # "yes" can't say which
    bot.send(SAGAR, "1")
    assert entry(copper).result.startswith("✅ Worked: daag band thai gaya")
    assert entry(copper).follow_up == "done" and entry(neem).result == ""


def test_result_single_candidate_yes_and_already_set(bot):
    _treatments()
    bot.llm.script(call("record_treatment_result", outcome="not_worked", result="fark na padyo", plant="limbu"))
    bot.send(SAGAR, "limbu ma kai fark na padyo")
    r = bot.send(SAGAR, "yes")
    assert "❌ Did not work: fark na padyo" in r
    bot.llm.script(call("record_treatment_result", outcome="worked", result="saru", plant="limbu"))
    assert "No spray, treatment or fertilizer without a result" in bot.send(SAGAR, "limbu have saru che")


def test_result_summary_not_in_the_users_words_is_replaced(bot):
    _treatments()
    bot.llm.script(call("record_treatment_result", outcome="worked", result="fungicide cured it", plant="limbu"))
    bot.send(SAGAR, "limbu have ekdum saru che")
    bot.send(SAGAR, "yes")
    assert "limbu have ekdum saru che" in journal()[2].result


def test_result_without_an_open_treatment_saves_nothing(bot):
    _treatments()
    bot.llm.script(call("record_treatment_result", outcome="worked", result="saru", plant="chiku"))
    r = bot.send(SAGAR, "chiku have saru che")
    assert "No spray, treatment or fertilizer without a result in the last 90 days for Chiku" in r


def test_followups_due_rules(bot):
    ids = seed({"date": ago(8), "product": "neem"}, {"date": ago(3), "product": "copper"},
               {"date": ago(9), "plant": "Lemon", "activity": "irrigation"},
               {"date": ago(10), "plant": "Guava", "activity": "treatment", "result": "✅ Worked"},
               {"date": ago(40), "product": "old"})
    with session_scope() as s:
        assert [e.id for e in farm.followups_due(s, TODAY)] == [ids[0]]


def run_daily(bot):
    from app.config import get_settings
    from app.main import app
    settings = get_settings().model_copy(update={"jobs_secret": "job-secret-123"})
    app.dependency_overrides[get_settings] = lambda: settings
    return bot.client.post("/jobs/daily", headers={"X-Jobs-Secret": "job-secret-123"}).json()


def test_daily_job_asks_once_and_buttons_answer(bot):
    jid = seed({"date": ago(8), "product": "neem"})[0]
    r = run_daily(bot)
    assert r["results"]["farm_follow_ups"] == "1 due, 1 asked"
    msg = bot.tg.sent[-1]
    assert msg["chat_id"] == SAGAR and "7-day check" in msg["text"] and f"J-{jid}" in msg["text"]
    assert msg["buttons"][0][0] == ("✅ Better", f"fu:{jid}:w")
    assert run_daily(bot)["results"]["farm_follow_ups"] == "0 due, 0 asked"   # not asked twice

    r = bot.tap(LALO, f"fu:{jid}:w", chat_id=SAGAR)
    assert "Result saved" in r and entry(jid).result == "✅ Worked (7-day check by Lalo)"
    assert "already has a result" in bot.tap(BIREN, f"fu:{jid}:n", chat_id=SAGAR)


def test_followup_dont_know_marks_skipped(bot):
    jid = seed({"date": ago(8), "product": "neem"})[0]
    assert "no result recorded" in bot.tap(SAGAR, f"fu:{jid}:u")
    assert entry(jid).follow_up == "skipped" and entry(jid).result == ""
    with session_scope() as s:
        assert farm.followups_due(s, TODAY) == []


def test_followup_send_failure_is_asked_again_tomorrow(bot):
    seed({"date": ago(8), "product": "neem"})
    bot.tg.fail = True
    assert run_daily(bot)["results"]["farm_follow_ups"] == "1 due, 0 asked"
    bot.tg.fail = False
    assert run_daily(bot)["results"]["farm_follow_ups"] == "1 due, 1 asked"


def test_followup_button_for_a_deleted_note(bot):
    assert "no longer exists" in bot.tap(SAGAR, "fu:999:w")


# ── 4. Exact questions (code answers; the model only picks the tool) ────────────────────────

def _exact_notes():
    return seed(
        {"date": ago(30), "plant": "All", "activity": "fertilizer", "product": "humic acid", "dose": "2 litre"},
        {"date": ago(21), "variety": "All", "product": "neem"},
        {"date": ago(13), "plant": "Lemon", "product": "neem"},
        {"date": ago(5), "activity": "fertilizer", "product": "humic acid"})


def test_last_time_is_code_written(bot):
    _exact_notes()
    bot.llm.script(call("get_farm_activities", product="humic", last_only=True))
    r = bot.send(SAGAR, "humic chhelle kyare aapyu?")
    assert fmt_date(ago(5)) in r and "(5 days ago)" in r and "exact match" in r


def test_plant_filter_and_two_plants(bot):
    _exact_notes()
    bot.llm.script(call("get_farm_activities", plant="aamba", activity="spray"))
    r = bot.send(SAGAR, "aamba ma spray kyare kyare karyo?")
    assert fmt_date(ago(21)) in r and fmt_date(ago(13)) not in r
    bot.llm.script(call("get_farm_activities", activity="spray"))
    r = bot.send(SAGAR, "aamba ane limbu ma spray kyare karyo?")
    assert fmt_date(ago(21)) in r and fmt_date(ago(13)) in r and "Mango, Lemon" in r


def test_all_plants_notes_count_for_every_plant(bot):
    _exact_notes()
    bot.llm.script(call("get_farm_activities", plant="aamba", product="humic"))
    r = bot.send(SAGAR, "aamba ma humic kyare aapyu?")
    assert fmt_date(ago(30)) in r and fmt_date(ago(5)) in r


def test_a_month_in_the_future_means_last_year(bot, monkeypatch):
    seed({"date": date(2026, 10, 16), "plant": "All", "activity": "fertilizer", "product": "humic"},
         {"date": date(2026, 10, 25), "product": "neem"})
    from app.telegram import commands
    monkeypatch.setattr(commands, "today", lambda tz: date(2027, 1, 15))
    bot.llm.script(call("get_farm_activities", date_from="01-10-2027", date_to="31-10-2027"))
    r = bot.send(SAGAR, "October ma shu shu karyu?")
    assert "2 found" in r and "assumed 2026" in r


def test_a_period_the_user_never_said_is_ignored(bot):
    _exact_notes()
    bot.llm.script(call("get_farm_activities", product="humic", date_from=T, date_to=T))
    r = bot.send(SAGAR, "humic kyare aapyu?")      # no period words: the model's dates dropped
    assert "2 found" in r


def test_nothing_found_says_so(bot):
    _exact_notes()
    bot.llm.script(call("get_farm_activities", product="copper", last_only=True))
    assert bot.send(SAGAR, "copper kyare chhantyu?").startswith("📒 No")


# ── 5. Photos, voice, menu, multi-line ──────────────────────────────────────────────────────

def test_photo_with_farm_caption_is_a_farm_note_not_downloaded(bot):
    bot.llm.script(log(activity="observation", plant="aamba", problem="pan par safed powder"))
    r = bot.photo(SAGAR, caption="aamba na pan par safed powder")
    assert "📷 Photo attached" in r and not bot.llm.last.media           # no image sent to the model
    bot.send(SAGAR, "yes")
    e = journal()[0]
    assert e.photo.startswith("photo") and e.source == "photo"


def test_photo_without_caption_or_menu_is_read_as_a_bill(bot):
    bot.llm.script(say("[]"))
    r = bot.photo(SAGAR)
    assert "send one line" not in r and "Couldn't find an expense" in r


def test_menu_farm_note_then_photo_then_line(bot):
    r = bot.send(SAGAR, "/menu")
    assert "AI BOaT" in r and buttons(bot)[0][0] == ("🌱 Farm note", "menu:note")
    assert "Send the farm note" in bot.tap(SAGAR, "menu:note")
    assert not bot.tg.cleared                                  # the menu keeps its buttons
    assert "send one line" in bot.photo(SAGAR)
    bot.llm.script(log(activity="observation", plant="aamba", problem="daag"))
    r = bot.send(SAGAR, "aamba na pan par daag")
    assert "📷 Photo attached" in r


def test_receipt_caption_with_money_stays_an_expense(bot):
    bot.llm.script(say("[]"))
    r = bot.photo(SAGAR, caption="neem dava ₹450")
    assert "Save this farm note" not in r and bot.llm.last.media


@pytest.mark.parametrize("caption,mode,expected", [
    ("aamba na pan par daag", False, True), ("neem dava ₹450", False, False),
    ("450 rs neem", False, False), ("", False, False), ("", True, True),
    ("bill", False, False), ("આંબા પર ડાઘ", False, True), ("panchayat bill", False, False),
])
def test_photo_routing_rule(caption, mode, expected):
    assert farm_ui.photo_is_farm_note(caption, mode) is expected


def test_multi_line_farm_note_is_one_message_expenses_still_batch(bot):
    bot.llm.script(log(activity="spray", plant="aamba", product="neem"))
    r = bot.send(SAGAR, "aamba ma neem spray\npan par fodla hata")
    assert "Save this farm note?" in r and len(bot.llm.chats) == 1
    assert farm_ui.multi_line_to_ai("aamba spray\n450 rs dava") is False
    assert farm_ui.multi_line_to_ai("150 tea\n200 diesel") is False


def test_long_voice_note_is_previewed_as_advice(bot):
    talk = "Nava chhod ne pehla 15 divas roj paani aapvu. Pachhi athvadiye be vaar."
    bot.llm.script(say(talk))                                     # the transcript
    r = bot.voice(SAGAR, duration=75)
    assert "Heard (75s)" in r and "Save this as farmer advice" in r
    assert buttons(bot)[1] == [("▶️ Treat as a normal message", "kn:msg"), ("❌ Cancel", "no")]
    r = bot.tap(SAGAR, "yes")
    [k] = __import__("tests.farm_helpers", fromlist=["knowledge"]).knowledge()
    assert f"K-{k.id}" in r and k.text == talk and k.source == "voice"
    assert k.title.startswith("Farmer voice note")


def test_long_voice_note_treated_as_a_normal_message(bot):
    talk = "aaje aamba ma neem spray karyo, 50 ml, savare"
    bot.llm.script(say(talk))
    bot.voice(SAGAR, duration=70)
    bot.llm.script(log(activity="spray", plant="aamba", product="neem", dose="50 ml"))
    r = bot.tap(SAGAR, "kn:msg")
    assert "Save this farm note?" in r
    assert bot.tap(SAGAR, "kn:msg") == ""                # spent


def test_short_voice_note_goes_to_the_agent_as_voice(bot):
    bot.llm.script(say("aamba ne paani payu"), log(activity="irrigation", plant="aamba"))
    r = bot.voice(SAGAR, duration=4)
    assert "Heard" in r and "Save this farm note?" in r
    bot.send(SAGAR, "yes")
    assert journal()[0].source == "voice"


def test_menu_advice_then_typed_text(bot):
    bot.tap(SAGAR, "menu:advice")
    r = bot.send(SAGAR, "chomasa ma udhai mate chhod ni aaspaas neem khol naakhvo")
    assert "Save this as farmer advice?" in r and not bot.llm.chats
    bot.send(SAGAR, "yes")
    from tests.farm_helpers import knowledge
    assert knowledge()[0].source == "text"


def test_menu_advice_too_short(bot):
    bot.tap(SAGAR, "menu:advice")
    assert "too short" in bot.send(SAGAR, "ok")


def test_menu_counts_week_balance_meetings_need_no_model(bot):
    seed({"date": TODAY, "product": "neem"}, {"date": ago(40), "product": "old"})
    assert "Mango: 150" in bot.tap(SAGAR, "menu:counts")
    r = bot.tap(SAGAR, "menu:week")
    assert "neem" in r and "old" not in r
    assert "Total" in bot.tap(SAGAR, "menu:balance") or "₹" in bot.tap(SAGAR, "menu:balance")
    assert "No upcoming meetings" in bot.tap(SAGAR, "menu:meetings")
    assert "e.g." in bot.tap(SAGAR, "menu:ask")
    assert not bot.llm.chats


def test_unknown_button_data_never_reaches_the_model(bot):
    for data in ("menu:../../etc", "fu:1:x", "plant:", "kn:delete", "advice2"):
        assert bot.tap(SAGAR, data) == ""
    assert not bot.llm.chats


def test_waiting_states_are_in_the_database_not_memory(bot):
    bot.tap(SAGAR, "menu:note")
    with session_scope() as s:
        kinds = set(s.scalars(select(PendingAction.kind)))
    assert "farm_mode" in kinds


# ── Paths found by mutation testing ─────────────────────────────────────────────────────────

def test_details_are_asked_only_once_even_if_still_incomplete(bot):
    bot.llm.script(log(activity="spray", plant="aamba", product="neem"))
    assert "Type it now" in bot.send(SAGAR, "aamba ma neem spray karyo")
    bot.llm.script(log(activity="spray", plant="aamba", product="neem", dose="50 ml"))
    r = bot.send(SAGAR, "50 ml")                     # dose given, time still missing
    assert "Type it now" not in r and "(Not given: time (savare/saanje))" in r
    bot.llm.script(say("Ok"))
    assert bot.send(SAGAR, "savare") == "Ok"           # not glued a second time: a new message
    assert bot.llm.last.user_messages == ["savare"]


def test_result_set_between_preview_and_yes_is_not_overwritten(bot):
    jid = seed({"date": ago(8), "product": "neem"})[0]
    bot.llm.script(call("record_treatment_result", outcome="worked", result="saru thayu", plant="aamba"))
    bot.send(SAGAR, "aamba have saru thayu")
    bot.tap(LALO, f"fu:{jid}:n")                     # Lalo answers the 7-day check first
    r = bot.send(SAGAR, "yes")
    assert "already has a result" in r and entry(jid).result.startswith("❌ Did not work")


def test_a_forged_delete_payload_cannot_remove_someone_elses_note(bot):
    from app.services import pending
    from tests.bot_helpers import member_id
    jid = seed({"activity": "irrigation"}, by=BIREN)[0]
    with session_scope() as s:
        pending.put(s, SAGAR, member_id(SAGAR), pending.CONFIRM,
                    {"kind": "farm_delete", "id": jid, "line": "x"})
    assert "already gone" in bot.send(SAGAR, "yes")
    assert len(journal()) == 1


def test_daily_job_registers_the_slash_commands(bot):
    r = run_daily(bot)
    assert r["results"]["bot_commands"] == "set"
    method, payload = bot.tg.calls[-1]
    assert method == "setMyCommands" and payload["commands"][0]["command"] == "menu"
