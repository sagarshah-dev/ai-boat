"""Point Telegram at the deployed Cloud Run service.

Usage (once after each new service URL, with .env filled in):
    python -m scripts.set_webhook https://ai-boat-xxxxx.asia-southeast1.run.app
"""
import json
import sys

from app.config import get_settings
from app.telegram.webhook import get_telegram


def main(base_url: str) -> None:
    settings = get_settings()
    tg = get_telegram()
    ok = tg.call("setWebhook", {
        "url": base_url.rstrip("/") + "/telegram/webhook",
        # Telegram echoes this in a header on every call; the app rejects calls without it.
        "secret_token": settings.telegram_webhook_secret,
        "allowed_updates": ["message", "callback_query"],
    })
    print("setWebhook:", "ok" if ok else "FAILED")
    info = tg.call("getWebhookInfo") or {}
    print(json.dumps({k: info.get(k) for k in ("url", "pending_update_count", "last_error_message")},
                     indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("usage: python -m scripts.set_webhook https://<cloud-run-url>")
    main(sys.argv[1])
