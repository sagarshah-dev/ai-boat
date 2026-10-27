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


@lru_cache
def get_settings() -> Settings:
    return Settings()
