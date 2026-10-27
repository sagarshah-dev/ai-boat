"""Talks to the Telegram Bot API. Nothing else in the app knows Telegram's URLs or JSON shapes.

Sending never raises: a failed reply is logged and reported as False. One lost reply is bad, but
an exception here would abort the database transaction of an expense that was already decided.
"""
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)

_RETRIES = 3
_BACKOFF_S = (0.5, 1.5)
_CALLBACK_DATA_MAX_BYTES = 64  # Telegram rejects the whole message above this


class TelegramClient:
    def __init__(self, token: str, timeout_s: float = 10.0, http: httpx.Client | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = http or httpx.Client(timeout=timeout_s)

    def call(self, method: str, payload: dict[str, Any] | None = None,
             timeout_s: float | None = None) -> dict[str, Any] | None:
        """POST one Bot API method. Retries network errors, 429 and 5xx. Returns `result` or None."""
        for attempt in range(_RETRIES):
            try:
                resp = self._http.post(f"{self._base}/{method}", json=payload or {},
                                       timeout=timeout_s or httpx.USE_CLIENT_DEFAULT)
                body = resp.json()
                if body.get("ok"):
                    return body.get("result")
                retry = resp.status_code == 429 or resp.status_code >= 500
                # The token is in the URL, so log the method name, never the URL.
                log.warning("telegram call failed", extra={"fields": {
                    "method": method, "status": resp.status_code,
                    "description": body.get("description"), "attempt": attempt + 1}})
                if not retry:
                    return None
                wait = body.get("parameters", {}).get("retry_after")
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telegram call error", extra={"fields": {
                    "method": method, "error": type(exc).__name__, "attempt": attempt + 1}})
                wait = None
            if attempt < _RETRIES - 1:
                time.sleep(min(float(wait), 5.0) if wait else _BACKOFF_S[attempt])
        return None

    def send_message(self, chat_id: int, text: str, buttons: Buttons | None = None) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = _keyboard(buttons)
        return self.call("sendMessage", payload) is not None

    def send_typing(self, chat_id: int) -> None:
        """Shows "typing…" while the AI works. Cosmetic: one try, short timeout, never retried,
        so it can never delay the real reply."""
        try:
            self._http.post(f"{self._base}/sendChatAction",
                            json={"chat_id": chat_id, "action": "typing"}, timeout=3.0)
        except httpx.HTTPError:
            pass

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> bool:
        """Replaces a message's text and removes its buttons, so nobody can press them twice."""
        return self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text}) is not None

    def answer_callback(self, callback_id: str, text: str | None = None) -> bool:
        """Stops the loading spinner Telegram shows on a pressed button."""
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        return self.call("answerCallbackQuery", payload) is not None


def _keyboard(buttons: Buttons) -> dict[str, Any]:
    rows = []
    for row in buttons:
        out = []
        for label, data in row:
            if len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
                raise ValueError(f"callback_data over {_CALLBACK_DATA_MAX_BYTES} bytes: {data!r}")
            out.append({"text": label, "callback_data": data})
        rows.append(out)
    return {"inline_keyboard": rows}
