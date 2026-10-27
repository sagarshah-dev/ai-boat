"""Exact command words (no model involved) and the settle-up flow."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update

from app.db.models import Expense, PendingAction, Settlement, SettlementTransfer
from app.db.session import session_scope
from tests.bot_helpers import BIREN, GROUP, LALO, RAMESH, SAGAR, add_rows, expenses_in_db
from tests.conftest import SETTLE_PIN, call


# ── Balance ────────────────────────────────────────────────────────────────────────────────

def test_balance_equal_split_between_sharing_members_only(bot):
    add_rows(("Sagar", 300, "seed"), ("Biren", 150, "diesel"))
    reply = bot.send(SAGAR, "balance")
    assert reply == (
        "💰 Balance Status\n\n"
        "Total Expenses: ₹450\n"
        "Fair Share (each): ₹150\n\n"
        "Sagar paid ₹300 → gets back ₹150\n"
        "Biren paid ₹150 → settled\n"
        "Lalo paid ₹0 → pending ₹150\n\n"
        "🔄 Settlement Suggestion:\n"
        "Lalo pays Sagar ₹150")
    assert "Ramesh" not in reply            # the guest is never in the split
    assert bot.llm.chats == []             # no model for a command word


def test_balance_rounding_never_creates_a_one_rupee_gap(bot):
    add_rows(("Sagar", 100, "a"))           # 100 / 3 = 33.33...
    reply = bot.send(SAGAR, "bal")
    assert "Biren pays Sagar ₹33" in reply and "Lalo pays Sagar ₹33" in reply


def test_balance_ignores_deleted_and_settled(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "undo")
    assert "Total Expenses: ₹0" in bot.send(SAGAR, "balance")
    assert "✅ Everyone is settled!" in bot.send(SAGAR, "balance")


def test_slash_command_with_bot_name_works(bot):
    assert "Balance Status" in bot.send(SAGAR, "/balance@AIBoatHQ_bot", chat_id=GROUP)


# ── Recent, undo, filters ──────────────────────────────────────────────────────────────────

def test_recent_shows_last_five_newest_first(bot):
    add_rows(*[("Sagar", 10 * i, f"item{i}") for i in range(1, 8)])
    reply = bot.send(BIREN, "recent")
    assert reply.startswith("🕐 Last 5 entries:")
    assert reply.index("item7") < reply.index("item3") and "item2" not in reply


def test_undo_removes_only_own_latest_and_keeps_the_row(bot):
    add_rows(("Sagar", 100, "sagar first"), by=SAGAR)
    add_rows(("Biren", 200, "biren entry"), by=BIREN)
    reply = bot.send(SAGAR, "undo")
    assert reply.startswith("↩️ Undone: #1 — ₹100 — sagar first")
    assert [e.description for e in expenses_in_db()] == ["biren entry"]
    deleted = [e for e in expenses_in_db(include_deleted=True) if e.deleted_at]
    assert [e.description for e in deleted] == ["sagar first"]   # soft delete: history kept


def test_undo_never_touches_settled_entries(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, f"settle {SETTLE_PIN}")
    assert bot.send(SAGAR, "undo") == "No recent unsettled entry by Sagar to undo."


def test_filter_by_name_and_category(bot):
    add_rows(("Sagar", 100, "diesel", "Fuel"), ("Biren", 50, "petrol", "Fuel"),
             ("Sagar", 70, "tea", "Food"))
    by_name = bot.send(LALO, "sagar")
    assert "diesel" in by_name and "tea" in by_name and "petrol" not in by_name
    assert "Total: ₹170" in by_name
    by_cat = bot.send(LALO, "fuel expenses")
    assert "diesel" in by_cat and "petrol" in by_cat and "tea" not in by_cat
    assert bot.llm.chats == []


def test_ordinary_sentence_is_not_mistaken_for_a_filter(bot):
    bot.llm.script(call("get_balances"))
    bot.send(SAGAR, "sagar balance")
    assert len(bot.llm.chats) == 1


def test_help(bot):
    assert "AI BOaT" in bot.send(RAMESH, "/help")


def test_photo_that_cannot_be_downloaded(bot):
    update = {"update_id": 5, "message": {"message_id": 1, "from": {"id": SAGAR},
                                          "chat": {"id": SAGAR}, "photo": [{"file_id": "gone"}]}}
    assert "Couldn't download that photo" in bot.post(update, SAGAR)
    assert bot.llm.chats == []


# ── Several lines = several expenses ───────────────────────────────────────────────────────

def test_multi_line_saves_good_lines_and_lists_bad_ones(bot):
    bot.llm.script(
        call("add_expense", amount=150, description="tea", paid_by="Sagar"),
        call("add_expense", amount=10, description="milk", paid_by="Sagar"),   # not in the text
        call("add_expense", amount=400, description="diesel", paid_by="Biren"))
    reply = bot.send(SAGAR, "150 tea\nmilk\n400 diesel by biren")
    assert [e.description for e in expenses_in_db()] == ["tea", "diesel"]
    assert reply.startswith("✅ Logged 2 entries:")
    assert '"milk"' in reply
    with session_scope() as s:                       # a batch never leaves a question waiting
        assert s.scalar(select(PendingAction)) is None


# ── Settle ─────────────────────────────────────────────────────────────────────────────────

def _settled_counts():
    with session_scope() as s:
        return (len(list(s.scalars(select(Settlement)))),
                len(list(s.scalars(select(SettlementTransfer)))))


def test_settle_two_step_pin(bot):
    add_rows(("Sagar", 300, "seed"))
    assert bot.send(BIREN, "settle") == "🔐 Enter the settle PIN to confirm:"
    reply = bot.send(BIREN, SETTLE_PIN)
    assert reply.startswith("✅ Settled! (#1)")
    assert "Biren pays Sagar ₹100" in reply and "Lalo pays Sagar ₹100" in reply
    assert all(e.settlement_id == 1 for e in expenses_in_db())
    assert _settled_counts() == (1, 2)
    assert "✅ Everyone is settled!" in bot.send(SAGAR, "balance")


def test_settle_wrong_pin_cancels_and_is_one_attempt(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "settle")
    assert "Incorrect PIN" in bot.send(SAGAR, "1234")
    # The next message is ordinary again, not a second PIN try.
    assert "Balance Status" in bot.send(SAGAR, "balance")
    assert _settled_counts() == (0, 0)


def test_settle_pin_expired(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "settle")
    with session_scope() as s:
        s.execute(update(PendingAction).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert "timed out" in bot.send(SAGAR, SETTLE_PIN)
    assert bot.llm.chats == []               # the late PIN never reached the model
    assert _settled_counts() == (0, 0)


def test_pin_prompt_is_per_person_in_a_group(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "settle", chat_id=GROUP)
    bot.llm.script(call("get_balances"))
    bot.send(BIREN, SETTLE_PIN, chat_id=GROUP)   # Biren never asked to settle
    assert _settled_counts() == (0, 0)


def test_settle_one_line_form(bot):
    add_rows(("Sagar", 300, "seed"))
    assert bot.send(SAGAR, f"settle {SETTLE_PIN}").startswith("✅ Settled!")
    assert bot.send(SAGAR, "settle 0000") == "❌ Send exactly: settle <PIN>"


def test_settle_with_nothing_open(bot):
    reply = bot.send(SAGAR, f"settle {SETTLE_PIN}")
    assert reply == "✅ Nothing to settle — there are no unsettled expenses."
    assert _settled_counts() == (0, 0)


def test_settle_only_marks_the_rows_it_counted(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, f"settle {SETTLE_PIN}")
    add_rows(("Biren", 90, "after settle"))
    open_rows = [e for e in expenses_in_db() if e.settlement_id is None]
    assert [e.description for e in open_rows] == ["after settle"]
    assert "Total Expenses: ₹90" in bot.send(SAGAR, "balance")


def test_settle_switched_off_without_pin(bot, monkeypatch):
    from app.config import get_settings
    from app.main import app
    settings = get_settings().model_copy(update={"settle_pin": ""})
    app.dependency_overrides[get_settings] = lambda: settings
    assert "switched off" in bot.send(SAGAR, "settle")
    assert "Send exactly" in bot.send(SAGAR, "settle 7777")


def test_settle_amounts_are_exact_decimal(bot):
    add_rows(("Sagar", Decimal("100.10"), "a"), ("Biren", Decimal("0.20"), "b"))
    reply = bot.send(SAGAR, "balance")
    assert "Total Expenses: ₹100" in reply
    with session_scope() as s:
        assert s.scalar(select(Expense.amount).where(Expense.description == "a")) == Decimal("100.10")


def test_expense_added_while_settling_stays_open(bot, monkeypatch):
    """An expense that lands after settle took its snapshot must not be marked settled —
    nobody saw it in the plan, so it was never paid."""
    from app.services import balance as balance_service
    from app.services import settlements
    real_compute = balance_service.compute

    def compute_then_someone_adds(session, lock=False):
        snapshot = real_compute(session, lock=lock)
        add_rows(("Biren", 90, "typed during settle"))   # separate transaction, commits first
        return snapshot
    monkeypatch.setattr(settlements.balance_service, "compute", compute_then_someone_adds)
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, f"settle {SETTLE_PIN}")
    still_open = [e.description for e in expenses_in_db() if e.settlement_id is None]
    assert still_open == ["typed during settle"]
