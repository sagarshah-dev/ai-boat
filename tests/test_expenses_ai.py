"""Typed expenses through the AI path, end to end: Telegram update -> scripted model -> gates ->
database -> reply. The model is scripted, so these tests check OUR code's decisions: what may be
saved, what is asked, and what the user reads."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.formatting import fmt_date, today
from app.db.models import Member, PendingAction
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from app.services import expenses as expense_service
from tests.bot_helpers import BIREN, GROUP, RAMESH, SAGAR, add_rows, expenses_in_db, member_id
from tests.conftest import call, calls, say

TODAY = fmt_date(today("Asia/Kolkata"))


def add(amount, description, paid_by="Sagar", category="Food", **extra):
    return call("add_expense", amount=amount, description=description, paid_by=paid_by,
                category=category, **extra)


# ── Saving ─────────────────────────────────────────────────────────────────────────────────

def test_typed_expense_is_saved_with_one_model_call(bot):
    bot.llm.script(add(150, "tea"))
    reply = bot.send(SAGAR, "150 tea by sagar")
    assert reply == f"✅ Logged #1: ₹150 for tea, paid by Sagar [Food] — {TODAY}"
    [row] = expenses_in_db()
    assert (row.amount, row.description, row.category) == (Decimal("150.00"), "tea", "Food")
    assert row.paid_by == member_id(SAGAR) and row.entered_by == member_id(SAGAR)
    # Template reply: the result never went back to the model for wording.
    assert bot.llm.last.tool_results == []
    assert bot.tg.typing == [SAGAR]


def test_payer_defaults_to_sender_and_entered_by_is_sender(bot):
    bot.llm.script(add(500, "diesel", paid_by="", category="Fuel"))
    bot.send(BIREN, "500 diesel")
    [row] = expenses_in_db()
    assert row.paid_by == member_id(BIREN) and row.entered_by == member_id(BIREN)


def test_first_name_of_a_full_telegram_name_matches(bot):
    with session_scope() as s:
        s.get(Member, member_id(BIREN)).name = "Biren Patel"
    bot.llm.script(add(80, "milk", paid_by="biren"))
    reply = bot.send(SAGAR, "80 milk by biren")
    assert "paid by Biren Patel" in reply


def test_explicit_date_and_merchant_are_kept(bot):
    bot.llm.script(add(1200, "pump repair", category="House", date="05-10-2026", merchant="Patel Motors"))
    reply = bot.send(SAGAR, "1,200 pump repair at patel motors on 5 oct")
    [row] = expenses_in_db()
    assert row.expense_date == date(2026, 10, 5) and row.merchant == "Patel Motors"
    assert "₹1,200" in reply and "05-10-2026" in reply


def test_unknown_category_becomes_misc(bot):
    bot.llm.script(add(90, "rope", category="Hardware"))
    bot.send(SAGAR, "90 rope")
    assert expenses_in_db()[0].category == "Misc"


def test_identical_call_twice_in_one_turn_saves_once(bot):
    a = {"amount": 150, "description": "tea", "paid_by": "Sagar", "category": "Food"}
    bot.llm.script(calls(("add_expense", a), ("add_expense", a)))
    reply = bot.send(SAGAR, "150 tea")
    assert len(expenses_in_db()) == 1
    assert reply.count("✅ Logged") == 1


def test_two_different_expenses_in_one_message(bot):
    bot.llm.script(calls(
        ("add_expense", {"amount": 150, "description": "tea", "paid_by": "Sagar"}),
        ("add_expense", {"amount": 400, "description": "diesel", "paid_by": "Sagar"})))
    reply = bot.send(SAGAR, "150 tea and 400 diesel")
    assert len(expenses_in_db()) == 2 and reply.count("✅ Logged") == 2


# ── Grounding: the amount must be in the user's words ──────────────────────────────────────

@pytest.mark.parametrize("text,model_amount", [
    ("tea by sagar", 10),              # the original hallucination bug
    ("5 kg khatar", 5),                # quantity, not money
    ("20 litre diesel", 20),
    ("tea on 10-10-2026", 10),         # a date, not money
    ("2 lakh tractor", 2),             # old bot accepted ₹2 here
])
def test_amount_not_written_is_never_saved_and_is_asked(bot, text, model_amount):
    bot.llm.script(add(model_amount, "x item"))
    reply = bot.send(SAGAR, text)
    assert expenses_in_db() == []
    assert f'"{text}" — how much was it?' in reply


@pytest.mark.parametrize("text,amount,shown", [
    ("2 lakh tractor", 200000, "₹2,00,000"),
    ("1.5k labour", 1500, "₹1,500"),
    ("₹1,12,412 borewell", 112412, "₹1,12,412"),
    ("tractor 3 crore", 30000000, "₹3,00,00,000"),
])
def test_amounts_with_units_are_grounded(bot, text, amount, shown):
    bot.llm.script(add(amount, text.split()[-1]))
    reply = bot.send(SAGAR, text)
    assert expenses_in_db()[0].amount == Decimal(amount)
    assert shown in reply


def test_arithmetic_is_refused_and_model_explains(bot):
    bot.llm.script(add(100, "tea"), say("Please send the total, e.g. 100 tea."))
    reply = bot.send(SAGAR, "2 x 50 tea")
    assert expenses_in_db() == []
    assert reply == "Please send the total, e.g. 100 tea."
    assert "calculation" in str(bot.llm.last.tool_results[0])


def test_no_item_is_asked_by_code(bot):
    bot.llm.script(add(200, "200"))
    reply = bot.send(SAGAR, "200 by sagar")
    assert expenses_in_db() == []
    assert reply == '❌ Nothing saved: what was ₹200 for?\nSend it with the item, e.g. "200 tea".'


def test_gujarati_one_letter_item_counts(bot):
    bot.llm.script(add(30, "ચા"))
    bot.send(SAGAR, "30 ચા")
    assert expenses_in_db()[0].description == "ચા"


# ── "How much was it?" — remembered in the database ────────────────────────────────────────

def test_missing_amount_then_amount_completes_the_entry(bot):
    bot.llm.script(add(0, "tea"))
    assert "how much was it?" in bot.send(SAGAR, "tea by sagar")
    bot.llm.script(add(100, "tea"))
    reply = bot.send(SAGAR, "100")
    assert bot.llm.last.user_messages == ["tea by sagar 100"]   # glued, then every gate again
    assert "✅ Logged #1: ₹100 for tea" in reply
    with session_scope() as s:
        assert s.scalar(select(PendingAction)) is None


def test_waiting_amount_belongs_to_the_sender_only(bot):
    bot.llm.script(add(0, "tea"))
    bot.send(SAGAR, "tea by sagar", chat_id=GROUP)
    # Biren's "200" in the same group is NOT glued to Sagar's tea.
    bot.llm.script(add(200, "200"))
    reply = bot.send(BIREN, "200", chat_id=GROUP)
    assert bot.llm.last.user_messages == ["200"]
    assert "what was ₹200 for?" in reply
    assert expenses_in_db() == []


def test_three_bad_answers_give_up(bot):
    bot.llm.script(add(0, "tea"))
    bot.send(SAGAR, "tea by sagar")
    bot.llm.script(add(0, "tea"))
    assert "2 tries left" in bot.send(SAGAR, "0 rs")
    bot.llm.script(add(0, "tea"))
    assert "1 try left" in bot.send(SAGAR, "₹0")
    bot.llm.script(add(0, "tea"))
    assert "No valid amount after 3 tries" in bot.send(SAGAR, "0")
    with session_scope() as s:
        assert s.scalar(select(PendingAction)) is None


def test_balance_asked_with_missing_amount_keeps_both(bot):
    # Old bot replaced the balance with the question; now both are shown.
    add_rows(("Sagar", 300, "seed"))
    bot.llm.script(calls(("get_balances", {}), ("add_expense", {"amount": 0, "description": "tea"})))
    reply = bot.send(SAGAR, "hisab aapo ane tea ma kharcho")
    assert "Balance Status" in reply and "how much was it?" in reply


def test_bare_yes_with_nothing_waiting_never_reaches_model(bot):
    assert bot.send(SAGAR, "yes") == "Nothing is waiting for your yes/no right now."
    assert bot.llm.chats == []


# ── Payer rules ────────────────────────────────────────────────────────────────────────────

def test_guest_entry_must_name_a_sharing_payer(bot):
    bot.llm.script(add(100, "tea", paid_by=""), say("Who paid — Sagar, Biren or Lalo?"))
    reply = bot.send(RAMESH, "100 tea")
    assert expenses_in_db() == [] and reply == "Who paid — Sagar, Biren or Lalo?"
    assert "guest" in bot.llm.last.system


def test_guest_can_add_for_an_owner(bot):
    bot.llm.script(add(100, "tea", paid_by="Lalo"))
    bot.send(RAMESH, "100 tea paid by lalo")
    [row] = expenses_in_db()
    assert row.paid_by == member_id(3001) and row.entered_by == member_id(RAMESH)


def test_payer_choices_given_to_model_are_only_sharing_members(bot):
    bot.llm.script(add(100, "tea"))
    bot.send(SAGAR, "100 tea")
    spec = next(t for t in bot.llm.last.tools if t.name == "add_expense")
    assert spec.parameters["properties"]["paid_by"]["enum"] == ["Sagar", "Biren", "Lalo"]


# ── Model mistakes and outages ─────────────────────────────────────────────────────────────

def test_bad_date_goes_back_to_model_which_corrects_it(bot):
    bot.llm.script(add(150, "tea", date="2026-10-05"), add(150, "tea", date="05-10-2026"))
    reply = bot.send(SAGAR, "150 tea on 5 oct")
    assert expenses_in_db()[0].expense_date == date(2026, 10, 5)
    assert reply.startswith("✅ Logged #1")


def test_model_down_before_any_write_says_why(bot):
    bot.llm.script(LlmError("quota"))
    reply = bot.send(SAGAR, "150 tea")
    assert "free AI limit is used up" in reply and "These still work" in reply
    assert expenses_in_db() == []


def test_model_down_after_write_still_reports_the_save(bot):
    bot.llm.script(calls(("add_expense", {"amount": 150, "description": "tea", "paid_by": "Sagar"}),
                         ("search_expenses", {"date_from": "bad"})),
                   LlmError("timeout"))
    reply = bot.send(SAGAR, "150 tea, and what did I spend")
    assert len(expenses_in_db()) == 1
    assert "✅ Logged #1" in reply and "(The change above is saved.)" in reply


def test_crashing_tool_rolls_back_only_itself(bot, monkeypatch):
    real_add = expense_service.add

    def broken(*a, **kw):
        real_add(*a, **kw)              # the row is written...
        raise RuntimeError("disk full")  # ...and then the tool fails
    monkeypatch.setattr(expense_service, "add", broken)
    bot.llm.script(add(150, "tea"), say("Sorry, that failed. Please try again."))
    reply = bot.send(SAGAR, "150 tea")
    assert expenses_in_db() == []        # savepoint rolled back the half-done write
    assert reply == "Sorry, that failed. Please try again."


def test_model_loop_has_a_brake(bot):
    bad = add(150, "tea", date="nope")
    bot.llm.script(bad, bad, bad, bad, bad)
    reply = bot.send(SAGAR, "150 tea")
    assert expenses_in_db() == [] and reply.startswith("Sorry")


# ── Reading ────────────────────────────────────────────────────────────────────────────────

def test_ai_balance_is_the_same_text_as_the_command(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.llm.script(call("get_balances"))
    ai_reply = bot.send(SAGAR, "hisab aapo")
    assert ai_reply == bot.send(SAGAR, "balance")


def test_search_reply_is_a_template_with_code_total(bot):
    add_rows(("Sagar", 300, "diesel", "Fuel"), ("Biren", 200, "diesel", "Fuel"),
             ("Sagar", 999, "tea", "Food"))
    bot.llm.script(call("search_expenses", text="diesel"))
    reply = bot.send(SAGAR, "search diesel")
    assert "📋 2 entries" in reply and "💵 Total: ₹500 (2 entries)" in reply
    assert "tea" not in reply


def test_search_by_dates_and_payer(bot):
    add_rows(("Sagar", 100, "a"), when=date(2026, 9, 20))
    add_rows(("Sagar", 200, "b"), when=date(2026, 10, 2))
    add_rows(("Biren", 400, "c"), when=date(2026, 10, 3))
    # dates given backwards on purpose: code swaps them
    bot.llm.script(call("search_expenses", paid_by="Sagar", date_from="31-10-2026", date_to="01-10-2026"))
    reply = bot.send(SAGAR, "sagar no kharch aa mahine")
    assert "— b —" in reply and "— a —" not in reply and "— c —" not in reply


def test_search_caps_list_but_totals_everything(bot):
    add_rows(*[("Sagar", 10, f"item{i}") for i in range(25)])
    bot.llm.script(call("search_expenses", text="item"))
    reply = bot.send(SAGAR, "search item")
    assert "(newest 20 shown)" in reply and "Total: ₹250 (25 entries)" in reply
    assert reply.count("• #") == 20


def test_search_wildcards_are_literal(bot):
    add_rows(("Sagar", 10, "50% advance"), ("Sagar", 20, "500 advance"))
    bot.llm.script(call("search_expenses", text="50%"))
    reply = bot.send(SAGAR, "search 50%")
    assert "50% advance" in reply and "500 advance" not in reply


def test_undo_through_ai_removes_own_entry(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.llm.script(call("delete_last_expense"))
    reply = bot.send(SAGAR, "galti thai gai, kadhi nakh")
    assert reply.startswith("↩️ Undone: #1 — ₹300 — seed")
    assert expenses_in_db() == []
