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
    pending.drop(session, chat_id, member.id, pending.PLANT)


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
            "voice": f"🎤 Log {'these' if n > 1 else 'this'} expense{'s' if n > 1 else ''}?",
            "check": "❓ Is this right? (the amount is the number you wrote)",
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
    return (f"↩️ Delete this entry?\n#{p['id']} — ₹{inr(Decimal(p['amount']))} — {p['description']} "
            f"({fmt_date(date.fromisoformat(p['date']))})\n\nTap ✅ Delete or ❌ Cancel (or reply yes / no).")


def _undo_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    from app.ai.agent import undo_text   # import here: agent imports this module
    undone = expenses.undo_by_id(ctx.session, ctx.sender, p["id"])
    if undone is None:
        return [Reply(f"Nothing to undo — #{p['id']} was already removed or settled.")]
    return [Reply(undo_text(undone))]


# ── One amount, two items: "150 tea, 250 vadapav, 500 sevsad, coffee" ──────────────────────
# Is ₹500 sevsad AND coffee, or was coffee's amount forgotten? Both are real. Money is never
# guessed, so the user taps: 1 = together, 2 = only the first item; the bot then asks coffee's.

def joined_proposal(item: dict, own: list[str], extra: list[str]) -> dict:
    return {"kind": "joined", "item": item, "own": own, "extra": extra,
            "choices": [{"line": "both"}, {"line": "separate"}]}


def _joined_preview(p: dict) -> str:
    it, own, extra = p["item"], ", ".join(p["own"]), ", ".join(p["extra"])
    amt = inr(Decimal(it["amount"]))
    return (f"❓ Is ₹{amt} for both {own} and {extra}?\n"
            f"1. Yes, together: ₹{amt} — {it['description']} — {it['paid_by']}\n"
            f"2. No, ₹{amt} is only {own} — then I ask the amount for {extra}\n\n"
            "Tap 1 or 2, or ❌ Cancel.")


def _joined_buttons(p: dict) -> Buttons:
    return [[("1 Together", "1"), (f"2 {', '.join(p['extra'])} separate"[:30], "2")],
            [("❌ Cancel", "no")]]


def _joined_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    it = dict(p["item"])
    if pick == 1:
        it["description"] = " ".join(p["own"])
    out = _expenses_apply(ctx, expenses_proposal([it], "text"), None)
    if pick == 1:
        payer = it["paid_by"].split()[0]
        extra = " ".join(p["extra"])
        # Same "how much?" memory as "tea by sagar": the next "300" completes it.
        pending.put(ctx.session, ctx.chat_id, ctx.sender.id, pending.AMOUNT,
                    {"message": f"{extra} by {payer}", "tries_left": 3})
        out.append(Reply(f"📝 \"{extra}\" — how much was it?\nSend just the amount, e.g. 100 "
                         "(within 2 minutes)."))
    return out


# ── Change your own last expense: "Na, 150 hata" ────────────────────────────────────────────
# Live 13-11-2026: "Na, 150 hata" after a ₹100 tea undid the entry with no question. A
# correction is an edit, shown before it is made.

def edit_proposal(view: expenses.ExpenseView, amount: Decimal | None, description: str) -> dict:
    return {"kind": "edit", "id": view.id, "old_amount": str(view.amount),
            "old_description": view.description,
            "amount": str(amount if amount is not None else view.amount),
            "description": description or view.description}


def _edit_preview(p: dict) -> str:
    old = f"₹{inr(Decimal(p['old_amount']))} — {p['old_description']}"
    new = f"₹{inr(Decimal(p['amount']))} — {p['description']}"
    return (f"✏️ Change #{p['id']}?\n{old}  →  {new}\n\n"
            "Tap ✅ Save or ❌ Cancel (or reply yes / no).")


def _edit_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    v = expenses.edit_own_open(ctx.session, ctx.sender, p["id"], amount=Decimal(p["amount"]),
                               description=p["description"])
    if v is None:
        return [Reply(f"Nothing changed — #{p['id']} was removed or settled meanwhile.")]
    return [Reply(f"✅ Changed #{v.id}: ₹{inr(v.amount)} for {v.description}, paid by {v.paid_by} "
                  f"— {fmt_date(v.date)}")]


register("expenses", Kind(preview=_expenses_preview, apply=_expenses_apply))
register("edit", Kind(preview=_edit_preview, apply=_edit_apply))
register("joined", Kind(preview=_joined_preview, apply=_joined_apply, buttons=_joined_buttons))
register("undo", Kind(preview=_undo_preview, apply=_undo_apply,
                      buttons=lambda p: yes_no("✅ Delete")))
