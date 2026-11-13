"""Short-term memory: the last few exchanges per chat and member.

Live baseline 13-11-2026, 0 of 3: "E divase biju kai kharch thayo?" after "Chhelle copper kyare
chhantyu?", "Shu karu?" after "Kesar par mor kala che", and "Na, 150 hata" after a ₹100 tea — the
model saw only the new message. Rows older than the window are deleted when a new one is written
(app/services/memory.py), so the table stays a few rows per person without a scheduled job.

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.CheckConstraint("role IN ('user', 'bot')", name="ck_conversation_turns_role"),
    )
    op.create_index("ix_conversation_turns_chat_member_time", "conversation_turns",
                    ["chat_id", "member_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_conversation_turns_chat_member_time", table_name="conversation_turns")
    op.drop_table("conversation_turns")
