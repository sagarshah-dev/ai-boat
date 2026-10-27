"""The ONLY place balances and the settle-up plan are calculated and worded.

Every entry point — the 'balance' command, an AI question like 'hisab aapo', settle, and later
the PDF — calls compute(), so the same question always gets the same numbers.
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.formatting import inr, round_rupees
from app.db.models import Expense
from app.services import members

ZERO = Decimal("0")
TOLERANCE = Decimal("0.005")


@dataclass(frozen=True)
class Share:
    member_id: int
    name: str
    paid: Decimal
    net: Decimal  # > 0 gets back, < 0 pending — unrounded


@dataclass(frozen=True)
class Transfer:
    from_id: int
    from_name: str
    to_id: int
    to_name: str
    amount: int  # whole rupees, what people actually hand over


@dataclass(frozen=True)
class Balance:
    total: Decimal
    fair_share: Decimal
    shares: list[Share]
    plan: list[Transfer]
    expense_ids: list[int]  # exactly the rows these numbers came from — settle marks only these

    @property
    def text(self) -> str:
        return format_text(self)


def compute(session: Session, *, lock: bool = False) -> Balance:
    """Snapshot of all unsettled, undeleted expenses.

    lock=True (settle) locks those rows until the transaction ends, so an undo running at the
    same moment cannot change a row that is being settled."""
    people = members.split_members(session)
    q = select(Expense.id, Expense.paid_by, Expense.amount).where(
        Expense.settlement_id.is_(None), Expense.deleted_at.is_(None))
    if lock:
        q = q.with_for_update()
    rows = session.execute(q.order_by(Expense.id)).all()

    total = sum((r.amount for r in rows), ZERO)
    fair = total / len(people) if people else ZERO
    paid_by: dict[int, Decimal] = {}
    for r in rows:
        paid_by[r.paid_by] = paid_by.get(r.paid_by, ZERO) + r.amount

    shares = [Share(m.id, m.name, paid_by.get(m.id, ZERO), paid_by.get(m.id, ZERO) - fair)
              for m in people]
    return Balance(total=total, fair_share=fair, shares=shares, plan=settlement_plan(shares),
                   expense_ids=[r.id for r in rows])


def settlement_plan(shares: list[Share]) -> list[Transfer]:
    """Greedy matching of pending -> gets-back on UNROUNDED values; each payment is rounded only
    at the end. Rounding first is what gave the old bot a ₹1 mismatch."""
    debtors = [[s, -s.net] for s in shares if s.net < 0]
    creditors = [[s, s.net] for s in shares if s.net > 0]
    plan: list[Transfer] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        if round_rupees(pay) > 0:
            plan.append(Transfer(debtors[i][0].member_id, debtors[i][0].name,
                                 creditors[j][0].member_id, creditors[j][0].name, round_rupees(pay)))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        # A leftover of a fraction of a paisa must not create a phantom ₹0 payment.
        if debtors[i][1] <= TOLERANCE:
            i += 1
        if creditors[j][1] <= TOLERANCE:
            j += 1
    return plan


def format_text(b: Balance) -> str:
    """The one balance message, same wording as the old bot ('pending' / 'gets back', never 'owe')."""
    lines = ["💰 Balance Status", "",
             f"Total Expenses: ₹{inr(b.total)}",
             f"Fair Share (each): ₹{inr(b.fair_share)}", ""]
    for s in b.shares:
        net = round_rupees(s.net)
        status = (f"gets back ₹{inr(net)}" if net > 0
                  else f"pending ₹{inr(-net)}" if net < 0 else "settled")
        lines.append(f"{s.name} paid ₹{inr(s.paid)} → {status}")
    lines.append("")
    if b.plan:
        lines.append("🔄 Settlement Suggestion:")
        lines += [f"{t.from_name} pays {t.to_name} ₹{inr(t.amount)}" for t in b.plan]
    else:
        lines.append("✅ Everyone is settled!")
    return "\n".join(lines)

