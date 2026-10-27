"""Pure rules: amount grounding, item words, Indian number format, the settle-up plan."""
from decimal import Decimal

import pytest

from app.ai import guards
from app.core.formatting import inr, round_rupees
from app.services.balance import Share, settlement_plan

PEOPLE = {"sagar", "biren", "lalo"}


@pytest.mark.parametrize("text,amount,expected", [
    ("150 tea", 150, True),
    ("₹1,200 diesel", 1200, True),
    ("1.5k labour", 1500, True),
    ("2k diesel", 2000, True),
    ("2 lakh tractor", 200000, True),
    ("2 lakh tractor", 2, False),
    ("3 crore", 30000000, True),
    ("100rs tea", 100, True),
    ("tea by sagar", 10, False),
    ("tea on 10-10-2026", 10, False),
    ("5 kg khatar", 5, False),
    ("20 litre diesel", 20, False),
    ("3 bags cement 900", 900, True),
    ("3 bags cement 900", 3, False),
    ("be hajar tea", 2000, False),
    ("12.50 chai", Decimal("12.5"), True),
])
def test_amount_in_text(text, amount, expected):
    assert guards.amount_in_text(Decimal(str(amount)), text) is expected


@pytest.mark.parametrize("text,expected", [
    ("200", False), ("200 by sagar", False), ("200 paid by biren", False), ("rs 200 only", False),
    ("200 tea", True), ("ચા 30", True), ("2 lakh", False), ("diesel", True),
])
def test_has_item_word(text, expected):
    assert guards.has_item_word(text, PEOPLE) is expected


@pytest.mark.parametrize("text,expected", [
    ("100", True), ("₹100", True), ("rs 100", True), ("1,200", True), ("2k", True),
    ("1.5 lakh", True), ("100 by sagar", True), ("100 paid by lalo", True),
    ("100 tea", False), ("100 by ramesh", False), ("hello", False),
])
def test_amount_only_reply(text, expected):
    assert bool(guards.amount_only_pattern(PEOPLE).match(text)) is expected


@pytest.mark.parametrize("text", ["yes", "Haa", "ok", "હા", " y. "])
def test_yes(text):
    assert guards.YES.match(text)


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (999, "999"), (1000, "1,000"), (112412, "1,12,412"), (10000000, "1,00,00,000"),
    (-150, "-150"), (Decimal("2.5"), "3"), (Decimal("3.5"), "4"),
])
def test_inr(value, expected):
    assert inr(value) == expected


def test_round_half_up_not_bankers():
    assert round_rupees(Decimal("0.5")) == 1 and round_rupees(Decimal("2.5")) == 3


def _shares(*pairs):
    total = sum(Decimal(p) for _, p in pairs)
    fair = total / len(pairs)
    return [Share(i, n, Decimal(p), Decimal(p) - fair) for i, (n, p) in enumerate(pairs)]


def test_plan_one_payer():
    plan = settlement_plan(_shares(("Sagar", 300), ("Biren", 0), ("Lalo", 0)))
    assert [(t.from_name, t.to_name, t.amount) for t in plan] == [
        ("Biren", "Sagar", 100), ("Lalo", "Sagar", 100)]


def test_plan_thirds_do_not_make_phantom_payments():
    plan = settlement_plan(_shares(("Sagar", 100), ("Biren", 0), ("Lalo", 0)))
    assert [t.amount for t in plan] == [33, 33]


def test_plan_everyone_equal():
    assert settlement_plan(_shares(("Sagar", 50), ("Biren", 50), ("Lalo", 50))) == []


def test_plan_two_creditors():
    plan = settlement_plan(_shares(("Sagar", 600), ("Biren", 300), ("Lalo", 0)))
    assert [(t.from_name, t.to_name, t.amount) for t in plan] == [("Lalo", "Sagar", 300)]


def test_paise_differences_make_no_zero_rupee_payments():
    plan = settlement_plan(_shares(("Sagar", "100.30"), ("Biren", "100"), ("Lalo", "100")))
    assert plan == []
