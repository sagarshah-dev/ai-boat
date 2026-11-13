"""Database tables.

Each table maps one tab of the old bot's Google Sheet (column names noted next to the field), plus
three tables for state the old bot kept in RAM (members, processed_updates, pending_actions).
Changing anything here needs a new migration in migrations/versions — tests/test_migrations.py
fails if the two ever drift apart.
"""
from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, MetaData,
    Numeric, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names, so later migrations can find and change them by name.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMBED_DIM = 768  # gemini-embedding-001 at 768 dims, same as the old bot's index

ROLES = ("admin", "owner", "guest")
STATUSES = ("pending", "active", "removed")
# "planned": saved by Farm Salah after ✅ — decided, not done yet. It becomes "spray" when the
# 2-day reminder is answered "done", so "when did we last spray?" never counts a plan.
ACTIVITIES = ("plantation", "fertilizer", "spray", "treatment", "irrigation", "pruning",
              "observation", "harvest", "dead", "replant", "other", "planned")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# 7-day "how did it go?" for a treatment: not asked yet, asked, answered, or "don't know".
FOLLOW_UP = ("", "sent", "done", "skipped")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    """Who may use the bot. Replaces OWNERS + ALLOWED_CHAT_IDS env vars of the old bot.

    Identity is the Telegram user ID, never the display name: names change and collide,
    and the old bot's first_name matching was an open bug.
    """
    __tablename__ = "members"
    __table_args__ = (
        CheckConstraint(_in("role", ROLES), name="role"),
        CheckConstraint(_in("status", STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL = a fixed payer who has not used the bot (yet). Such a row shares expenses but can
    # never log in: access is looked up BY Telegram ID (migration 0003).
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(Text)
    # Other names people type for this person: "jignesh" is Lalo, "bako" is Biren.
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    telegram_username: Mapped[str | None] = mapped_column(Text)
    # Only admin and owner share expenses; a guest uses every feature but is never in the split.
    role: Mapped[str] = mapped_column(Text, server_default="guest")
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class Settlement(Base):
    """One settle-up event. Replaces the '═══ SETTLED ═══' marker row in the Expenses tab."""
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    settled_on: Mapped[date] = mapped_column(Date)
    created_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    created_at: Mapped[datetime] = _created()


class SettlementTransfer(Base):
    """Settlements tab: Date, From, To, Amount — one row per payment in the settle-up plan."""
    __tablename__ = "settlement_transfers"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), index=True)
    from_member: Mapped[int] = mapped_column(ForeignKey("members.id"))
    to_member: Mapped[int] = mapped_column(ForeignKey("members.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Expense(Base):
    """Expenses tab: ID, Date, Amount, Description, PaidBy, Merchant, Category, EnteredBy, Settled."""
    __tablename__ = "expenses"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_date: Mapped[date] = mapped_column(Date)
    # NUMERIC, never float: float sums gave the old bot a ₹1 rounding difference.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    description: Mapped[str] = mapped_column(Text)
    paid_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    merchant: Mapped[str] = mapped_column(Text, server_default="")
    category: Mapped[str] = mapped_column(Text, server_default="")
    entered_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    # Settled = Yes/No in the sheet. NULL = not settled yet; otherwise which settle-up paid it.
    settlement_id: Mapped[int | None] = mapped_column(ForeignKey("settlements.id"), index=True)
    created_at: Mapped[datetime] = _created()
    # Undo marks the row instead of deleting it, so history and reports stay explainable.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Meeting(Base):
    """Meetings tab: ID, Title, Date, Time, DurationMin, Agenda, CalendarEventId, CreatedBy,
    Status, ReminderSent, UpdatedAt. Date + Time are one timezone-aware timestamp here."""
    __tablename__ = "meetings"
    __table_args__ = (
        CheckConstraint(_in("status", ("scheduled", "cancelled")), name="status"),
        CheckConstraint("duration_min > 0", name="duration_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_min: Mapped[int] = mapped_column(Integer, server_default="60")
    agenda: Mapped[str] = mapped_column(Text, server_default="")
    calendar_event_id: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    status: Mapped[str] = mapped_column(Text, server_default="scheduled")
    reminder_sent: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JournalEntry(Base):
    """Journal tab, all 22 columns: ID, Date, DateTo, Plant, Variety, Location, PlantQty, Activity,
    Product, Dose, TimeOfDay, Problem, Result, ResultDate, Note, Photo, Source, AddedBy, AddedAt,
    Indexed, FollowUp, NoteEnglish."""
    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint(_in("activity", ACTIVITIES), name="activity"),
        CheckConstraint(_in("follow_up", FOLLOW_UP), name="follow_up"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    date_to: Mapped[date | None] = mapped_column(Date)
    plant: Mapped[str] = mapped_column(Text)
    variety: Mapped[str] = mapped_column(Text, server_default="")
    location: Mapped[str] = mapped_column(Text, server_default="")
    plant_qty: Mapped[int | None] = mapped_column(Integer)
    activity: Mapped[str] = mapped_column(Text)
    product: Mapped[str] = mapped_column(Text, server_default="")
    dose: Mapped[str] = mapped_column(Text, server_default="")
    time_of_day: Mapped[str] = mapped_column(Text, server_default="")
    problem: Mapped[str] = mapped_column(Text, server_default="")
    result: Mapped[str] = mapped_column(Text, server_default="")
    result_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str] = mapped_column(Text, server_default="")
    # Telegram file_id: a service account on personal Gmail has no Drive quota for photos.
    photo: Mapped[str] = mapped_column(Text, server_default="")
    source: Mapped[str] = mapped_column(Text, server_default="")
    added_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    added_at: Mapped[datetime] = _created()
    # Indexed in the sheet. NULL = not yet in rag_chunks.
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up: Mapped[str] = mapped_column(Text, server_default="")
    note_english: Mapped[str] = mapped_column(Text, server_default="")


class Plant(Base):
    """Plants tab: Plant, Variety, Aliases, Count, CountDate."""
    __tablename__ = "plants"
    __table_args__ = (UniqueConstraint("plant", "variety", name="uq_plants_plant_variety"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plant: Mapped[str] = mapped_column(Text)
    variety: Mapped[str] = mapped_column(Text, server_default="")
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    count: Mapped[int] = mapped_column(Integer, server_default="0")
    count_date: Mapped[date | None] = mapped_column(Date)


class Knowledge(Base):
    """Knowledge tab: ID, Date, Title, Text, Source, AddedBy, AddedAt, Indexed, TextEnglish."""
    __tablename__ = "knowledge"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default="")
    added_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    added_at: Mapped[datetime] = _created()
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text_english: Mapped[str] = mapped_column(Text, server_default="")


class SearchStopword(Base):
    """Words that carry no topic in a question ("have", "shu", "karu"). Data, not code: a new
    filler word is a row, and the keyword search stops matching notes on it."""
    __tablename__ = "search_stopwords"

    word: Mapped[str] = mapped_column(Text, primary_key=True)


class RagChunk(Base):
    """Search index for journal + knowledge (old Neon table farm_chunks, same columns).

    row_id points at journal_entries or knowledge depending on `kind`, so it has no foreign key.
    """
    __tablename__ = "rag_chunks"
    __table_args__ = (
        CheckConstraint(_in("kind", ("journal", "knowledge")), name="kind"),
        Index("ix_rag_chunks_terms", "terms", postgresql_using="gin"),
        Index("ix_rag_chunks_kind_row", "kind", "row_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    row_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    plants: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    variety: Mapped[str] = mapped_column(Text, server_default="")
    location: Mapped[str] = mapped_column(Text, server_default="")
    activity: Mapped[str] = mapped_column(Text, server_default="")
    day: Mapped[date | None] = mapped_column(Date)
    meta: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    terms: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProcessedUpdate(Base):
    """Telegram update IDs already handled. Was an in-memory Set in the old bot, so a restart
    forgot it and a Telegram retry could save the same expense twice."""
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)


class ConversationTurn(Base):
    """Short-term memory: one message of a chat, by one member (role user) or the bot's reply to
    them (role bot). Only the last few, for a short time, are kept (app/services/memory.py)."""
    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'bot')", name="role"),
        Index("ix_conversation_turns_chat_member_time", "chat_id", "member_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    role: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class PendingAction(Base):
    """A question the bot is waiting on (missing amount, yes/no confirm, settle PIN).
    Was an in-memory Map in the old bot; a restart lost it mid-conversation."""
    __tablename__ = "pending_actions"
    __table_args__ = (
        UniqueConstraint("chat_id", "member_id", "kind", name="uq_pending_actions_chat_member_kind"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = _created()
