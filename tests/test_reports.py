"""Feature 3: report scopes, PDFs, email, the settle snapshot and the daily job."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import PendingAction, ProcessedUpdate
from app.db.session import session_scope
from app.integrations.gemini import LlmError
from app.jobs import routes as jobs
from app.main import app
from app.services import reports
from tests.bot_helpers import SAGAR, add_rows
from tests.conftest import ADMIN_ID, call

WED = date(2026, 10, 28)   # a Wednesday


def scope(text, today=WED):
    with session_scope() as s:
        return reports.parse_scope(s, text, today)


def use_settings(**changes):
    settings = get_settings().model_copy(update=changes)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


MAIL = dict(smtp_host="smtp.example.com", smtp_user="u", smtp_password="p",
            mail_from="bot@example.com", recipient_emails="a@x.com, b@x.com")


# ── Scope grammar ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kind,dates", [
    ("report", "since-settle", None),
    ("report all", "all", None),
    ("report today", "date-range", (WED, WED)),
    ("report yesterday", "date-range", (date(2026, 10, 27),) * 2),
    ("report this week", "date-range", (date(2026, 10, 26), WED)),
    ("report last week", "date-range", (date(2026, 10, 19), date(2026, 10, 25))),
    ("report this month", "date-range", (date(2026, 10, 1), WED)),
    ("report last month", "date-range", (date(2026, 9, 1), date(2026, 9, 30))),
    ("report 01 aug to till date", "date-range", (date(2026, 8, 1), WED)),
    ("report aug 1 to 15 aug", "date-range", (date(2026, 8, 1), date(2026, 8, 15))),
    ("report 15/8 to 1/8", "date-range", (date(2026, 8, 1), date(2026, 8, 15))),   # swapped
    ("report 1st sept 2025 to today", "date-range", (date(2025, 9, 1), WED)),
])
def test_scope_dates(bot, text, kind, dates):
    sc = scope(text)
    assert sc.kind == kind
    if dates:
        assert (sc.date_from, sc.date_to) == dates


def test_last_month_in_january_is_december():
    s = scope("report last month", today=date(2027, 1, 10))
    assert (s.date_from, s.date_to) == (date(2026, 12, 1), date(2026, 12, 31))


@pytest.mark.parametrize("text,kind", [
    ("report sagar", "owner"), ("report fuel", "category"), ("report above 1000", "amount-above"),
    ("report below 200", "amount-below"), ("report fuel by biren", "category+payer"),
    ("report above 1000 by lalo", "amount-above+payer"), ("report arjun construction", "text-search"),
    ("report 32 aug to today", "text-search"),          # impossible date: searched, not "everything"
    ("report diesel by ramesh", "text-search"),          # a guest is not a payer
    ("search expense fuel", "category"), ("find sagar", "owner"),
])
def test_scope_kinds(bot, text, kind):
    assert scope(text).kind == kind


# ── report command ─────────────────────────────────────────────────────────────────────────

def test_report_sends_a_pdf_of_unsettled_entries(bot):
    add_rows(("Sagar", 300, "seed"), ("Biren", 6000, "pump"))
    assert bot.send(SAGAR, "report") == ""
    [doc] = bot.tg.documents
    assert doc["data"].startswith(b"%PDF") and doc["filename"].startswith("report-since-settle-")
    assert "2 entries, ₹6,300 total" in doc["caption"]


def test_report_excludes_settled_but_report_all_includes_them(bot):
    add_rows(("Sagar", 300, "old"))
    bot.send(SAGAR, "settle 7777")
    add_rows(("Biren", 50, "new"))
    bot.send(SAGAR, "report")
    assert "1 entries" in bot.tg.documents[-1]["caption"]
    bot.send(SAGAR, "report all")
    assert "2 entries" in bot.tg.documents[-1]["caption"]


def test_report_with_nothing_found(bot):
    assert "No entries found for Category: Fuel" in bot.send(SAGAR, "report fuel")
    assert bot.tg.documents == []


def test_pdf_survives_gujarati_and_many_pages(bot):
    add_rows(*[("Sagar", 10 + i, "ખાતર અને દવા 🌱") for i in range(120)])
    bot.send(SAGAR, "report all")
    assert bot.tg.documents[-1]["data"].count(b"/Type /Page\n") + \
        bot.tg.documents[-1]["data"].count(b"/Type /Page") >= 2


# ── email command ──────────────────────────────────────────────────────────────────────────

def test_email_not_set_up(bot):
    assert "Email is not set up" in bot.send(SAGAR, "email")
    assert bot.mailer.sent == []


def test_email_sends_after_reply_and_reports_success(bot):
    use_settings(**MAIL)
    add_rows(("Sagar", 300, "seed"))
    reply = bot.send(SAGAR, "email fuel by sagar") or ""
    assert "No entries found" in reply
    add_rows(("Sagar", 900, "diesel", "Fuel"))
    reply = bot.send(SAGAR, "email fuel")
    assert reply.startswith("📧 Emailing Category: Fuel — 1 entries, ₹900")
    assert "emailed to a@x.com, b@x.com" in reply
    [mail] = bot.mailer.sent
    assert mail.to == ("a@x.com", "b@x.com") and mail.attachments[0][2] == "application/pdf"


def test_email_failure_is_said_out_loud_and_the_pdf_really_comes(bot):
    use_settings(**MAIL)
    bot.mailer.ok = False
    bot.mailer.last_error = "login refused — check SMTP_USER and SMTP_PASSWORD"
    add_rows(("Sagar", 300, "seed"))
    reply = bot.send(SAGAR, "email")
    assert "could not be sent. The PDF is in this chat" in reply
    assert "Reason: login refused" in reply
    assert len(bot.tg.documents) == 1                 # the promise in the message is kept


def test_email_success_does_not_also_send_the_pdf(bot):
    use_settings(**MAIL)
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "email")
    assert bot.mailer.sent and not bot.tg.documents


def test_settle_email_failure_sends_the_pdf_once(bot):
    use_settings(**MAIL)
    bot.mailer.ok = False
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "settle")
    reply = bot.send(SAGAR, "7777")
    assert "could not be sent" in reply and len(bot.tg.documents) == 1   # snapshot, not twice


@pytest.mark.parametrize("exc,words", [
    ("auth", "SMTP_PASSWORD"), ("sender", "MAIL_FROM"), ("rcpt", "RECIPIENT_EMAILS"),
    ("timeout", "SMTP_HOST"), ("refused", "SMTP_HOST"),
])
def test_email_failure_reason_names_the_setting_to_fix(exc, words):
    import smtplib
    from app.integrations.email import failure_reason
    err = {"auth": smtplib.SMTPAuthenticationError(535, b"bad"),
           "sender": smtplib.SMTPSenderRefused(553, b"no", "x@y"),
           "rcpt": smtplib.SMTPRecipientsRefused({"a@b": (550, b"no")}),
           "timeout": TimeoutError(), "refused": ConnectionRefusedError()}[exc]
    assert words in failure_reason(err)


def test_mailer_records_why_it_failed(monkeypatch):
    import smtplib
    from app.core.replies import Email
    from app.integrations.email import Mailer, SmtpSettings

    class Broken:
        def __init__(self, *a, **kw):
            raise smtplib.SMTPAuthenticationError(535, b"bad")
    monkeypatch.setattr(smtplib, "SMTP", Broken)
    m = Mailer(SmtpSettings("smtp.gmail.com", 587, "u", "p", "u@x.com"))
    assert m.send(Email("s", "b", ("a@x.com",))) is False
    assert "login refused" in m.last_error


# ── settle snapshot ────────────────────────────────────────────────────────────────────────

def test_settle_sends_snapshot_pdf_of_exactly_what_was_settled(bot):
    use_settings(**MAIL)
    add_rows(("Sagar", 300, "seed"), ("Biren", 60, "tea"))
    bot.send(SAGAR, "settle 7777")
    [doc] = bot.tg.documents
    assert doc["filename"].startswith("settlement-1-")
    assert "2 entries, ₹360" in doc["caption"]
    [mail] = bot.mailer.sent
    assert "Balance Status" in mail.body and "Lalo pays Sagar ₹120" in mail.body


def test_settle_without_email_still_gives_pdf(bot):
    add_rows(("Sagar", 300, "seed"))
    bot.send(SAGAR, "settle 7777")
    assert len(bot.tg.documents) == 1 and bot.mailer.sent == []


# ── search falls back to the grammar when the AI is down ───────────────────────────────────

def test_search_uses_ai_normally(bot):
    add_rows(("Sagar", 300, "diesel"))
    bot.llm.script(call("search_expenses", text="diesel"))
    assert "💵 Total: ₹300" in bot.send(SAGAR, "search diesel")


def test_search_falls_back_when_ai_is_down(bot):
    add_rows(("Sagar", 300, "diesel", "Fuel"), ("Biren", 50, "tea"))
    bot.llm.script(LlmError("quota"))
    reply = bot.send(SAGAR, "search fuel")
    assert reply.startswith("🤖 AI is unavailable — basic search:")
    assert "diesel" in reply and "tea" not in reply


# ── daily job ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def jobs_client(tg, mailer, llm):
    from app.telegram.webhook import get_llm, get_mailer, get_telegram
    app.dependency_overrides[get_telegram] = lambda: tg
    app.dependency_overrides[get_mailer] = lambda: mailer
    app.dependency_overrides[get_llm] = lambda: llm
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def run_daily(client, secret="job-secret-123", force=""):
    return client.post(f"/jobs/daily?force={force}", headers={"X-Jobs-Secret": secret})


def test_jobs_switched_off_without_secret(jobs_client):
    assert run_daily(jobs_client).status_code == 503


def test_jobs_wrong_secret(jobs_client):
    use_settings(jobs_secret="job-secret-123")
    assert run_daily(jobs_client, secret="nope").status_code == 401


def test_daily_cleanup(jobs_client, bot):
    use_settings(jobs_secret="job-secret-123")
    old = datetime.now(timezone.utc) - timedelta(days=8)
    with session_scope() as s:
        s.add(ProcessedUpdate(update_id=1, received_at=old))
        s.add(ProcessedUpdate(update_id=2))
        s.add(PendingAction(chat_id=1, member_id=1, kind="amount",
                            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    r = run_daily(jobs_client)
    assert r.status_code == 200 and "removed 1 old update ids, 1 expired" in r.json()["results"]["cleanup"]
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedUpdate)) >= 1


def test_weekly_report_emailed(jobs_client, bot, mailer):
    use_settings(jobs_secret="job-secret-123", **MAIL)
    add_rows(("Sagar", 300, "seed"), when=date.today() - timedelta(days=date.today().weekday() + 3))
    r = run_daily(jobs_client, force="weekly")
    assert r.json()["results"]["weekly"].startswith("emailed")
    assert mailer.sent[-1].subject.startswith("Weekly Report")


def test_monthly_report_without_email_goes_to_admin_chat(jobs_client, bot, tg):
    use_settings(jobs_secret="job-secret-123")
    first = date.today().replace(day=1)
    add_rows(("Sagar", 300, "seed"), when=first - timedelta(days=3))
    r = run_daily(jobs_client, force="monthly")
    assert r.json()["results"]["monthly"] == "email not set up, sent to Telegram"
    assert tg.documents[-1]["chat_id"] == ADMIN_ID


def test_failed_email_falls_back_to_chat_and_says_so(jobs_client, bot, tg, mailer):
    use_settings(jobs_secret="job-secret-123", **MAIL)
    mailer.ok = False
    add_rows(("Sagar", 300, "seed"), when=date.today() - timedelta(days=date.today().weekday() + 3))
    run_daily(jobs_client, force="weekly")
    assert tg.documents and "email failed" in tg.texts_to(ADMIN_ID)[-1]


def test_one_failing_task_does_not_stop_the_others(jobs_client, monkeypatch):
    use_settings(jobs_secret="job-secret-123")

    def boom(settings, tg, llm):
        raise RuntimeError("x")
    monkeypatch.setattr(jobs, "DAILY_HOOKS", [("broken", boom), ("fine", lambda s, t, m: "ok")])
    r = run_daily(jobs_client)
    assert r.json()["results"]["broken"] == "failed: RuntimeError"
    assert r.json()["results"]["fine"] == "ok"


def test_amount_precision_in_report_total(bot):
    add_rows(("Sagar", Decimal("0.50"), "a"), ("Sagar", Decimal("0.50"), "b"))
    bot.send(SAGAR, "report")
    assert "₹1 total" in bot.tg.documents[-1]["caption"]


