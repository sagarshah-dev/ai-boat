"""Fixes from the live checks of 05-11-2026.

"Kesar par daag che, have shu karu?" returned 5 notes, one of them a Dadam note, and the AI
summary failed (log said only 'LlmError'). Causes, each covered here with every path:
1. "have" / "karu" were search words (stop words now live in a table, not in code);
2. "Kesar" was no known plant, so no plant filter (Mango varieties are now plant rows);
3. the failure log had no reason (Gemini wrapper tests: test_gemini.py);
4. the two small wording bugs from 31-10 ("1 entries", the Path line)."""
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.ai import agent
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from app.services import farm, rag, rag_index
from tests.bot_helpers import SAGAR
from tests.conftest import call
from tests.farm_helpers import ago, journal, log, rag_answer, seed, seed_knowledge, sync

LIVE_Q = "Kesar par daag che, have shu karu?"


@pytest.fixture
def live_notes(bot):
    """The live journal of 05-11 (baseline test data)."""
    ids = seed(
        {"date": ago(21), "plant": "Mango", "variety": "Kesar", "product": "copper oxychloride",
         "problem": "pan par kala daag dekhaya", "result": "✅ Worked: Kesar ma daag ochha thaya",
         "note": "15-Oct: Kesar aamba na pan par kala daag dekhay"},
        {"date": ago(18), "plant": "Guava", "activity": "observation",
         "problem": "Dadam na fal ma tirad padi, pani aniyamit hatu, have drip roj 30 min karyu",
         "note": "18-Oct: Dadam na fal ma tirad padi, pani aniyamit hatu, have drip roj 30 min karyu"},
        {"date": ago(9), "plant": "Mango", "problem": "pan par fodla", "note": "pan par fodla, spray karyu"},
        {"date": ago(9), "plant": "Mango", "product": "neem", "dose": "10 ml per 1 ltr",
         "note": "aaje aamba ma neem spray karyo 10 ml per 1 ltr"},
        {"date": ago(16), "plant": "Chiku", "product": "neem oil", "problem": "mealybug",
         "result": "❌ Did not work: mealybug haju che",
         "note": "20-Oct: Chiku par mealybug dekhaya, neem oil spray karyu"})
    seed_knowledge("Mango plantation care", "Urea vadhu aapvu nahi. Udhai dekhay to neem khol naakhvo.")
    sync(bot.llm)
    return ids


def stop():
    with session_scope() as s:
        return rag_index.stopwords(s)


# ── 1. Filler words are not search words ────────────────────────────────────────────────────

@pytest.mark.parametrize("q,want", [
    (LIVE_Q, ["kesar", "daag"]),
    ("neem spray ketlu karwanu", ["neem", "spray"]),
    ("hamna aamba ma shu karvu", ["aamba"]),
    ("keri ma 0:52:34 kyare aapyu?", ["keri", "0:52:34", "aapyu"]),     # NPK grade stays one term
])
def test_question_words_are_dropped(bot, q, want):
    assert rag_index.keyword_terms(q, stop()) == want


def test_live_dadam_note_no_longer_matches_on_have(bot, live_notes):
    dadam = f"J-{live_notes[1]}"
    with session_scope() as s:
        terms = rag_index.keyword_terms(LIVE_Q, rag_index.stopwords(s))
        _, kw = rag.candidates(s, None, terms, rag.Filters())
    assert dadam not in [h.id for h in kw]


def test_search_itself_uses_the_table(bot, live_notes):
    dadam = f"J-{live_notes[1]}"
    with session_scope() as s:
        found = rag.retrieve(s, bot.llm, LIVE_Q, rag.Filters(), 0.3)
    assert dadam not in found.trace["steps"][0]["kw_top"]


def test_a_new_stop_word_is_a_row_not_a_deploy(bot):
    assert "dekhay" in rag_index.keyword_terms("daag dekhay", stop())
    with session_scope() as s:
        s.execute(text("INSERT INTO search_stopwords (word) VALUES ('dekhay')"))
    rag_index.reset_stopwords_cache()                   # production: picked up within 5 minutes
    assert rag_index.keyword_terms("daag dekhay", stop()) == ["daag"]


def test_cache_is_read_once_not_per_question(bot):
    stop()
    with session_scope() as s:
        s.execute(text("INSERT INTO search_stopwords (word) VALUES ('daag')"))
    assert "daag" in rag_index.keyword_terms("daag", stop())    # still the cached list


def test_empty_stop_word_table_still_searches(bot, live_notes):
    with session_scope() as s:
        s.execute(text("DELETE FROM search_stopwords"))
    rag_index.reset_stopwords_cache()
    bot.llm.script(call("search_farm_notes", query=LIVE_Q),
                   rag_answer("Copper spray karyu hatu.", [f"J-{live_notes[0]}"]))
    assert "Copper spray karyu hatu." in bot.send(SAGAR, LIVE_Q)


