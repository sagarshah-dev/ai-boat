"""Settle-up: record who pays whom, and mark exactly the expenses that were counted as settled.

One transaction does all of it. In the sheet this was three separate writes (log transfers, mark
rows, add a marker row) — a failure halfway left balances that matched nothing.
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.core.formatting import inr
from app.db.models import Expense, Member, Settlement, SettlementTransfer
from app.services import balance as balance_service
from app.services.balance import Balance

# Any fixed number; two settles running at once both ask for this lock and the second waits
# until the first has committed, then finds nothing left to settle.
_SETTLE_LOCK_KEY = 4_200_001


@dataclass(frozen=True)
class SettleResult:
    settlement_id: int | None   # None when there was nothing to settle
    balance: Balance            # the numbers as they were just BEFORE settling

    @property
    def text(self) -> str:
        if self.settlement_id is None:
            return "✅ Nothing to settle — there are no unsettled expenses."
        plan = ("\n".join(f"{t.from_name} pays {t.to_name} ₹{inr(t.amount)}"
                          for t in self.balance.plan)
                or "Everyone was already settled!")
        return f"✅ Settled! (#{self.settlement_id})\n{plan}\n\nBalances reset to ₹0."


def settle(session: Session, by: Member, on: date) -> SettleResult:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SETTLE_LOCK_KEY})
    snapshot = balance_service.compute(session, lock=True)
    if not snapshot.expense_ids:
        return SettleResult(None, snapshot)

    settlement = Settlement(settled_on=on, created_by=by.id)
    session.add(settlement)
    session.flush()
    for t in snapshot.plan:
        session.add(SettlementTransfer(settlement_id=settlement.id, from_member=t.from_id,
                                       to_member=t.to_id, amount=t.amount))
    # Only the rows the plan was computed from — an expense added a second later stays open.
    session.execute(update(Expense).where(Expense.id.in_(snapshot.expense_ids))
                    .values(settlement_id=settlement.id))
    session.flush()
    return SettleResult(settlement.id, snapshot)
