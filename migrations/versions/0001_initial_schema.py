"""Initial schema: the old bot's six sheet tabs + search index + the state it kept in RAM.

Revision ID: 0001
Revises: 
Create Date: 2026-10-27 11:48:57.562086

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # pgvector must exist before rag_chunks can use the vector type.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('telegram_user_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('telegram_username', sa.Text(), nullable=True),
    sa.Column('role', sa.Text(), server_default='guest', nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('admin', 'owner', 'guest')", name=op.f('ck_members_role')),
    sa.CheckConstraint("status IN ('pending', 'active', 'removed')", name=op.f('ck_members_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_members')),
    sa.UniqueConstraint('telegram_user_id', name=op.f('uq_members_telegram_user_id'))
    )
    op.create_table('plants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('plant', sa.Text(), nullable=False),
    sa.Column('variety', sa.Text(), server_default='', nullable=False),
    sa.Column('aliases', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('count_date', sa.Date(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plants')),
    sa.UniqueConstraint('plant', 'variety', name='uq_plants_plant_variety')
    )
    op.create_table('processed_updates',
    sa.Column('update_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('update_id', name=op.f('pk_processed_updates'))
    )
    op.create_index(op.f('ix_processed_updates_received_at'), 'processed_updates', ['received_at'], unique=False)
    op.create_table('rag_chunks',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('row_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('hash', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('plants', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('variety', sa.Text(), server_default='', nullable=False),
    sa.Column('location', sa.Text(), server_default='', nullable=False),
    sa.Column('activity', sa.Text(), server_default='', nullable=False),
    sa.Column('day', sa.Date(), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('terms', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=768), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('journal', 'knowledge')", name=op.f('ck_rag_chunks_kind')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rag_chunks'))
    )
    op.create_index('ix_rag_chunks_kind_row', 'rag_chunks', ['kind', 'row_id'], unique=False)
    op.create_index('ix_rag_chunks_terms', 'rag_chunks', ['terms'], unique=False, postgresql_using='gin')
    op.create_table('journal_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entry_date', sa.Date(), nullable=False),
    sa.Column('date_to', sa.Date(), nullable=True),
    sa.Column('plant', sa.Text(), nullable=False),
    sa.Column('variety', sa.Text(), server_default='', nullable=False),
    sa.Column('location', sa.Text(), server_default='', nullable=False),
    sa.Column('plant_qty', sa.Integer(), nullable=True),
    sa.Column('activity', sa.Text(), nullable=False),
    sa.Column('product', sa.Text(), server_default='', nullable=False),
    sa.Column('dose', sa.Text(), server_default='', nullable=False),
    sa.Column('time_of_day', sa.Text(), server_default='', nullable=False),
    sa.Column('problem', sa.Text(), server_default='', nullable=False),
    sa.Column('result', sa.Text(), server_default='', nullable=False),
    sa.Column('result_date', sa.Date(), nullable=True),
    sa.Column('note', sa.Text(), server_default='', nullable=False),
    sa.Column('photo', sa.Text(), server_default='', nullable=False),
    sa.Column('source', sa.Text(), server_default='', nullable=False),
    sa.Column('added_by', sa.Integer(), nullable=False),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('follow_up', sa.Text(), server_default='', nullable=False),
    sa.Column('note_english', sa.Text(), server_default='', nullable=False),
    sa.CheckConstraint("activity IN ('plantation', 'fertilizer', 'spray', 'treatment', 'irrigation', 'pruning', 'observation', 'harvest', 'dead', 'replant', 'other')", name=op.f('ck_journal_entries_activity')),
    sa.CheckConstraint("follow_up IN ('', 'sent', 'done')", name=op.f('ck_journal_entries_follow_up')),
    sa.ForeignKeyConstraint(['added_by'], ['members.id'], name=op.f('fk_journal_entries_added_by_members')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_journal_entries'))
    )
    op.create_index(op.f('ix_journal_entries_entry_date'), 'journal_entries', ['entry_date'], unique=False)
    op.create_table('knowledge',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entry_date', sa.Date(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), server_default='', nullable=False),
    sa.Column('added_by', sa.Integer(), nullable=False),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('text_english', sa.Text(), server_default='', nullable=False),
    sa.ForeignKeyConstraint(['added_by'], ['members.id'], name=op.f('fk_knowledge_added_by_members')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge'))
    )
    op.create_table('meetings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration_min', sa.Integer(), server_default='60', nullable=False),
    sa.Column('agenda', sa.Text(), server_default='', nullable=False),
    sa.Column('calendar_event_id', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('status', sa.Text(), server_default='scheduled', nullable=False),
    sa.Column('reminder_sent', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('scheduled', 'cancelled')", name=op.f('ck_meetings_status')),
    sa.CheckConstraint('duration_min > 0', name=op.f('ck_meetings_duration_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['members.id'], name=op.f('fk_meetings_created_by_members')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meetings'))
    )
    op.create_index(op.f('ix_meetings_starts_at'), 'meetings', ['starts_at'], unique=False)
    op.create_table('pending_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_pending_actions_attempts_non_negative')),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], name=op.f('fk_pending_actions_member_id_members')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pending_actions')),
    sa.UniqueConstraint('chat_id', 'member_id', 'kind', name='uq_pending_actions_chat_member_kind')
    )
    op.create_index(op.f('ix_pending_actions_expires_at'), 'pending_actions', ['expires_at'], unique=False)
    op.create_table('settlements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('settled_on', sa.Date(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['members.id'], name=op.f('fk_settlements_created_by_members')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_settlements'))
    )
    op.create_table('expenses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('expense_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('paid_by', sa.Integer(), nullable=False),
    sa.Column('merchant', sa.Text(), server_default='', nullable=False),
    sa.Column('category', sa.Text(), server_default='', nullable=False),
    sa.Column('entered_by', sa.Integer(), nullable=False),
    sa.Column('settlement_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('amount > 0', name=op.f('ck_expenses_amount_positive')),
    sa.ForeignKeyConstraint(['entered_by'], ['members.id'], name=op.f('fk_expenses_entered_by_members')),
    sa.ForeignKeyConstraint(['paid_by'], ['members.id'], name=op.f('fk_expenses_paid_by_members')),
    sa.ForeignKeyConstraint(['settlement_id'], ['settlements.id'], name=op.f('fk_expenses_settlement_id_settlements')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_expenses'))
    )
    op.create_index(op.f('ix_expenses_settlement_id'), 'expenses', ['settlement_id'], unique=False)
    op.create_table('settlement_transfers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('settlement_id', sa.Integer(), nullable=False),
    sa.Column('from_member', sa.Integer(), nullable=False),
    sa.Column('to_member', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.CheckConstraint('amount > 0', name=op.f('ck_settlement_transfers_amount_positive')),
    sa.ForeignKeyConstraint(['from_member'], ['members.id'], name=op.f('fk_settlement_transfers_from_member_members')),
    sa.ForeignKeyConstraint(['settlement_id'], ['settlements.id'], name=op.f('fk_settlement_transfers_settlement_id_settlements')),
    sa.ForeignKeyConstraint(['to_member'], ['members.id'], name=op.f('fk_settlement_transfers_to_member_members')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_settlement_transfers'))
    )
    op.create_index(op.f('ix_settlement_transfers_settlement_id'), 'settlement_transfers', ['settlement_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_settlement_transfers_settlement_id'), table_name='settlement_transfers')
    op.drop_table('settlement_transfers')
    op.drop_index(op.f('ix_expenses_settlement_id'), table_name='expenses')
    op.drop_table('expenses')
    op.drop_table('settlements')
    op.drop_index(op.f('ix_pending_actions_expires_at'), table_name='pending_actions')
    op.drop_table('pending_actions')
    op.drop_index(op.f('ix_meetings_starts_at'), table_name='meetings')
    op.drop_table('meetings')
    op.drop_table('knowledge')
    op.drop_index(op.f('ix_journal_entries_entry_date'), table_name='journal_entries')
    op.drop_table('journal_entries')
    op.drop_index('ix_rag_chunks_terms', table_name='rag_chunks', postgresql_using='gin')
    op.drop_index('ix_rag_chunks_kind_row', table_name='rag_chunks')
    op.drop_table('rag_chunks')
    op.drop_index(op.f('ix_processed_updates_received_at'), table_name='processed_updates')
    op.drop_table('processed_updates')
    op.drop_table('plants')
    op.drop_table('members')