# ── 2. Mango varieties are known plants ─────────────────────────────────────────────────────

@pytest.mark.parametrize("msg,variety", [
    ("Kesar par daag che", "Kesar"), ("કેસર ma daag", "Kesar"), ("rajapuri ma mor aavyo", "Rajapuri"),
    ("tota ma fodla", "Totapuri"), ("nurjahan ne paani", "Noorjahan"), ("miyazaki ma khatar", "Miyazaki"),
    ("sonpari par daag", "Sonpari"), ("badam aamba ma spray", "Badam"),
])
def test_variety_names_resolve_to_mango(bot, msg, variety):
    with session_scope() as s:
        m = farm.resolve_plants(farm.plants(s), msg)
    assert m.plants == ["Mango"] and m.variety == variety


@pytest.mark.parametrize("msg,plants,variety", [
    ("aamba ma neem", ["Mango"], ""), ("badha aamba ma neem", ["Mango"], farm.ALL),
    ("aamba ane chiku ma spray", ["Mango", "Chiku"], ""),
])
def test_plain_mango_questions_are_unchanged(bot, msg, plants, variety):
    with session_scope() as s:
        m = farm.resolve_plants(farm.plants(s), msg)
    assert m.plants == plants and m.variety == variety


def test_variety_rows_do_not_change_the_mango_count(bot):
    with session_scope() as s:
        counts = {c.plant: c.alive for c in farm.live_counts(s)}
    assert counts["Mango"] == 150 and len(counts) == 5


def test_live_question_is_filtered_to_kesar_and_skips_dadam(bot, live_notes):
    j3, dadam, chiku = (f"J-{live_notes[i]}" for i in (0, 1, 4))
    # No date in the scripted answer: the notes are seeded relative to today, so a fixed "15-10"
    # was only "grounded" while some note's date fell in October (failed from 24-11-2026).
    bot.llm.script(call("search_farm_notes", query=LIVE_Q),
                   rag_answer("Copper oxychloride chhantyu hatu, kaam karyu.", [j3]))
    r = bot.send(SAGAR, LIVE_Q)
    assert "filter: Mango, Kesar" in r
    prompt = [p for sys, p in bot.llm.completions if sys == rag.ANSWER_RULES][-1]
    assert f"[{j3}]" in prompt and f"[{dadam}]" not in prompt and f"[{chiku}]" not in prompt


def test_variety_note_is_saved_with_its_variety_then_found(bot):
    bot.llm.script(log(activity="spray", product="neem", dose="10 ml"))
    r = bot.send(SAGAR, "kesar ma neem spray karyu 10 ml")
    assert "Mango (Kesar)" in r and "Which plant" not in r
    bot.send(SAGAR, "yes")                              # the next reply to "Save this farm note?"
    e = journal()[-1]
    assert (e.plant, e.variety) == ("Mango", "Kesar")


def test_variety_note_then_no_is_not_saved(bot):
    bot.llm.script(log(activity="spray", product="neem"))
    bot.send(SAGAR, "rajapuri ma neem spray karyu")
    bot.send(SAGAR, "no")
    assert journal() == []


# ── 3. A failed summary still answers, and the next question works ──────────────────────────

def test_failed_summary_shows_the_notes_then_next_question_is_normal(bot, live_notes):
    j3 = f"J-{live_notes[0]}"
    bot.llm.script(call("search_farm_notes", query=LIVE_Q), LlmError("timeout", "x"))
    r = bot.send(SAGAR, LIVE_Q)
    assert "Matching notes (best match first):" in r and "word for word" in r
    assert "filter: Mango, Kesar" in r and "Dadam" not in r
    bot.llm.script(call("search_farm_notes", query=LIVE_Q), rag_answer("Copper ✅.", [j3]))
    r2 = bot.send(SAGAR, LIVE_Q)
    assert "Copper ✅." in r2 and "word for word" not in r2


def test_neem_dose_question_finds_the_dose_note_first(bot, live_notes):
    neem = f"J-{live_notes[3]}"
    with session_scope() as s:
        found = rag.retrieve(s, bot.llm, "neem spray ketlu karwanu", rag.Filters(), 0.3)
    assert found.hits and found.hits[0].id == neem


# ── 4. Wording ──────────────────────────────────────────────────────────────────────────────

class _Row:
    def __init__(self, i):
        self.id = i

    def line(self):
        return f"row {self.id}"


@pytest.mark.parametrize("n,word", [(1, "1 entry"), (2, "2 entries"), (0, None)])
def test_entry_counts_read_right(n, word):
    rows = [_Row(i) for i in range(n)]
    out = agent.search_text(rows, Decimal(0)) + agent.recent_text(rows)
    if word:
        assert word in out and ("1 entries" not in out)
    else:
        assert "No entries" in out
