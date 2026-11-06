"""Scheduled work, triggered by ONE Cloud Scheduler job calling POST /jobs/daily once a day.

One job, not one per task: Cloud Scheduler is free for 3 jobs per billing account, and one daily
entry point decides by date what is due (weekly report on Mondays, monthly on the 1st). Each task
runs on its own: one failing never stops the others, and every failure is reported in Telegram
— the old bot's email failed silently for weeks.
"""
import hmac
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import delete

from app.config import Settings, get_settings
from app.core.formatting import entries, inr
from app.db.models import PendingAction, ProcessedUpdate
from app.db.session import session_scope
from app.integrations.email import Mailer
from app.integrations.gemini import LLM
from app.jobs import farm_jobs
from app.services import balance, mail, reports
from app.telegram.client import TelegramClient
from app.telegram.webhook import get_llm, get_mailer, get_telegram

log = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs")

KEEP_UPDATES = timedelta(days=7)

# Daily tasks of other features. Each gets (settings, tg, llm) and returns a one-line result.
DAILY_HOOKS: list[tuple[str, Callable[[Settings, TelegramClient, LLM], str]]] = [
    ("farm_follow_ups", farm_jobs.follow_ups),
    ("search_sync", farm_jobs.search_sync),
    ("bot_commands", farm_jobs.bot_commands),
]


def _check(secret: str | None, settings: Settings) -> None:
    if not settings.jobs_secret:
        raise HTTPException(status_code=503, detail="JOBS_SECRET is not configured")
    if not hmac.compare_digest((secret or "").encode(), settings.jobs_secret.encode()):
        raise HTTPException(status_code=401, detail="bad secret")


def notify_chat(settings: Settings) -> int:
    return settings.notify_chat_id or settings.admin_telegram_id


@router.post("/daily")
def daily(
    secret: str | None = Header(default=None, alias="X-Jobs-Secret"),
    force: str = Query(default="", description="weekly / monthly: run that report today too"),
    settings: Settings = Depends(get_settings),
    tg: TelegramClient = Depends(get_telegram),
    mailer: Mailer = Depends(get_mailer),
    llm: LLM = Depends(get_llm),
) -> dict:
    _check(secret, settings)
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    results: dict[str, str] = {"cleanup": _run("cleanup", cleanup)}
    if today.weekday() == 0 or force == "weekly":
        results["weekly"] = _run("weekly", lambda: send_report(
            settings, tg, mailer, today, "last week", "Weekly Report"))
    if today.day == 1 or force == "monthly":
        results["monthly"] = _run("monthly", lambda: send_report(
            settings, tg, mailer, today, "last month", "Monthly Report", with_balance=True))
    for name, hook in DAILY_HOOKS:
        results[name] = _run(name, lambda h=hook: h(settings, tg, llm))
    log.info("daily job done", extra={"fields": results})
    return {"ok": True, "date": today.isoformat(), "results": results}


def _run(name: str, task: Callable[[], str]) -> str:
    try:
        return task()
    except Exception as exc:  # noqa: BLE001 — reported, and the next task still runs
        log.exception("daily task failed", extra={"fields": {"task": name}})
        return f"failed: {type(exc).__name__}"


def cleanup() -> str:
    """Duplicate-check IDs older than a week can no longer be retried by Telegram; expired
    waiting questions are dead. Both would otherwise grow forever."""
    cutoff = datetime.now(timezone.utc) - KEEP_UPDATES
    with session_scope() as s:
        updates = s.execute(delete(ProcessedUpdate).where(ProcessedUpdate.received_at < cutoff)).rowcount
        waiting = s.execute(delete(PendingAction).where(
            PendingAction.expires_at < datetime.now(timezone.utc))).rowcount
    return f"removed {updates} old update ids, {waiting} expired questions"


def send_report(settings: Settings, tg: TelegramClient, mailer: Mailer, today: date,
                scope_text: str, title: str, with_balance: bool = False) -> str:
    chat = notify_chat(settings)
    with session_scope() as s:
        scope = reports.parse_scope(s, scope_text, today)
        now = datetime.now(ZoneInfo(settings.timezone))
        report = reports.build_report(s, scope, today=today, now=now)
        bal = balance.compute(s) if with_balance else None
    if not report.count:
        return f"no entries for {scope.label}"
    caption = f"📄 {title} — {entries(report.count)}, ₹{inr(report.total)}"
    if mail.ready(settings):
        if mailer.send(mail.report_email(settings, report, title, bal)):
            return f"emailed {entries(report.count)}"
        # Not hidden: the report still reaches people, and the failure is said out loud.
        if chat:
            tg.send_document(chat, report.filename, report.data, caption)
            tg.send_message(chat, f"⚠️ The {title.lower()} email failed; the PDF is above.")
        return "email failed, sent to Telegram"
    if chat:
        tg.send_document(chat, report.filename, report.data, caption)
        return "email not set up, sent to Telegram"
    return "email not set up and no chat to send to"
