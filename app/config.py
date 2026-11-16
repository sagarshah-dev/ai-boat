"""All settings come from environment variables (a local .env file, or Cloud Run variables).

The app refuses to start when a required value is missing, so a typo in Cloud Run fails at deploy
time with a clear message instead of failing later on the first Telegram message.
"""
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MCP_MIN_PASSWORD = 12


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
    # Key of the service account the calendar is shared with (the JSON text itself).
    google_service_account_json: str = ""

    # Farm search: how close in meaning a note must be to count as found. Tuned by the eval run
    # with the real embedding model (old bot: 0.62), never by feel.
    rag_min_score: float = 0.62
    # A voice note at least this long is treated as the farmer's advice, not a quick note.
    knowledge_min_seconds: int = 60

    # MCP server for Claude (app/mcp). Both empty = /mcp is not mounted at all (fail closed).
    # public_base_url is the service's own https URL: OAuth metadata and token audience are
    # built from it, and Claude checks they match the URL typed into the connector exactly.
    public_base_url: str = ""
    # The one person allowed to sign in (Sagar). Money data, so a short password is refused.
    mcp_owner_password: str = ""

    @model_validator(mode="after")
    def _mcp_settings_complete(self) -> "Settings":
        url, pw = self.public_base_url.strip(), self.mcp_owner_password
        if url or pw:
            # OAuth 2.1 allows plain http only for loopback addresses (local runs).
            if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValueError("PUBLIC_BASE_URL must be the https:// URL of this service")
            if len(pw) < MCP_MIN_PASSWORD:
                raise ValueError(f"MCP_OWNER_PASSWORD must be at least {MCP_MIN_PASSWORD} characters")
        return self

    @property
    def mcp_enabled(self) -> bool:
        return bool(self.public_base_url.strip() and self.mcp_owner_password)

    @property
    def recipients(self) -> tuple[str, ...]:
        return tuple(e.strip() for e in self.recipient_emails.split(",") if e.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
