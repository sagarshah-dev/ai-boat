"""Propose, then confirm: a change that is shown first and applied only after "yes".

Used where a mistake is expensive or the input can be misheard: photos of a khata, voice notes,
meetings (three calendars), farm notes. The proposal is stored in the database (pending.CONFIRM),
so a restart between the preview and the "yes" loses nothing. "yes" and "no" are matched by
regex, never by the model, so they can only ever mean one thing.

Each kind registers how to preview it, which buttons to show, and how to apply it. Apply runs
the checks again: between the preview and the "yes" the data may have changed.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.formatting import fmt_date, inr
from app.core.replies import Buttons, Out, Reply, yes_no
from app.db.models import Member
from app.services import expenses, members, pending

if TYPE_CHECKING:
    from app.ai.agent import Context

FOOTER = "Tap ✅ Save or ❌ Cancel (or reply yes / no)."
DROPPED = "❌ Dropped — nothing was changed."
TIMED_OUT = "⌛ That confirmation timed out, so nothing was saved. Please send it again."


@dataclass(frozen=True)
class Kind:
    preview: Callable[[dict], str]
    apply: Callable[["Context", dict, int | None], list[Out]]
    buttons: Callable[[dict], Buttons] = lambda p: yes_no()
    # A reply that is neither yes nor no (e.g. "50 ml, savare" adding details to a farm note).
    # Returns replies to stop routing, or None to treat the text as a new message.
    on_other_text: Callable[["Context", dict, str], list[Out] | None] | None = None


REGISTRY: dict[str, Kind] = {}


def register(name: str, kind: Kind) -> None:
    REGISTRY[name] = kind


def preview(p: dict) -> str:
    return REGISTRY[p["kind"]].preview(p)


def buttons(p: dict) -> Buttons:
    return REGISTRY[p["kind"]].buttons(p)


def store(session: Session, chat_id: int, member: Member, p: dict) -> None:
    """Newest proposal wins: only one thing may own this person's "yes" at a time."""
    pending.put(session, chat_id, member.id, pending.CONFIRM, p)
    pending.drop(session, chat_id, member.id, pending.AMOUNT)
    pending.drop(session, chat_id, member.id, pending.TIME)


def apply(ctx: "Context", p: dict, pick: int | None = None) -> list[Out]:
    return REGISTRY[p["kind"]].apply(ctx, p, pick)


# ── Expenses (photo, voice) ─────────────────────────────────────────────────────────────────

def expense_item(*, amount: Decimal, description: str, paid_by: Member, category: str,
                 merchant: str, on: date, missing: list[str] | None = None) -> dict:
    """One expense inside a proposal, stored as plain JSON."""
    return {"amount": str(amount), "description": description, "paid_by_id": paid_by.id,
            "paid_by": paid_by.name, "category": expenses.normalize_category(category),
            "merchant": merchant or "", "date": on.isoformat(), "missing": missing or []}


def expenses_proposal(items: list[dict], source: str) -> dict:
    return {"kind": "expenses", "items": items, "source": source}


def _item_line(i: int, it: dict) -> str:
    warn = f" ⚠️ {', '.join(it['missing'])} unclear" if it.get("missing") else ""
    return (f"{i}. ₹{inr(Decimal(it['amount']))} — {it['description']} — {it['paid_by']} "
            f"[{it['category']}] — {fmt_date(date.fromisoformat(it['date']))}{warn}")


def _expenses_preview(p: dict) -> str:
    n = len(p["items"])
    head = {"photo": f"📸 I read {n} expense{'s' if n > 1 else ''}:",
            "voice": f"🎤 Log {'these' if n > 1 else 'this'} expense{'s' if n > 1 else ''}?"
            }.get(p.get("source"), f"Log {n} expense{'s' if n > 1 else ''}?")
    lines = "\n".join(_item_line(i, it) for i, it in enumerate(p["items"], start=1))
    return f"{head}\n\n{lines}\n\n{FOOTER}"


def _expenses_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    people = {m.id: m for m in members.split_members(ctx.session)}
    # Checked for every item before anything is written: all or nothing.
    gone = next((it["paid_by"] for it in p["items"] if it["paid_by_id"] not in people), None)
    if gone:
        return [Reply(f"❌ Nothing saved: {gone} no longer shares expenses.")]
    saved = [expenses.add(
        ctx.session, amount=Decimal(it["amount"]), description=it["description"],
        paid_by=people[it["paid_by_id"]], entered_by=ctx.sender,
        expense_date=date.fromisoformat(it["date"]), category=it["category"],
        merchant=it["merchant"]) for it in p["items"]]
    lines = "\n".join(f"#{v.id} — ₹{inr(v.amount)} {v.description} ({v.paid_by}) — {fmt_date(v.date)}"
                      for v in saved)
    return [Reply(f"✅ Logged {len(saved)} entr{'y' if len(saved) == 1 else 'ies'}:\n{lines}")]


def undo_proposal(view: expenses.ExpenseView) -> dict:
    return {"kind": "undo", "id": view.id, "amount": str(view.amount),
            "description": view.description, "date": view.date.isoformat()}


def _undo_preview(p: dict) -> str:
    return (f"🎤 Undo this entry?\n#{p['id']} — ₹{inr(Decimal(p['amount']))} — {p['description']} "
            f"({fmt_date(date.fromisoformat(p['date']))})\n\nTap ✅ Delete or ❌ Cancel (or reply yes / no).")


def _undo_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    from app.ai.agent import undo_text   # import here: agent imports this module
    undone = expenses.undo_by_id(ctx.session, ctx.sender, p["id"])
    if undone is None:
        return [Reply(f"Nothing to undo — #{p['id']} was already removed or settled.")]
    return [Reply(undo_text(undone))]


register("expenses", Kind(preview=_expenses_preview, apply=_expenses_apply))
register("undo", Kind(preview=_undo_preview, apply=_undo_apply,
                      buttons=lambda p: yes_no("✅ Delete")))
