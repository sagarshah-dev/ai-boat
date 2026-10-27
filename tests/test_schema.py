"""The schema itself: migrations match the models, and the database enforces the money rules."""
from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models import Expense, Member
from app.db.session import session_scope
from app.main import app
from tests.conftest import alembic_config


def test_migrations_match_models():
    # Fails when someone edits models.py and forgets to write a migration.
    command.check(alembic_config())


def test_downgrade_then_upgrade_works():
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def _member(session, tid=1, role="admin") -> Member:
    m = Member(telegram_user_id=tid, name=f"m{tid}", role=role, status="active")
    session.add(m)
    session.flush()
    return m


def test_amount_is_exact_decimal_not_float():
    with session_scope() as s:
        m = _member(s)
        for _ in range(3):  # 0.1 + 0.2 style float errors would show up here
            s.add(Expense(expense_date=date(2026, 10, 27), amount=Decimal("33.33"),
                          description="tea", paid_by=m.id, entered_by=m.id))
    with session_scope() as s:
        total = s.execute(text("SELECT sum(amount) FROM expenses")).scalar()
    assert total == Decimal("99.99")


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-5")])
def test_zero_or_negative_expense_is_refused_by_the_database(amount):
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            m = _member(s)
            s.add(Expense(expense_date=date(2026, 10, 27), amount=amount,
                          description="x", paid_by=m.id, entered_by=m.id))


def test_unknown_role_is_refused_by_the_database():
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            _member(s, role="superuser")


def test_expense_needs_a_real_member():
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(Expense(expense_date=date(2026, 10, 27), amount=Decimal("10"),
                          description="x", paid_by=999, entered_by=999))


def test_health_reports_database_ok():
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "revision": "local"}
