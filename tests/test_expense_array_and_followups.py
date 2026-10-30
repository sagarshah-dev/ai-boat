"""add_expenses(items=[...]): one call for a whole list, and the next message after a question.

Live bugs 29-10-2026:
- "150 tea, 250 vadapav, 500 sevsad, coffee all by sagar": 1 of 3 saved even after a retry.
  The model made one parallel call and stopped; now the list is ONE call with an array.
- "..., coffee by lalo" -> "No amount for coffee" -> user sends "50" -> "what was ₹50 for?".
  The question was only text, nothing waited for the answer. Every bot question now has a
  test for the message that answers it.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.db.models import Member, PendingAction
from app.db.session import session_scope
from tests.bot_helpers import BIREN, GROUP, LALO, SAGAR, expenses_in_db
from tests.conftest import call, say


def item(amount, description, paid_by="Sagar", category="Food"):
    return {"amount": amount, "description": description, "paid_by": paid_by, "category": category}


def adds(*items):
    return call("add_expenses", items=list(items))


def rows():
    with session_scope() as s:
        names = {m.id: m.name for m in s.scalars(select(Member))}
    return [(int(r.amount), r.description, names[r.paid_by]) for r in expenses_in_db()]


def waiting(user=SAGAR, chat=None):
    with session_scope() as s:
        m = s.scalar(select(Member).where(Member.telegram_user_id == user))
        p = s.scalar(select(PendingAction).where(PendingAction.member_id == m.id,
                                                PendingAction.kind == "amount",
                                                PendingAction.chat_id == (chat or user)))
        return p.payload["message"] if p else None


LIVE = "150 tea by lalo, 250 vadapav by sagar, 500 sevsad by biren, coffee by lalo"


# ── One call, many items ─────────────────────────────────────────────────────────────────

def test_array_saves_every_item_with_one_model_call(bot):
    bot.llm.script(adds(item(150, "tea"), item(250, "vadapav"), item(500, "sevsad")))
    reply = bot.send(SAGAR, "150 tea, 250 vadapav, 500 sevsad all by sagar")
    assert rows() == [(150, "tea", "Sagar"), (250, "vadapav", "Sagar"), (500, "sevsad", "Sagar")]
    assert bot.llm.chats[-1].tool_results == []   # templates, no second model call
    assert reply.count("✅ Logged") == 3 and "Not saved" not in reply


def test_each_array_item_passes_the_gates_on_its_own(bot):
    # 999 is not in the message: only that item is refused, the rest is saved.
    bot.llm.script(adds(item(150, "tea"), item(999, "vadapav")), say("ok"))
    reply = bot.send(SAGAR, "150 tea, 250 vadapav")
    assert rows() == [(150, "tea", "Sagar")]
    assert '"250 vadapav"' in reply


def test_incomplete_array_retries_once_with_the_stronger_model(bot):
    bot.llm.script(adds(item(150, "tea")),
                   adds(item(250, "vadapav"), item(500, "sevsad")))
    bot.send(SAGAR, "150 tea, 250 vadapav, 500 sevsad")
    chat = bot.llm.chats[-1]
    assert len(rows()) == 3 and chat.escalated == 1
    [results] = chat.tool_results
    name, result = results[0]
    assert name == "add_expenses" and len(result["items"]) == 1
    assert result["not_saved_yet"] == ["250 vadapav", "500 sevsad"]


def test_still_incomplete_after_retry_is_reported(bot):
    bot.llm.script(adds(item(150, "tea")), say("Done."))
    reply = bot.send(SAGAR, "150 tea, 250 vadapav, 500 sevsad")
    assert len(rows()) == 1 and '⚠️ Not saved: "250 vadapav", "500 sevsad"' in reply
    assert "Done." not in reply


def test_empty_items_asks_for_the_amount(bot):
    bot.llm.script(adds())
    reply = bot.send(SAGAR, "tea by sagar")
    assert rows() == [] and "how much was it?" in reply


def test_old_single_call_still_works(bot):
    bot.llm.script(call("add_expense", amount=150, description="tea", paid_by="Sagar"))
    bot.send(SAGAR, "150 tea")
    assert rows() == [(150, "tea", "Sagar")]


class _Keep(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_each_round_is_logged(bot):
    # The app's startup replaces the root handlers, so listen on the agent's logger itself.
    keep, log = _Keep(), logging.getLogger("app.ai.agent")
    log.addHandler(keep)
    # alembic's fileConfig (run by the test setup) disables loggers that already exist.
    old, was_off = log.level, log.disabled
    log.setLevel(logging.INFO)
    log.disabled = False
    try:
        bot.llm.script(adds(item(150, "tea"), item(999, "vadapav")), say("ok"))
        bot.send(SAGAR, "150 tea, 250 vadapav")
    finally:
        log.removeHandler(keep)
        log.setLevel(old)
        log.disabled = was_off
    rec = next(r for r in keep.records if r.getMessage() == "tool round")
    assert [c["result"] for c in rec.fields["calls"]] == ["ok", "needs_amount"]
    assert rec.fields["calls"][0]["amount"] == "150"


# ── The answer to "No amount for coffee" (live screenshot) ───────────────────────────────

def _live(bot):
    bot.llm.script(adds(item(150, "tea", "Lalo"), item(250, "vadapav"), item(500, "sevsad", "Biren"),
                        item(0, "coffee", "Lalo")))
    return bot.send(SAGAR, LIVE)


def test_live_item_without_amount_waits_for_the_amount(bot):
    reply = _live(bot)
    assert rows() == [(150, "tea", "Lalo"), (250, "vadapav", "Sagar"), (500, "sevsad", "Biren")]
    assert "No amount for coffee. How much was it?" in reply
    assert waiting() == "coffee by lalo"


def test_live_plain_amount_completes_it_for_the_right_payer(bot):
    _live(bot)
    bot.llm.script(adds(item(50, "coffee", "Lalo")))
    reply = bot.send(SAGAR, "50")
    assert bot.llm.chats[-1].user_messages[-1] == "coffee by lalo 50"
    assert rows()[-1] == (50, "coffee", "Lalo") and "✅ Logged" in reply
    assert waiting() is None


def test_item_and_amount_reply_is_the_same_entry_not_a_new_one(bot):
    _live(bot)
    bot.llm.script(adds(item(50, "coffee", "Lalo")))
    bot.send(SAGAR, "coffee 50")
    assert bot.llm.chats[-1].user_messages[-1] == "coffee by lalo 50"
    assert rows()[-1] == (50, "coffee", "Lalo") and len(rows()) == 4


def test_wrong_answer_uses_a_try_and_keeps_waiting(bot):
    _live(bot)
    bot.llm.script(say("Sorry, what do you mean?"))
    reply = bot.send(SAGAR, "abc")
    assert "Still waiting for the amount" in reply and "2 tries left" in reply
    assert waiting() == "coffee by lalo"


def test_expired_question_does_not_complete(bot):
    _live(bot)
    with session_scope() as s:
        s.execute(update(PendingAction).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    bot.llm.script(adds(item(50, "")))
    reply = bot.send(SAGAR, "50")
    assert len(rows()) == 3 and "what was ₹50 for?" in reply


def test_someone_elses_amount_does_not_complete_it(bot):
    bot.llm.script(adds(item(150, "tea", "Lalo"), item(0, "coffee", "Lalo")))
    bot.send(SAGAR, "150 tea by lalo, coffee by lalo", chat_id=GROUP)
    bot.llm.script(adds(item(50, "", "Biren")))
    bot.send(BIREN, "50", chat_id=GROUP)
    assert len(rows()) == 1 and waiting(SAGAR, GROUP) == "coffee by lalo"


def test_balance_in_between_keeps_the_question(bot):
    _live(bot)
    reply = bot.send(SAGAR, "balance")
    assert "Balance" in reply and waiting() == "coffee by lalo"
    bot.llm.script(adds(item(50, "coffee", "Lalo")))
    bot.send(SAGAR, "50")
    assert rows()[-1] == (50, "coffee", "Lalo")


def test_one_payer_named_elsewhere_is_carried_to_the_question(bot):
    bot.llm.script(adds(item(150, "tea", "Lalo"), item(0, "coffee", "Lalo")))
    bot.send(SAGAR, "150 tea, coffee all by lalo")
    assert waiting() == "coffee all by lalo"
    bot.llm.script(adds(item(150, "tea", "Lalo"), item(0, "coffee")))
    bot.send(SAGAR, "150 tea by lalo, coffee")
    assert waiting() == "coffee by Lalo"


def test_two_items_without_amount_asks_first_and_names_the_second(bot):
    bot.llm.script(adds(item(150, "tea"), item(0, "coffee"), item(0, "nasta")))
    reply = bot.send(SAGAR, "150 tea, coffee, nasta")
    assert "No amount for coffee. How much was it?" in reply
    assert "No amount for nasta either" in reply and waiting() == "coffee"


def test_unclear_payer_for_the_item_is_not_remembered(bot):
    bot.llm.script(adds(item(150, "tea", "Lalo"), item(250, "vada", "Biren"), item(0, "coffee")))
    reply = bot.send(SAGAR, "150 tea by lalo, 250 vada by biren, coffee")
    assert waiting() is None and 'e.g. "coffee 100"' in reply


def test_voice_list_is_one_preview(bot):
    bot.llm.script(say("150 tea ane 400 diesel"), adds(item(150, "tea"), item(400, "diesel")))
    reply = bot.voice(SAGAR)
    assert "Log these expenses?" in reply and "1. ₹150" in reply and "2. ₹400" in reply
    assert rows() == []
    bot.send(SAGAR, "yes")
    assert [r[1] for r in rows()] == ["tea", "diesel"]


# ── Regressions caught by the real-model eval, 29-10-2026 ──────────────────────────────────

def test_eval_and_between_two_payers_without_comma(bot):
    # PR #6 read only commas, so "... by sagar and ... by biren" looked like two names for one
    # amount and nothing was saved.
    bot.llm.script(adds(item(200, "tea"), item(500, "diesel", "Biren")))
    bot.send(SAGAR, "200 tea by sagar and 500 diesel by biren")
    assert rows() == [(200, "tea", "Sagar"), (500, "diesel", "Biren")]


def test_eval_and_inside_one_item_is_still_one_entry(bot):
    bot.llm.script(adds(item(200, "tea and nasta")))
    reply = bot.send(SAGAR, "tea and nasta 200 by lalo")
    assert rows() == [(200, "tea and nasta", "Lalo")] and "Not saved" not in reply


def test_eval_amount_reply_keeps_its_payer(bot):
    # PR #7 treated "biren" in "100 by biren" as an item word and glued "tea 100": the payer was
    # dropped, then refused as a guess.
    bot.llm.script(adds(item(0, "tea")))
    bot.send(SAGAR, "tea")
    bot.llm.script(adds(item(100, "tea", "Biren")))
    bot.send(SAGAR, "100 by biren")
    assert bot.llm.chats[-1].user_messages[-1] == "tea 100 by biren"
    assert rows() == [(100, "tea", "Biren")]
