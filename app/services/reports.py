"""Expense reports: the scope grammar ("report fuel", "report last week", "report above 1000 by
sagar", "report 01 aug to till date") and the PDF.

Ported from the old bot's report.js. The grammar is plain code, no AI: it is also the fallback
for "search ..." when the AI is down, so it must never depend on the model.
"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from fpdf import FPDF
from sqlalchemy.orm import Session

from app.core.formatting import fmt_date, inr, round_rupees
from app.db.models import Member
from app.services import balance as balance_service
from app.services import expenses, members
from app.services.expenses import CATEGORIES, ExpenseView

HIGHLIGHT_ABOVE = Decimal("5000")

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MONTHS["sept"] = 9


@dataclass(frozen=True)
class Scope:
    kind: str                    # since-settle, all, owner, category, date-range, amount-above, ...
    label: str
    unsettled_only: bool = False
    paid_by: Member | None = None
    category: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    amount_above: Decimal | None = None
    amount_below: Decimal | None = None
    text: str | None = None

    def rows(self, session: Session) -> list[ExpenseView]:
        rows = expenses.search(session, paid_by=self.paid_by, category=self.category,
                               amount_above=self.amount_above, amount_below=self.amount_below,
                               text=self.text, date_from=self.date_from, date_to=self.date_to)
        if self.unsettled_only:
            rows = [r for r in rows if not r.settled]
        return sorted(rows, key=lambda r: r.id)


def parse_day(text: str, today: date) -> date | None:
    """'today', '01-08-2026', '1/8', '1 aug', 'aug 1', '1 august 2026' -> a date. None if unclear."""
    t = text.strip().lower().rstrip(".")
    if re.fullmatch(r"today|now|till\s*date|till\s*today|aaj", t):
        return today
    if t in ("yesterday", "kale"):
        return today - timedelta(days=1)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return _safe_date(_year(y, today), mo, d)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?(?:,?\s+(\d{2,4}))?", t)
    if m and m.group(2) in _MONTHS:
        return _safe_date(_year(m.group(3), today), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{2,4}))?", t)
    if m and m.group(1) in _MONTHS:
        return _safe_date(_year(m.group(3), today), _MONTHS[m.group(1)], int(m.group(2)))
    return None


def _year(raw: str | None, today: date) -> int:
    if not raw:
        return today.year
    y = int(raw)
    return y + 2000 if y < 100 else y


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_scope(session: Session, text: str, today: date) -> Scope:
    """Never fails: anything not recognised becomes a word search over description + shop,
    exactly like the old bot."""
    rest = re.sub(r"^(report|email|search|find)\b", "", text.strip(), flags=re.IGNORECASE).strip()
    rest = re.sub(r"^expenses?\b", "", rest, flags=re.IGNORECASE).strip()
    people = members.split_members(session)

    # "<scope> by <name>" combines any scope with a payer.
    by = re.fullmatch(r"(.+)\s+by\s+(\S+)", rest, flags=re.IGNORECASE)
    if by:
        payer = members.match_name(people, by.group(2))
        if payer:
            base = parse_scope(session, by.group(1), today)
            if base.kind != "text-search":
                return Scope(**{**base.__dict__, "kind": f"{base.kind}+payer",
                                "label": f"{base.label} — Paid By {payer.name}", "paid_by": payer})

    lower = rest.lower()
    if not rest:
        return Scope("since-settle", "Since Last Settlement", unsettled_only=True)
    if lower == "all":
        return Scope("all", "All Entries")
    payer = members.match_name(people, lower)
    if payer:
        return Scope("owner", f"Paid By {payer.name}", paid_by=payer)
    cat = next((c for c in CATEGORIES if c.lower() == lower), None)
    if cat:
        return Scope("category", f"Category: {cat}", category=cat)
    if lower == "today":
        return _range(today, today, "Today")
    if lower == "yesterday":
        y = today - timedelta(days=1)
        return _range(y, y, "Yesterday")
    monday = today - timedelta(days=today.weekday())
    if re.fullmatch(r"this\s+week", lower):
        return _range(monday, today, "This Week")
    if re.fullmatch(r"last\s+week", lower):
        return _range(monday - timedelta(days=7), monday - timedelta(days=1), "Last Week")
    first = today.replace(day=1)
    if re.fullmatch(r"this\s+month", lower):
        return _range(first, today, "This Month")
    if re.fullmatch(r"last\s+month", lower):
        end = first - timedelta(days=1)
        return _range(end.replace(day=1), end, "Last Month")
    m = re.fullmatch(r"(above|over|more than|greater than)\s+(\d+(?:\.\d+)?)", lower)
    if m:
        return Scope("amount-above", f"Amount Above Rs. {m.group(2)}", amount_above=Decimal(m.group(2)))
    m = re.fullmatch(r"(below|under|less than)\s+(\d+(?:\.\d+)?)", lower)
    if m:
        return Scope("amount-below", f"Amount Below Rs. {m.group(2)}", amount_below=Decimal(m.group(2)))
    parts = re.split(r"\s+to\s+", rest, flags=re.IGNORECASE)
    if len(parts) == 2:
        start, end = parse_day(parts[0], today), parse_day(parts[1], today)
        if start and end:
            start, end = min(start, end), max(start, end)
            return _range(start, end, f"{fmt_date(start)} to {fmt_date(end)}")
    return Scope("text-search", f'Text Search: "{rest}"', text=rest)


def _range(start: date, end: date, label: str) -> Scope:
    return Scope("date-range", f"Date Range: {label}", date_from=start, date_to=end)


# ── Reading a report in chat ────────────────────────────────────────────────────────────────

def search_text(rows: list[ExpenseView], label: str) -> str:
    if not rows:
        return f"No entries found for {label}."
    total = expenses.total(rows)
    newest = sorted(rows, key=lambda r: r.id, reverse=True)
    shown = newest[:20]
    more = " (newest 20 shown)" if len(rows) > 20 else ""
    return (f"📋 {label}{more}:\n\n" + "\n".join(r.line() for r in shown)
            + f"\n\n💵 Total: ₹{inr(total)} ({len(rows)} entries)")


# ── PDF ─────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Report:
    filename: str
    data: bytes
    count: int
    total: Decimal
    label: str
    rows: list[ExpenseView] = field(default_factory=list)


GREEN = (45, 106, 79)
DIVIDER = (233, 237, 233)
ZEBRA = (244, 246, 245)
BLUE = (26, 86, 219)


def _latin(text: str) -> str:
    """The built-in PDF fonts are Latin-1 only. Gujarati or emoji in a description must not
    crash the report; it shows as '?' in the PDF (the chat and the database keep the original)."""
    return (text or "").replace("₹", "Rs. ").encode("latin-1", "replace").decode("latin-1")


def build_pdf(rows: list[ExpenseView], label: str, *, generated: datetime,
              people: list[Member], balance: balance_service.Balance | None = None) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, _latin("AI BOaT - Expense Report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 5, _latin(f"Scope: {label}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Generated: {generated.strftime('%d-%m-%Y %H:%M')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    cols = [("Description", 80, "L"), ("Paid By", 35, "L"), ("Category", 35, "L"), ("Amount", 40, "R")]

    def header():
        pdf.set_fill_color(*GREEN)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        for name, w, align in cols:
            pdf.cell(w, 8, name, fill=True, align=align)
        pdf.ln()

    def ensure_space(h: float, divider: str | None = None) -> None:
        if pdf.get_y() + h > 280:
            pdf.add_page()
            header()
            if divider:
                day_bar(f"{divider} (cont'd)")

    def day_bar(text: str) -> None:
        pdf.set_fill_color(*DIVIDER)
        pdf.set_text_color(*GREEN)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(sum(w for _, w, _ in cols), 6, _latin(text), fill=True, new_x="LMARGIN", new_y="NEXT")

    header()
    current, zebra = None, 0
    # Grouped by day, oldest first; the date is shown once per group, not on every row.
    for r in sorted(rows, key=lambda r: (r.date, r.id)):
        day = r.date.strftime("%d %b %Y")
        if day != current:
            ensure_space(12)
            day_bar(day)
            current, zebra = day, 0
        ensure_space(7, current)
        fill = zebra % 2 == 1
        pdf.set_fill_color(*ZEBRA)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(80, 7, _latin(r.description)[:48], fill=fill)
        pdf.cell(35, 7, _latin(r.paid_by)[:20], fill=fill)
        pdf.cell(35, 7, _latin(r.category), fill=fill)
        if r.amount > HIGHLIGHT_ABOVE:
            pdf.set_text_color(*BLUE)
        pdf.cell(40, 7, f"Rs. {inr(r.amount)}", fill=fill, align="R")
        pdf.ln()
        zebra += 1

    total = expenses.total(rows)
    ensure_space(30)
    pdf.ln(3)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, f"Total: Rs. {inr(total)}  ({len(rows)} entries)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 5, "Blue amount = entry over Rs. 5,000", new_x="LMARGIN", new_y="NEXT")

    # Per-person subtotal of what THIS report shows.
    pdf.ln(3)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Total by person:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for m in people:
        paid = sum((r.amount for r in rows if r.paid_by == m.name), Decimal("0"))
        pdf.cell(0, 5, _latin(f"{m.name}: Rs. {inr(paid)}"), new_x="LMARGIN", new_y="NEXT")

    # Balance + plan ONLY for "since last settlement": other scopes can include settled entries,
    # and a plan computed from them would ask people to pay money that already changed hands.
    if balance is not None:
        ensure_space(40)
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Balance Summary:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"Fair Share (each): Rs. {inr(balance.fair_share)}", new_x="LMARGIN", new_y="NEXT")
        for s in balance.shares:
            net = round_rupees(s.net)
            line = (f"{s.name}: gets back Rs. {inr(net)}" if net > 0
                    else f"{s.name}: pending Rs. {inr(-net)}" if net < 0 else f"{s.name}: settled")
            pdf.cell(0, 5, _latin(line), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Settlement Suggestion:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for t in balance.plan or []:
            pdf.cell(0, 5, _latin(f"{t.from_name} pays {t.to_name}: Rs. {inr(t.amount)}"),
                     new_x="LMARGIN", new_y="NEXT")
        if not balance.plan:
            pdf.cell(0, 5, "Everyone is settled!", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def build_report(session: Session, scope: Scope, *, today: date, now: datetime,
                 balance: balance_service.Balance | None = None,
                 rows: list[ExpenseView] | None = None) -> Report:
    rows = scope.rows(session) if rows is None else rows
    if balance is None and scope.kind == "since-settle":
        balance = balance_service.compute(session)
    data = build_pdf(rows, scope.label, generated=now, people=members.split_members(session),
                     balance=balance if scope.kind == "since-settle" else None)
    return Report(filename=f"report-{scope.kind}-{fmt_date(today)}.pdf", data=data,
                  count=len(rows), total=expenses.total(rows), label=scope.label, rows=rows)
