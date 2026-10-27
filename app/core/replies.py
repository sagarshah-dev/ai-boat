"""What the bot wants to send back, as plain data.

Services and the AI layer return these; only app/telegram turns them into Bot API calls. Nothing
is sent until the database transaction has committed, so the bot never announces a change that
was rolled back. An Email runs after the commit too, and its outcome becomes one more message.
"""
from dataclasses import dataclass, field

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback data)


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: Buttons | None = None


@dataclass(frozen=True)
class Document:
    filename: str
    data: bytes
    caption: str = ""


@dataclass(frozen=True)
class Photo:
    file_id: str      # a Telegram file_id: the photo is re-sent from Telegram's own storage
    caption: str = ""


@dataclass(frozen=True)
class Email:
    subject: str
    body: str
    to: tuple[str, ...]
    attachments: tuple[tuple[str, bytes, str], ...] = ()   # (filename, data, mime type)
    ok_note: str = ""       # told to the user when sent ("" = say nothing)
    fail_note: str = "⚠️ The email could not be sent."


Out = Reply | Document | Photo | Email


@dataclass
class Outbox:
    """Collects replies in order; helpers keep call sites short."""
    items: list[Out] = field(default_factory=list)

    def say(self, text: str, buttons: Buttons | None = None) -> "Outbox":
        if text:
            self.items.append(Reply(text, buttons))
        return self

    def add(self, *items: Out) -> "Outbox":
        self.items.extend(items)
        return self


YES_NO: Buttons = [[("✅ Save", "yes"), ("❌ Cancel", "no")]]


def yes_no(yes_label: str = "✅ Save") -> Buttons:
    return [[(yes_label, "yes"), ("❌ Cancel", "no")]]
