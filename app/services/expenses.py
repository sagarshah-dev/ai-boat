"""Expense records: add, list, search, undo. Plain values in, plain values out — no Telegram,
no AI. Every rule about WHAT may be saved (grounding, payer, item word) lives in app/ai/guards.py
and is checked before these functions are called."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.formatting import fmt_date, inr
from app.db.models import Expense, Member

CATEGORIES = ("Plant", "Fuel", "House", "Utilities", "Food", "Labor", "Misc")


@dataclass(frozen=True)
class ExpenseView:
    id: int
    date: date
    amount: Decimal
    description: str
    paid_by: str
    category: str
    merchant: str
    entered_by: str
    settled: bool

    def line(self) -> str:
        """The one-line form used in every list the bot sends."""
        return (f"• #{self.id} — ₹{inr(self.amount)} — {self.description} — "
                f"{self.paid_by} ({fmt_date(self.date)})")


def normalize_category(raw: object) -> str:
    """Anything outside the fixed list becomes Misc, so reports never get a stray category."""
    wanted = str(raw or "").strip().lower()
    return next((c for c in CATEGORIES if c.lower() == wanted), "Misc")


def add(session: Session, *, amount: Decimal, description: str, paid_by: Member,
        entered_by: Member, expense_date: date, category: str = "Misc",
        merchant: str = "") -> ExpenseView:
    row = Expense(expense_date=expense_date, amount=amount, description=description.strip(),
                  paid_by=paid_by.id, entered_by=entered_by.id,
                  category=normalize_category(category), merchant=(merchant or "").strip())
    session.add(row)
    session.flush()  # the database assigns the ID here — no max(ID)+1 race like the sheet had
    return _view(row, paid_by.name, entered_by.name)


def _query() -> Select:
    payer, enterer = aliased(Member), aliased(Member)
    return (select(Expense, payer.name, enterer.name)
            .join(payer, Expense.paid_by == payer.id)
            .join(enterer, Expense.entered_by == enterer.id)
            .where(Expense.deleted_at.is_(None)))


def _view(e: Expense, payer: str, enterer: str) -> ExpenseView:
    return ExpenseView(id=e.id, date=e.expense_date, amount=e.amount, description=e.description,
                       paid_by=payer, category=e.category, merchant=e.merchant,
                       entered_by=enterer, settled=e.settlement_id is not None)


def recent(session: Session, n: int = 5) -> list[ExpenseView]:
    """Newest first, settled or not — 'recent' is for checking what was just typed."""
    rows = session.execute(_query().order_by(Expense.id.desc()).limit(n)).all()
    return [_view(*r) for r in rows]


def search(session: Session, *, paid_by: Member | None = None, category: str | None = None,
           amount_above: Decimal | None = None, amount_below: Decimal | None = None,
           text: str | None = None, date_from: date | None = None,
           date_to: date | None = None) -> list[ExpenseView]:
    """All filters optional and combined with AND. Runs over ALL rows in the database, newest
    first, so an old date range is found however far back it is."""
    q = _query()
    if paid_by is not None:
        q = q.where(Expense.paid_by == paid_by.id)
    if category:
        q = q.where(Expense.category == normalize_category(category))
    if amount_above is not None:
        q = q.where(Expense.amount > amount_above)
    if amount_below is not None:
        q = q.where(Expense.amount < amount_below)
    if date_from is not None:
        q = q.where(Expense.expense_date >= date_from)
    if date_to is not None:
        q = q.where(Expense.expense_date <= date_to)
    for word in (text or "").lower().split():
        # Every word must appear, in the description or the merchant. Wildcards typed by the
        # user are escaped so "50%" searches for "50%", not "50 followed by anything".
        pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        q = q.where(or_(Expense.description.ilike(pattern, escape="\\"),
                        Expense.merchant.ilike(pattern, escape="\\")))
    return [_view(*r) for r in session.execute(q.order_by(Expense.id.desc())).all()]


def undo_last(session: Session, member: Member) -> ExpenseView | None:
    """Soft-deletes the newest unsettled expense this member ENTERED. Only their own: one
    person's 'undo' can never remove someone else's entry. Settled history never moves."""
    row = session.execute(
        _query().where(Expense.entered_by == member.id, Expense.settlement_id.is_(None))
        .order_by(Expense.id.desc()).limit(1).with_for_update(of=Expense)).first()
    if row is None:
        return None
    expense = row[0]
    expense.deleted_at = datetime.now(timezone.utc)
    session.flush()
    return _view(*row)


def find_own_open(session: Session, member: Member, expense_id: int) -> ExpenseView | None:
    """This member's entry, still unsettled and not undone — the only kind undo may touch."""
    row = session.execute(_query().where(
        Expense.id == expense_id, Expense.entered_by == member.id,
        Expense.settlement_id.is_(None)).with_for_update(of=Expense)).first()
    return _view(*row) if row else None


def newest_own_open(session: Session, member: Member) -> ExpenseView | None:
    row = session.execute(_query().where(
        Expense.entered_by == member.id, Expense.settlement_id.is_(None))
        .order_by(Expense.id.desc()).limit(1)).first()
    return _view(*row) if row else None


def undo_by_id(session: Session, member: Member, expense_id: int) -> ExpenseView | None:
    """Undo of one specific entry, after a confirmed preview. Re-checked here: between the preview
    and the "yes" the entry may have been settled or undone already."""
    view = find_own_open(session, member, expense_id)
    if view is None:
        return None
    session.get(Expense, expense_id).deleted_at = datetime.now(timezone.utc)
    session.flush()
    return view


def edit_own_open(session: Session, member: Member, expense_id: int, *, amount: Decimal,
                  description: str) -> ExpenseView | None:
    """Change amount / item of one's own unsettled entry, after a confirmed preview. Re-checked
    here like undo_by_id: it may have been settled or undone since the preview."""
    if find_own_open(session, member, expense_id) is None:
        return None
    e = session.get(Expense, expense_id)
    e.amount, e.description = amount, description
    session.flush()
    return find_own_open(session, member, expense_id)


def by_ids(session: Session, ids: list[int]) -> list[ExpenseView]:
    if not ids:
        return []
    rows = session.execute(_query().where(Expense.id.in_(ids)).order_by(Expense.id)).all()
    return [_view(*r) for r in rows]


def total(views: list[ExpenseView]) -> Decimal:
    return sum((v.amount for v in views), Decimal("0"))
