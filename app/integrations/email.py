"""Sending email through an SMTP relay (Brevo in this project, any provider works).

Never raises: the caller is told True/False and decides what to say. Email always runs after the
database commit, so a slow or broken mail server can never undo, or appear to undo, a change
that already happened.

Old bot lesson: Render silently blocked SMTP ports and every email failed for weeks without
anyone noticing. Here every failure is logged with its reason AND reported back in Telegram.
"""
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.core.replies import Email

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str

    @property
    def configured(self) -> bool:
        return all((self.host, self.port, self.user, self.password, self.sender))


class Mailer:
    def __init__(self, cfg: SmtpSettings, timeout_s: float = 20.0):
        self.cfg = cfg
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def build(self, email: Email) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = email.subject
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(email.to)
        msg.set_content(email.body)
        for filename, data, mime in email.attachments:
            # "text/calendar; method=REQUEST" -> type, subtype and parameters, so calendar apps
            # recognise an invite (REQUEST) or a cancellation (CANCEL).
            kind, *raw_params = [p.strip() for p in mime.split(";")]
            main, _, sub = kind.partition("/")
            params = dict(p.split("=", 1) for p in raw_params if "=" in p)
            msg.add_attachment(data, maintype=main, subtype=sub or "octet-stream",
                               filename=filename, params=params or None)
        return msg

    def send(self, email: Email) -> bool:
        if not self.configured:
            log.warning("email skipped: SMTP is not configured")
            return False
        if not email.to:
            log.warning("email skipped: no recipients")
            return False
        try:
            with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=self.timeout_s) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.cfg.user, self.cfg.password)
                smtp.send_message(self.build(email))
            log.info("email sent", extra={"fields": {"subject": email.subject, "to": len(email.to)}})
            return True
        except (OSError, smtplib.SMTPException) as exc:
            log.error("email failed", extra={"fields": {"error": type(exc).__name__, "detail": str(exc)[:200]}})
            return False
