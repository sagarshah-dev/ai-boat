"""MCP server sign-in: OAuth clients, tokens and failed logins.

Claude's custom connector supports OAuth or no auth at all for a personal account (static
request headers are a beta for some organisations only, checked 16-11-2026). Money data needs
sign-in, so the service is its own OAuth server; its state lives here because Cloud Run
restarts would wipe anything kept in memory.

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.Text(), primary_key=True),
        sa.Column("info", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_table(
        "oauth_tokens",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(),
                  sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("data", JSONB(), server_default="{}", nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.CheckConstraint("kind IN ('request', 'code', 'access', 'refresh')",
                           name="ck_oauth_tokens_kind"),
    )
    op.create_index("ix_oauth_tokens_family", "oauth_tokens", ["family"])
    op.create_index("ix_oauth_tokens_expires_at", "oauth_tokens", ["expires_at"])
    op.create_table(
        "oauth_login_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_oauth_login_failures_created_at", "oauth_login_failures", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_oauth_login_failures_created_at", table_name="oauth_login_failures")
    op.drop_table("oauth_login_failures")
    op.drop_index("ix_oauth_tokens_expires_at", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_family", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_clients")
