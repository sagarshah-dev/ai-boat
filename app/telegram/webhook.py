"""Entry point for every Telegram update: check it is genuine, drop duplicates, check access,
route it, then send the replies.

Order matters: replies are sent only AFTER the database transaction commits, so the bot never
says "approved" or "saved" for a change that was then rolled back.
"""
import hmac
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.logging import bind, reset
from app.db.models import ProcessedUpdate
from app.db.session import session_scope
from app.services import members
from app.services.members import Decision, DecisionOutcome, StartOutcome, TgUser
from app.telegram.client import Buttons, TelegramClient

log = logging.getLogger(__name__)
router = APIRouter()

ROLE_LABEL = {"admin": "Admin", "owner": "Owner", "guest": "Guest"}


# ── Replies are collected as data first, then sent after commit ─────────────────────────────

@dataclass(frozen=True)
class Send:
    chat_id: int
    text: str
    buttons: Buttons | None = None


@dataclass(frozen=True)
class Edit:
    chat_id: int
    message_id: int
    text: str


@dataclass(frozen=True)
class AnswerButton:
    callback_id: str
    text: str | None = None


Action = Send | Edit | AnswerButton


@lru_cache
def get_telegram() -> TelegramClient:
    return TelegramClient(get_settings().telegram_token)


@router.post("/telegram/webhook")
def telegram_webhook(
    update: dict[str, Any] = Body(...),
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    settings: Settings = Depends(get_settings),
    tg: TelegramClient = Depends(get_telegram),
) -> dict[str, bool]:
    # Constant-time compare: a plain == leaks how many leading characters were right.
    if not hmac.compare_digest((secret or "").encode(), settings.telegram_webhook_secret.encode()):
        raise HTTPException(status_code=401, detail="bad secret")
    handle_update(update, tg, settings)
    # Always 200 for a genuine update, even after an internal error: a non-200 makes Telegram
    # retry the same update for hours and hold back every newer message behind it.
    return {"ok": True}


