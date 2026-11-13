"""Filler words of live farm questions, as search stop words.

Live 06-11 / 11-11 / 13-11-2026: "Je divase Kesar par copper chhantyu, e divase biju kai kharch
thayo?" searched the journal for "divase / biju / kharch / thayo", and "Mor kalo che, plan yaad
che. Khali dava kaho ne last kyare lidhi ti?" for "plan / yaad / khali / lidhi". None of these
say what a note is ABOUT, so a note was matched (or missed) for words it never needed. "banao" /
"banavo" ("make a plan") are the same kind of word.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WORDS = ["divase", "divse", "biju", "biji", "thayo", "thai", "kharch", "kharcho", "ketlo",
         "yaad", "khali", "lidhi", "lidhu", "plan", "banao", "banavo", "ti", "tu"]


def upgrade() -> None:
    conn = op.get_bind()
    for w in WORDS:
        conn.execute(sa.text("INSERT INTO search_stopwords (word) VALUES (:w) "
                             "ON CONFLICT DO NOTHING"), {"w": w})


def downgrade() -> None:
    conn = op.get_bind()
    for w in WORDS:
        conn.execute(sa.text("DELETE FROM search_stopwords WHERE word = :w"), {"w": w})
