"""One JSON object per log line.

Cloud Run reads `severity` and `message` from JSON logs, so errors show up red in Logs Explorer
and every line can be filtered by update_id or chat_id — "what happened to that one message?"
becomes a single search instead of scrolling.
"""
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

# Set once per Telegram update; every log line written while handling it carries these.
_context: contextvars.ContextVar[dict] = contextvars.ContextVar("log_context", default={})


def bind(**fields) -> contextvars.Token:
    return _context.set({**_context.get(), **fields})


def reset(token: contextvars.Token) -> None:
    _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_context.get(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            entry.update(extra)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs every request URL, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
