"""Who paid, and lists of expenses in one message.

Live bugs, 28/29-10-2026 (AI BOaT had no Lalo/Biren payers yet):
  "150 tea, 250 vadapav, 500 sevsad, coffee 300 all by lalo"  -> 1 entry, paid by Sagar, no warning
  "150 tea, 250 vadapav, 500 sevsad, coffee 300 all by biren" -> 4 entries, all paid by Sagar
  "150 tea, 250 vadapav, 500 sevsad, coffee all by sagar"     -> "sevsad, coffee" ₹500, never asked
The model is scripted to make those same mistakes; the tests check that the CODE catches them.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.db.models import Member, PendingAction
from app.db.session import session_scope
from app.services import members as member_service
from tests.bot_helpers import BIREN, LALO, RAMESH, SAGAR, expenses_in_db, member_id
from tests.conftest import call, calls

LIST = "150 tea, 250 vadapav, 500 sevsad, coffee 300"


def add(amount, description, paid_by="Sagar", category="Food"):
    return ("add_expense", {"amount": amount, "description": description, "paid_by": paid_by,
                            "category": category})


@pytest.fixture
def nick(bot):
    """The nicknames production gets from migration 0003."""
    with session_scope() as s:
        s.execute(update(Member).where(Member.name == "Lalo").values(aliases=["lalo", "jignesh", "લાલો"]))
        s.execute(update(Member).where(Member.name == "Biren").values(aliases=["biren", "bako", "બિરેન"]))
        s.execute(update(Member).where(Member.name == "Sagar").values(aliases=["sagar", "સાગર"]))
    return bot


def payers():
    names = {}
    with session_scope() as s:
        names = {m.id: m.name for m in s.scalars(select(Member))}
    return [(int(r.amount), r.description, names[r.paid_by]) for r in expenses_in_db()]


# ── A. Who paid ────────────────────────────────────────────────────────────────────────────

def test_live_bug_all_by_biren_saves_all_four_for_biren(nick):
    # The model (enum had only Sagar) said Sagar four times. The user wrote "biren".
    nick.llm.script(calls(add(150, "tea"), add(250, "vadapav"), add(500, "sevsad"), add(300, "coffee")))
    nick.send(SAGAR, LIST + " all by biren")
    assert payers() == [(150, "tea", "Biren"), (250, "vadapav", "Biren"),
                        (500, "sevsad", "Biren"), (300, "coffee", "Biren")]


@pytest.mark.parametrize("text,who", [
    ("500 diesel by jignesh", "Lalo"),
    ("500 diesel by bako", "Biren"),
    ("લાલો એ 500 diesel", "Lalo"),
    ("લાલોએ 500 diesel", "Lalo"),
    ("500 diesel", "Sagar"),            # nobody named -> the sender
    ("500 diesel by card", "Sagar"),    # how it was paid, not who
])
def test_payer_comes_from_the_users_words(nick, text, who):
    nick.llm.script(call("add_expense", amount=500, description="diesel", paid_by="Sagar",
                         category="Fuel"))
    nick.send(SAGAR, text)
    assert payers() == [(500, "diesel", who)]


def test_unknown_name_saves_nothing_and_asks(nick):
    nick.llm.script(call("add_expense", amount=500, description="diesel", paid_by="Sagar"))
    reply = nick.send(SAGAR, "500 diesel by bhavesh")
    assert expenses_in_db() == []
    assert "Not saved: ₹500 diesel — who paid?" in reply and "'bhavesh'" in reply


def test_typo_name_is_not_corrected_by_the_model(nick):
    # Gemini "fixes" biran -> Biren. Close is not the same: ask.
    nick.llm.script(call("add_expense", amount=500, description="diesel", paid_by="Biren"))
    reply = nick.send(SAGAR, "500 diesel by biran")
    assert expenses_in_db() == [] and "'biran'" in reply


def test_ne_means_given_to_so_ask(nick):
    nick.llm.script(call("add_expense", amount=500, description="labour", paid_by="Lalo"))
    reply = nick.send(SAGAR, "lalo ne 500 labour aapya")
    assert expenses_in_db() == [] and "TO Lalo" in reply


def test_two_names_one_amount_asks(nick):
    nick.llm.script(call("add_expense", amount=500, description="diesel", paid_by="Sagar"))
    reply = nick.send(SAGAR, "500 diesel by sagar and biren")
    assert expenses_in_db() == [] and "More than one person" in reply


def test_each_part_keeps_its_own_payer(nick):
    nick.llm.script(calls(add(150, "tea", "Sagar"), add(250, "vadapav", "Sagar")))
    nick.send(SAGAR, "tea 150 by sagar, vadapav 250 by biren")
    assert payers() == [(150, "tea", "Sagar"), (250, "vadapav", "Biren")]


def test_model_naming_someone_unwritten_is_a_guess(nick):
    nick.llm.script(call("add_expense", amount=500, description="diesel", paid_by="Lalo"))
    reply = nick.send(SAGAR, "500 diesel")
    assert expenses_in_db() == [] and "does not say who paid" in reply


def test_guest_must_name_the_payer(nick):
    # Whatever the model fills in, a guest's "100 tea" names nobody who shares: ask.
    nick.llm.script(call("add_expense", amount=100, description="tea", paid_by="Sagar"))
    reply = nick.send(RAMESH, "100 tea")
    assert expenses_in_db() == [] and "who paid?" in reply


# ── B/C. Lists: nothing is dropped silently ─────────────────────────────────────────────────

def test_live_bug_model_adds_one_of_four_then_adds_the_rest_when_told(nick):
    nick.llm.script(calls(add(150, "tea", "Sagar")),
                    calls(add(250, "vadapav"), add(500, "sevsad"), add(300, "coffee")))
    nick.send(SAGAR, LIST + " all by lalo")
    assert [p[2] for p in payers()] == ["Lalo"] * 4 and len(payers()) == 4
    # The second round was asked for with exactly the missing parts.
    told = nick.llm.chats[-1].tool_results[-1]
    assert "250 vadapav" in str(told) and "not_saved_yet" in str(told)


def test_live_bug_model_still_skips_them_then_user_is_told(nick):
    nick.llm.script(calls(add(150, "tea")), calls())
    reply = nick.send(SAGAR, LIST + " all by lalo")
    assert len(payers()) == 1
    assert '⚠️ Not saved: "250 vadapav", "500 sevsad", "coffee 300 all by lalo"' in reply


def test_same_amount_twice_needs_two_entries(nick):
    nick.llm.script(calls(add(100, "tea")), calls())
    reply = nick.send(SAGAR, "100 tea, 100 coffee")
    assert '"100 coffee"' in reply


def test_one_bill_for_two_things_is_one_entry(nick):
    nick.llm.script(calls(add(200, "tea and nasta")))
    reply = nick.send(SAGAR, "tea and nasta 200")
    assert payers() == [(200, "tea and nasta", "Sagar")] and "Not saved" not in reply


def test_digit_grouping_is_not_a_list(nick):
    nick.llm.script(calls(add(112412, "borewell")))
    reply = nick.send(SAGAR, "₹1,12,412 borewell")
    assert len(payers()) == 1 and "Not saved" not in reply


def test_total_line_is_not_a_missing_entry(nick):
    nick.llm.script(calls(add(150, "tea"), add(300, "coffee")))
    reply = nick.send(SAGAR, "150 tea, 300 coffee, total 450")
    assert len(payers()) == 2 and "Not saved" not in reply


def test_item_without_amount_is_reported(nick):
    nick.llm.script(calls(add(150, "tea"), add(250, "vadapav"), add(500, "sevsad"),
                          add(0, "coffee")))
    reply = nick.send(SAGAR, "150 tea, 250 vadapav, 500 sevsad, coffee all by sagar")
    assert len(payers()) == 3
    assert "No amount for coffee, so it was not saved" in reply


# ── B1. "500 sevsad, coffee": together or forgotten? ────────────────────────────────────────

def _joined(nick):
    nick.llm.script(calls(add(150, "tea"), add(250, "vadapav"), add(500, "sevsad, coffee")))
    return nick.send(SAGAR, "150 tea, 250 vadapav, 500 sevsad, coffee all by sagar")


def test_live_bug_joined_item_is_asked_not_saved(nick):
    reply = _joined(nick)
    assert [p[1] for p in payers()] == ["tea", "vadapav"]
    assert "Is ₹500 for both sevsad and coffee?" in reply
    assert "No amount for coffee" not in reply   # one question, not two


def test_joined_item_together(nick):
    _joined(nick)
    nick.tap(SAGAR, "1")
    assert payers()[-1] == (500, "sevsad, coffee", "Sagar")


def test_joined_item_separate_then_amount(nick):
    _joined(nick)
    reply = nick.tap(SAGAR, "2")
    assert payers()[-1] == (500, "sevsad", "Sagar") and '"coffee" — how much' in reply
    nick.llm.script(call("add_expense", amount=300, description="coffee", paid_by="Sagar"))
    nick.send(SAGAR, "300")
    assert payers()[-1] == (300, "coffee", "Sagar") and len(payers()) == 4


def test_joined_item_yes_must_pick(nick):
    _joined(nick)
    reply = nick.send(SAGAR, "yes")
    assert "Tap the number" in reply and len(payers()) == 2


# ── Fixed payers: seeded rows and later /start ─────────────────────────────────────────────

def test_seeded_payer_without_telegram_id_shares_but_cannot_log_in(bot):
    with session_scope() as s:
        s.execute(update(Member).where(Member.name == "Lalo").values(telegram_user_id=None))
    bot.llm.script(call("add_expense", amount=500, description="diesel", paid_by="Lalo"))
    bot.send(SAGAR, "500 diesel by lalo")
    assert payers() == [(500, "diesel", "Lalo")]
    with session_scope() as s:
        assert member_service.get_active(s, LALO) is None


def test_seeded_owner_is_linked_when_he_joins(bot):
    with session_scope() as s:
        lalo = s.scalar(select(Member).where(Member.name == "Lalo"))
        lalo.telegram_user_id, lalo.aliases = None, ["lalo", "jignesh"]
        seeded_id = lalo.id
    bot.post({"update_id": 99001, "message": {"message_id": 1, "chat": {"id": 5555},
              "from": {"id": 5555, "first_name": "Jignesh", "last_name": "Patel"},
              "text": "/start"}}, 5555)
    with session_scope() as s:
        req = s.scalar(select(Member).where(Member.telegram_user_id == 5555))
        req_id = req.id
    bot.tap(SAGAR, f"join:{req_id}:owner")
    with session_scope() as s:
        owners = s.scalars(select(Member).where(Member.role.in_(("admin", "owner")))).all()
        assert len(owners) == 3        # not a 4th share
        assert s.get(Member, seeded_id).telegram_user_id == 5555
