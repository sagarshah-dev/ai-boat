"""All settings come from environment variables (a local .env file, or Cloud Run variables).

The app refuses to start when a required value is missing, so a typo in Cloud Run fails at deploy
time with a clear message instead of failing later on the first Telegram message.
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_token: str = Field(min_length=10)
    # Telegram sends this back in a header on every webhook call; without it anyone who finds
    # the URL could post fake updates.
    telegram_webhook_secret: str = Field(min_length=16)
    database_url: str = Field(min_length=10)
    # 0 = not set yet. The bot then tells whoever writes to it their own ID, so the owner can
    # find theirs without any extra tool.
    admin_telegram_id: int = 0
    log_level: str = "INFO"

    gemini_api_key: str = Field(min_length=1)
    gemini_model: str = "gemini-3.5-flash-lite"
    # Tried only when the main model stays overloaded or slow after retries. Empty = none.
    gemini_fallback_model: str = ""

    # PIN for "settle". Empty = settling is switched off (fail closed).
    settle_pin: str = ""
    # "Today" for expense dates. The server runs in UTC; the owners live in India.
    timezone: str = "Asia/Kolkata"

    # Email (reports, settle snapshot, meeting invites). All empty = email switched off.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    recipient_emails: str = ""          # comma-separated

    # Protects the /jobs/* URLs that Cloud Scheduler calls. Empty = jobs switched off.
    jobs_secret: str = ""
    # Where scheduled messages (farm follow-ups, reports notice) go. 0 = the admin's chat.
    notify_chat_id: int = 0

    # Google Calendar for meetings. Empty = meetings are kept in the bot only.
    calendar_id: str = ""

    @property
    def recipients(self) -> tuple[str, ...]:
        return tuple(e.strip() for e in self.recipient_emails.split(",") if e.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
