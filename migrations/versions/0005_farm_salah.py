"""Farm Salah: a "planned" journal activity, and LangGraph's checkpoint tables.

- journal_entries.activity may be 'planned': a plan saved after ✅, not done yet.
- checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations: where a paused
  graph keeps its state between messages (up to 24 hours, through Cloud Run sleeping).

The checkpoint tables are created from langgraph-checkpoint-postgres's own SQL (so their shape is
exactly what the library expects), inside this migration instead of PostgresSaver.setup() at
start-up: schema changes in this project happen in migrations only. Its "CREATE INDEX
CONCURRENTLY" cannot run inside a migration's transaction; on new, empty tables a plain CREATE
INDEX is the same thing. The library's version table is filled in, so a later setup() call
finds nothing to do.

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = ("plantation", "fertilizer", "spray", "treatment", "irrigation", "pruning", "observation",
       "harvest", "dead", "replant", "other")
NEW = OLD + ("planned",)
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations")


def _activity_check(values: tuple[str, ...]) -> str:
    return f"activity IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_journal_entries_activity"), "journal_entries", type_="check")
    op.create_check_constraint(op.f("ck_journal_entries_activity"), "journal_entries",
                               _activity_check(NEW))

    from langgraph.checkpoint.postgres.base import BasePostgresSaver
    for v, sql in enumerate(BasePostgresSaver.MIGRATIONS):
        op.execute(sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX"))
        if v > 0:      # statement 0 creates the version table itself
            op.execute(f"INSERT INTO checkpoint_migrations (v) VALUES ({v}) ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO checkpoint_migrations (v) VALUES (0) ON CONFLICT DO NOTHING")


def downgrade() -> None:
    for t in CHECKPOINT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t}")
    op.execute("DELETE FROM journal_entries WHERE activity = 'planned'")
    op.drop_constraint(op.f("ck_journal_entries_activity"), "journal_entries", type_="check")
    op.create_check_constraint(op.f("ck_journal_entries_activity"), "journal_entries",
                               _activity_check(OLD))