def handle_update(update: dict[str, Any], tg: TelegramClient, settings: Settings) -> None:
    """Shared by the webhook (Cloud Run) and scripts/dev_polling.py (local), so both behave alike."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        log.warning("update without update_id ignored")
        return
    chat_id = _chat_id(update)
    token = bind(update_id=update_id, chat_id=chat_id)
    try:
        # Claimed in its own committed transaction BEFORE any work. If the work then fails, the
        # update is not retried (at-most-once): for money, a lost message the user can resend is
        # far better than an expense saved twice.
        with session_scope() as session:
            if not claim_update(session, update_id):
                log.info("duplicate update skipped")
                return
        with session_scope() as session:
            actions = route(session, update, settings)
        deliver(tg, actions)
    except Exception:
        log.exception("update failed")
        if chat_id is not None:
            tg.send_message(chat_id, "⚠️ Something went wrong on my side. Please send that again.")
    finally:
        reset(token)


def claim_update(session: Session, update_id: int) -> bool:
    """True the first time this update_id is seen, False for a Telegram retry."""
    stmt = (pg_insert(ProcessedUpdate).values(update_id=update_id)
            .on_conflict_do_nothing(index_elements=["update_id"])
            .returning(ProcessedUpdate.update_id))
    return session.execute(stmt).first() is not None


def deliver(tg: TelegramClient, actions: list[Action]) -> None:
    for action in actions:
        if isinstance(action, Send):
            tg.send_message(action.chat_id, action.text, action.buttons)
        elif isinstance(action, Edit):
            tg.edit_message_text(action.chat_id, action.message_id, action.text)
        elif isinstance(action, AnswerButton):
            tg.answer_callback(action.callback_id, action.text)


# ── Routing ─────────────────────────────────────────────────────────────────────────────────

def route(session: Session, update: dict[str, Any], settings: Settings) -> list[Action]:
    if "callback_query" in update:
        return _on_button(session, update["callback_query"], settings)
    message = update.get("message")
    if message and message.get("from"):
        return _on_message(session, message, settings)
    return []  # edited messages, channel posts, etc. are not used yet


def _on_message(session: Session, message: dict[str, Any], settings: Settings) -> list[Action]:
    chat_id = message["chat"]["id"]
    user = _tg_user(message["from"])
    text = (message.get("text") or "").strip()

    # "/start" or "/start@BotName" (the form Telegram uses in groups)
    if text.split("@")[0].split(" ")[0].lower() == "/start":
        return _on_start(session, chat_id, user, settings)

    member = members.get_active(session, user.id)
    if member is None:
        return [Send(chat_id, "🔒 You don't have access yet. Send /start to request it.")]

    # Expenses, journal and AI arrive in the next steps; until then, prove the pipe works.
    return [Send(chat_id, f"✅ Got it, {member.name}. Expenses and AI are coming in the next update.")]


def _on_start(session: Session, chat_id: int, user: TgUser, settings: Settings) -> list[Action]:
    outcome, member = members.request_access(session, user, settings.admin_telegram_id)

    if outcome is StartOutcome.ADMIN_NOT_CONFIGURED:
        return [Send(chat_id, "⚙️ Setup is not finished yet.\n"
                              f"Your Telegram ID is {user.id}.\n"
                              "Put it in ADMIN_TELEGRAM_ID and restart the bot.")]
    if outcome is StartOutcome.ADMIN_READY:
        return [Send(chat_id, f"👋 Welcome {user.name}! You are the admin.\n"
                              "New people send /start, and you get Approve buttons here.")]
    if outcome is StartOutcome.ALREADY_ACTIVE:
        return [Send(chat_id, "✅ You already have access.")]
    if outcome is StartOutcome.REQUEST_PENDING:
        return [Send(chat_id, "⏳ Your request is waiting for the admin.")]

    assert member is not None
    handle = f" (@{user.username})" if user.username else ""
    return [
        Send(chat_id, "📨 Request sent to the admin. You'll get a message here once it's approved."),
        Send(settings.admin_telegram_id,
             f"🙋 New access request\nName: {user.name}{handle}\nTelegram ID: {user.id}",
             buttons=[[("✅ Owner", f"join:{member.id}:owner"), ("✅ Guest", f"join:{member.id}:guest")],
                      [("❌ Reject", f"join:{member.id}:reject")]]),
    ]


def _on_button(session: Session, query: dict[str, Any], settings: Settings) -> list[Action]:
    callback_id = query["id"]
    data = query.get("data") or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "join" or not parts[1].isdigit():
        return [AnswerButton(callback_id, "This button is no longer valid.")]
    try:
        decision = Decision(parts[2])
    except ValueError:
        return [AnswerButton(callback_id, "This button is no longer valid.")]

    outcome, member = members.decide(session, query["from"]["id"], int(parts[1]), decision)
    if outcome is DecisionOutcome.NOT_ADMIN:
        return [AnswerButton(callback_id, "Only the admin can do this.")]
    if outcome is DecisionOutcome.NOT_FOUND:
        return [AnswerButton(callback_id, "Request not found.")]
    assert member is not None
    if outcome is DecisionOutcome.ALREADY_DECIDED:
        return [AnswerButton(callback_id, "Already decided.")]

    actions: list[Action] = [AnswerButton(callback_id)]
    msg = query.get("message")
    if outcome is DecisionOutcome.APPROVED:
        label = ROLE_LABEL[member.role]
        if msg:
            actions.append(Edit(msg["chat"]["id"], msg["message_id"],
                                f"✅ {member.name} approved as {label}."))
        actions.append(Send(member.telegram_user_id,
                            f"✅ You're in! Access approved as {label}. Send any message to start."))
    else:
        if msg:
            actions.append(Edit(msg["chat"]["id"], msg["message_id"], f"❌ {member.name} rejected."))
        actions.append(Send(member.telegram_user_id, "Sorry, your access request was not approved."))
    return actions


# ── Small helpers ───────────────────────────────────────────────────────────────────────────

def _tg_user(raw: dict[str, Any]) -> TgUser:
    name = " ".join(p for p in (raw.get("first_name"), raw.get("last_name")) if p) or "Unknown"
    return TgUser(id=raw["id"], name=name, username=raw.get("username"))


def _chat_id(update: dict[str, Any]) -> int | None:
    if "message" in update:
        return update["message"].get("chat", {}).get("id")
    if "callback_query" in update:
        return update["callback_query"].get("message", {}).get("chat", {}).get("id")
    return None
