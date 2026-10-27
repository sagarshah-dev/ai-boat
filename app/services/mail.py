"""Which emails the bot sends and what they say. Building only — sending happens after commit
(app/integrations/email.py), and its outcome is reported in Telegram."""
from app.config import Settings
from app.core.formatting import inr
from app.core.replies import Email
from app.services.balance import Balance
from app.services.reports import Report


def ready(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password
                and settings.mail_from and settings.recipients)


NOT_SET_UP = ("📧 Email is not set up yet (SMTP settings and RECIPIENT_EMAILS on Cloud Run). "
              "Use \"report\" to get the PDF here instead.")


def report_email(settings: Settings, report: Report, subject_prefix: str,
                 balance: Balance | None = None) -> Email:
    body = (f"Attached: AI BOaT expense report — {report.label}.\n"
            f"Total: ₹{inr(report.total)}\nEntries: {report.count}\n\n"
            "(Blue amounts in the PDF are entries over ₹5,000.)")
    if balance is not None:
        # The balance text is the SAME string the "balance" command shows, pasted verbatim, so
        # the email and the chat can never disagree.
        body += f"\n\n{'─' * 32}\n{balance.text}"
    to = settings.recipients
    return Email(
        subject=f"{subject_prefix} — ₹{inr(report.total)} ({report.count} entries)",
        body=body, to=to,
        attachments=((report.filename, report.data, "application/pdf"),),
        ok_note=f"📧 {subject_prefix} emailed to {', '.join(to)}.",
        fail_note=f"⚠️ The {subject_prefix.lower()} email could not be sent. The PDF is in this chat.",
    )
