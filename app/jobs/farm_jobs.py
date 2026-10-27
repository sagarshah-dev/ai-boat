"""Daily farm work for /jobs/daily: the 7-day "how did it go?" questions, and keeping the search
index in step with the journal (notes whose indexing failed at save time, results added since).
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.ai import farm_tools
from app.config import Settings
from app.db.session import session_scope
from app.integrations.gemini import LLM
from app.services import farm, rag_index
from app.telegram.client import TelegramClient

log = logging.getLogger(__name__)
MAX_FOLLOW_UPS = 10      # a backlog after a quiet month must not flood the chat


def follow_ups(settings: Settings, tg: TelegramClient, llm: LLM) -> str:
    chat = settings.notify_chat_id or settings.admin_telegram_id
    if not chat:
        return "no chat to ask in"
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    with session_scope() as s:
        due = [(e.id, farm.journal_line(e)) for e in farm.followups_due(s, today)]
    asked = []
    for jid, line in due[:MAX_FOLLOW_UPS]:
        if tg.send_message(chat, f"🔔 7-day check:\n{line}\n\nHow is it now?",
                           farm_tools.followup_buttons(jid)):
            asked.append(jid)
    # Marked only after Telegram accepted the message: a failed send is asked again tomorrow.
    with session_scope() as s:
        for jid in asked:
            farm.mark_followup(s, jid, "sent")
    return f"{len(due)} due, {len(asked)} asked"


def search_sync(settings: Settings, tg: TelegramClient, llm: LLM) -> str:
    with session_scope() as s:
        r = rag_index.sync(s, llm)
    return f"embedded {r['embedded']}, deleted {r['deleted']}, total {r['total']}"


# What Telegram shows when someone types "/". Registered by the daily job (one idempotent call),
# so nobody has to paste a list into BotFather and it can never drift from the code.
BOT_COMMANDS = [
    ("menu", "Buttons: farm note, ask the farm, counts, balance, meetings"),
    ("balance", "Who is pending, who gets back"),
    ("recent", "Last 5 entries"),
    ("undo", "Remove your own last entry"),
    ("report", "PDF report, e.g. /report fuel"),
    ("email", "Email the PDF report"),
    ("settle", "Settle up (needs the PIN)"),
    ("help", "Everything the bot can do"),
]


def bot_commands(settings: Settings, tg: TelegramClient, llm: LLM) -> str:
    ok = tg.call("setMyCommands", {"commands": [{"command": c, "description": d}
                                                for c, d in BOT_COMMANDS]})
    return "set" if ok is not None else "failed"
