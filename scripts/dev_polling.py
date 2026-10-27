"""Run the bot on your own PC, no public URL needed.

Telegram can only push webhooks to a public HTTPS address, which a home PC doesn't have. Instead,
this asks Telegram for new messages in a loop (long polling) and hands each one to the SAME
handle_update() the Cloud Run webhook uses — so what works here works there.

Usage (from the repo root, with .env filled in):
    python -m scripts.dev_polling

Stop with Ctrl+C. This removes the webhook while it runs; run scripts.set_webhook again after
deploying to Cloud Run.
"""
import logging

from app.config import get_settings
from app.core.logging import setup_logging
from app.telegram.webhook import get_telegram, handle_update

log = logging.getLogger("dev_polling")
POLL_TIMEOUT_S = 25


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    tg = get_telegram()

    # Telegram refuses getUpdates while a webhook is set.
    tg.call("deleteWebhook", {"drop_pending_updates": False})
    log.info("polling started — message the bot in Telegram, Ctrl+C to stop")

    offset = None
    while True:
        updates = tg.call("getUpdates", {
            "timeout": POLL_TIMEOUT_S,
            "offset": offset,
            "allowed_updates": ["message", "callback_query"],
        }, timeout_s=POLL_TIMEOUT_S + 10) or []
        for update in updates:
            handle_update(update, tg, settings)
            offset = update["update_id"] + 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
