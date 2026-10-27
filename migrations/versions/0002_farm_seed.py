"""Farm journal: the plants list the owners gave on 19-10-2026, and the "skipped" follow-up.

Plants are reference data the bot cannot work without (a note must name a plant), so they are
seeded by a migration: every fresh database — test, CI, production — starts with the same list.
After that the table is the source of truth; a new plant is a row, not a deploy.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same rows as the old bot's SEED_PLANTS. Not "aam": in Gujarati it also means "like this".
PLANTS = [
    ("Mango", "mango,mangoes,aamba,amba,ambo,aambo,keri,kairi,કેરી,આંબો,આંબા", 150),
    ("Coconut", "coconut,coconuts,nariyel,nariyal,naliyer,naliyeri,nadiyer,નાળિયેર,નારિયેળ,નાળીયેર", 20),
    ("Guava", "guava,guavas,jamfal,jamphal,jamrukh,peru,amrud,જામફળ", 38),
    ("Chiku", "chiku,chikoo,cheeku,chikku,sapota,sapodilla,ચીકુ", 8),
    ("Lemon", "lemon,lemons,limbu,limboo,limdu,nimbu,lime,લીંબુ", 40),
]


def upgrade() -> None:
    op.drop_constraint(op.f("ck_journal_entries_follow_up"), "journal_entries", type_="check")
    op.create_check_constraint(op.f("ck_journal_entries_follow_up"), "journal_entries",
                               "follow_up IN ('', 'sent', 'done', 'skipped')")
    plants = sa.table("plants", sa.column("plant", sa.Text), sa.column("variety", sa.Text),
                      sa.column("aliases", postgresql.ARRAY(sa.Text)),
                      sa.column("count", sa.Integer), sa.column("count_date", sa.Date))
    from datetime import date
    op.bulk_insert(plants, [{"plant": name, "variety": "", "aliases": aliases.split(","),
                             "count": count, "count_date": date(2026, 10, 19)}
                            for name, aliases, count in PLANTS])


def downgrade() -> None:
    op.execute("DELETE FROM plants")
    op.execute("UPDATE journal_entries SET follow_up = '' WHERE follow_up = 'skipped'")
    op.drop_constraint(op.f("ck_journal_entries_follow_up"), "journal_entries", type_="check")
    op.create_check_constraint(op.f("ck_journal_entries_follow_up"), "journal_entries",
                               "follow_up IN ('', 'sent', 'done')")
