"""The three co-owners are fixed payers, with the names people actually type.

Live bug (28-10-2026): "150 tea ... all by lalo" was saved as paid by Sagar. AI BOaT started
blank, so Lalo and Biren were not members yet; the payer list had only Sagar, and the agent fell
back to the sender. The owners are a fixed set (Sagar, Lalo, Biren), so they are reference data,
seeded like the plants list — not something each of them has to /start and wait for.

- telegram_user_id becomes optional: a payer can exist before (or without) ever messaging the
  bot. Access is unchanged: get_active() looks people up BY Telegram ID, so a row without one can
  pay a share but can never use the bot. When that person does /start and the admin approves
  them as Owner, the request is linked to this row (members.decide), not added as a 4th owner.
- aliases: "jignesh" is Lalo, "bako" is Biren. Same idea as plants.aliases.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (name, aliases). Sagar is the admin row created by /start; only his aliases are set.
OWNERS = [
    ("Lalo", ["lalo", "jignesh", "લાલો", "જીગ્નેશ"]),
    ("Biren", ["biren", "bako", "બિરેન", "બકો"]),
]
ADMIN_ALIASES = ["sagar", "સાગર"]


def upgrade() -> None:
    op.alter_column("members", "telegram_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("members", sa.Column("aliases", postgresql.ARRAY(sa.Text()),
                                       server_default="{}", nullable=False))
    conn = op.get_bind()
    # The admin row's name came from Telegram as "sagar"; replies read "paid by sagar".
    conn.execute(sa.text("UPDATE members SET aliases = :a, "
                         "name = CASE WHEN name = lower(name) THEN initcap(name) ELSE name END "
                         "WHERE role = 'admin' AND lower(split_part(name, ' ', 1)) = 'sagar'"),
                 {"a": ADMIN_ALIASES})
    for name, aliases in OWNERS:
        # Someone may already have joined under this name: give that row the aliases instead of
        # creating a second one (two "Lalo" owners would split every expense four ways).
        found = conn.execute(sa.text(
            "UPDATE members SET aliases = :a "
            "WHERE lower(split_part(name, ' ', 1)) = ANY(:a) AND role = 'owner' "
            "AND status = 'active' RETURNING id"), {"a": aliases}).fetchall()
        if not found:
            conn.execute(sa.text(
                "INSERT INTO members (telegram_user_id, name, role, status, aliases, joined_at) "
                "VALUES (NULL, :n, 'owner', 'active', :a, now())"), {"n": name, "a": aliases})


def downgrade() -> None:
    # Rows that never got a Telegram ID can't survive NOT NULL; remove them only if unused.
    op.execute("DELETE FROM members m WHERE m.telegram_user_id IS NULL AND NOT EXISTS "
               "(SELECT 1 FROM expenses e WHERE e.paid_by = m.id OR e.entered_by = m.id)")
    op.drop_column("members", "aliases")
    op.alter_column("members", "telegram_user_id", existing_type=sa.BigInteger(), nullable=False)
