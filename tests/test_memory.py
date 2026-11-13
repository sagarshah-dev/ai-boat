"""Short-term memory (sliding window): live baseline 13-11-2026 was 0 of 3 follow-ups.

M1 "Chhelle copper kyare chhantyu?" -> "E divase biju kai kharch thayo?"
M2 "Kesar par mor kala che"         -> "Shu karu?"
M3 "Biren e 100 tea lidhi"          -> "Na, 150 hata"   (was: the entry undone, no question)

Each person's last 3 exchanges in a chat, for 30 minutes, go to the model before the new message.
Grounding may use them for the subject (a plant, a topic) but never for a number; every write
still needs ✅.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.ai import farm_salah
from app.db.models import ConversationTurn, Expense
from app.db.session import session_scope
from app.services import memory
from tests.bot_helpers import BIREN, GROUP, SAGAR, add_rows, expenses_in_db
from tests.conftest import SETTLE_PIN, call, say
from tests.farm_helpers import seed_knowledge, sync
from tests.test_farm_salah import (DOSE_TEXT, OCT15, dose_form, farm_data,  # noqa: F401
                                   graded, salah)

M1_A, M1_B = "Chhelle copper kyare chhantyu?", "E divase biju kai kharch thayo?"


def history(bot):
    return bot.llm.chats[-1].history


def turns(chat_id=None):
    with session_scope() as s:
        q = select(ConversationTurn).order_by(ConversationTurn.id)
        if chat_id is not None:
            q = q.where(ConversationTurn.chat_id == chat_id)
        return [(t.member_id, t.role, t.text) for t in s.scalars(q)]


# ── The window ──────────────────────────────────────────────────────────────────────────────

def test_keeps_the_last_three_exchanges_oldest_first(bot):
    bot.send(SAGAR, "balance")                                # any message is remembered
    with session_scope() as s:
        me = memory.recent(s, SAGAR, 1)
    assert [r for r, _ in me] == ["user", "bot"] and me[0][1] == "balance"
    t0 = datetime.now(timezone.utc)
    with session_scope() as s:
        for i in range(5):
            memory.record(s, 5, 1, f"q{i}", f"a{i}", now=t0 + timedelta(seconds=i))
        got = memory.recent(s, 5, 1, now=t0 + timedelta(seconds=10))
        assert got == [("user", "q2"), ("bot", "a2"), ("user", "q3"), ("bot", "a3"),
                       ("user", "q4"), ("bot", "a4")]
        # pruned on write: no scheduled job needed
        assert s.scalar(select(ConversationTurn.id).where(ConversationTurn.text == "q0")) is None


def test_forgets_after_thirty_minutes(bot):
    t0 = datetime.now(timezone.utc)
    with session_scope() as s:
        memory.record(s, 5, 1, "old", "old reply", now=t0 - timedelta(minutes=31))
        memory.record(s, 5, 1, "new", "new reply", now=t0)
        assert memory.recent(s, 5, 1, now=t0) == [("user", "new"), ("bot", "new reply")]


def test_long_replies_are_clipped(bot):
    with session_scope() as s:
        memory.record(s, 5, 1, "list", "x" * 5000)
        assert len(memory.recent(s, 5, 1)[1][1]) == memory.MAX_CHARS


def test_each_person_and_each_chat_has_its_own_memory(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(BIREN, "balance", chat_id=GROUP)
    bot.send(SAGAR, "recent")                                 # Sagar's private chat
    bot.llm.script(say("ok"))
    bot.send(SAGAR, "kem cho", chat_id=GROUP)
    assert history(bot) == []          # neither Biren's group message nor Sagar's private one
    bot.llm.script(say("ok"))
    bot.send(SAGAR, "aaje shu karvanu?", chat_id=GROUP)
    assert [r for r, _ in history(bot)] == ["user", "bot"] and history(bot)[0][1] == "kem cho"


def test_a_settle_pin_is_never_remembered(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "settle")
    bot.send(SAGAR, SETTLE_PIN)
    bot.send(SAGAR, f"settle {SETTLE_PIN}")
    assert not any(SETTLE_PIN in text for _, _, text in turns())


def test_an_expired_pin_question_still_keeps_the_pin_away_from_the_model(bot):
    from app.db.models import PendingAction
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "settle")
    with session_scope() as s:
        s.execute(update(PendingAction).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert "timed out" in bot.send(SAGAR, SETTLE_PIN)
    assert bot.llm.chats == [] and not any(SETTLE_PIN in t for _, _, t in turns())


def test_voice_notes_are_remembered_as_their_transcript(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.llm.script(say("hisab batav"), call("get_balances"))
    bot.voice(SAGAR)
    assert turns()[0][1:] == ("user", "hisab batav")


# ── M1: "e divase" ───────────────────────────────────────────────────────────────────────────

def _m1(bot):
    add_rows(("Lalo", 300, "majuri"), when=OCT15)
    bot.llm.script(call("get_farm_activities", text="copper", last_only=True))
    first = bot.send(SAGAR, M1_A)
    assert "15-10-2026" in first and "copper oxychloride" in first


def test_m1_the_day_comes_from_the_last_reply(bot, farm_data):
    _m1(bot)
    bot.llm.script(salah("same_day", topic="copper"), graded(3))
    r = bot.send(SAGAR, M1_B)
    assert history(bot)[0] == ("user", M1_A) and "copper oxychloride" in history(bot)[1][1]
    assert "Which farm work" not in r
    assert "📒 copper oxychloride — 15-10-2026 (J-3)" in r and "₹300 — majuri — Lalo" in r
    # the relevance check is told what "e divase" refers to
    prompt = next(p for sysmsg, p in bot.llm.completions if sysmsg == farm_salah.GRADE_RULES)
    assert f"{M1_A} — {M1_B}" in prompt


def test_m1_without_memory_still_asks(bot, farm_data):
    add_rows(("Lalo", 300, "majuri"), when=OCT15)
    bot.llm.script(salah("same_day", topic="copper"))
    assert "Which farm work" in bot.send(SAGAR, M1_B)


def test_m1_after_thirty_minutes_asks_again(bot, farm_data):
    _m1(bot)
    with session_scope() as s:
        s.execute(update(ConversationTurn).values(
            created_at=datetime.now(timezone.utc) - timedelta(minutes=31)))
    bot.llm.script(salah("same_day", topic="copper"))
    assert "Which farm work" in bot.send(SAGAR, M1_B)
    assert history(bot) == []


def test_another_members_question_does_not_explain_mine(bot, farm_data):
    add_rows(("Lalo", 300, "majuri"), when=OCT15)
    bot.llm.script(call("get_farm_activities", text="copper", last_only=True))
    bot.send(BIREN, M1_A, chat_id=GROUP)
    bot.llm.script(salah("same_day", topic="copper"))
    assert "Which farm work" in bot.send(SAGAR, M1_B, chat_id=GROUP)


# ── M2: "shu karu?" ──────────────────────────────────────────────────────────────────────────

def _advice():
    seed_knowledge("Hexaconazole dose for mango tree", DOSE_TEXT, source="text")


def test_m2_the_problem_and_plant_come_from_the_last_message(bot, farm_data):
    _advice()
    sync(bot.llm)
    bot.llm.script(say("Note karu?"))
    bot.send(SAGAR, "Kesar par mor kala che")
    bot.llm.script(salah("plan"), graded(1, 2), dose_form())
    r = bot.send(SAGAR, "Shu karu?")
    assert "hexaconazole" in r and "How many trees?" in r
    prompt = next(p for sysmsg, p in bot.llm.completions if sysmsg == farm_salah.GRADE_RULES)
    assert "Kesar par mor kala che — Shu karu?" in prompt
    assert bot.send(SAGAR, "60").startswith("🌱 Spray plan — Mango (Kesar) · mor kala · 60 trees")


def test_trees_never_come_from_memory(bot, farm_data):
    _advice()
    sync(bot.llm)
    bot.llm.script(say("ok"))
    bot.send(SAGAR, "Kesar na 100 zhad che")
    bot.llm.script(salah("plan", trees=100), graded(1, 2), dose_form())
    assert "How many trees?" in bot.send(SAGAR, "mor kala che, shu karu?")


# ── M3: "na, 150 hata" ───────────────────────────────────────────────────────────────────────

def _tea(bot):
    bot.llm.script(call("add_expense", amount=100, description="tea", paid_by="Biren"))
    assert "Logged" in bot.send(SAGAR, "Biren e 100 tea lidhi")


def test_m3_a_correction_is_an_edit_shown_before_it_is_made(bot):
    _tea(bot)
    bot.llm.script(call("edit_last_expense", amount=150))
    r = bot.send(SAGAR, "Na, 150 hata")
    assert "✏️ Change #1?" in r and "₹100 — tea  →  ₹150 — tea" in r
    assert [int(e.amount) for e in expenses_in_db()] == [100]
    assert history(bot)[0] == ("user", "Biren e 100 tea lidhi")
    assert bot.send(SAGAR, "yes").startswith("✅ Changed #1: ₹150 for tea, paid by Biren")
    assert [int(e.amount) for e in expenses_in_db()] == [150]


def test_m3_no_keeps_the_entry(bot):
    _tea(bot)
    bot.llm.script(call("edit_last_expense", amount=150))
    bot.send(SAGAR, "Na, 150 hata")
    bot.send(SAGAR, "no")
    assert [int(e.amount) for e in expenses_in_db()] == [100]


def test_m3_undo_by_mistake_is_only_a_question(bot):
    # The live failure: the model chose undo. Now it is a question, not a deletion.
    _tea(bot)
    bot.llm.script(call("delete_last_expense"))
    assert "Delete this entry?" in bot.send(SAGAR, "Na, 150 hata")
    assert len(expenses_in_db()) == 1


def test_edit_amount_must_be_in_the_latest_message(bot):
    _tea(bot)
    bot.llm.script(call("edit_last_expense", amount=200), say("Ketla hata?"))
    assert "Ketla hata?" in bot.send(SAGAR, "Na, 150 hata")
    assert [int(e.amount) for e in expenses_in_db()] == [100]


def test_edit_item_only(bot):
    _tea(bot)
    bot.llm.script(call("edit_last_expense", amount=0, description="coffee"))
    assert "₹100 — tea  →  ₹100 — coffee" in bot.send(SAGAR, "tea nahi coffee hati")
    bot.send(SAGAR, "yes")
    assert [e.description for e in expenses_in_db()] == ["coffee"]


def test_edit_nothing_new_changes_nothing(bot):
    _tea(bot)
    bot.llm.script(call("edit_last_expense", amount=100))
    assert "already ₹100 — tea" in bot.send(SAGAR, "100 j hata")


def test_edit_only_ones_own_entry(bot):
    _tea(bot)                                  # entered by Sagar
    bot.llm.script(call("edit_last_expense", amount=150))
    assert "No recent unsettled entry by Biren to change" in bot.send(BIREN, "na, 150 hata")


def test_edit_of_an_entry_settled_meanwhile_changes_nothing(bot):
    _tea(bot)
    bot.llm.script(call("edit_last_expense", amount=150))
    bot.send(SAGAR, "Na, 150 hata")
    with session_scope() as s:
        s.execute(update(Expense).values(deleted_at=datetime.now(timezone.utc)))
    assert "Nothing changed" in bot.send(SAGAR, "yes")
