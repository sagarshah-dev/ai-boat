"""Search stop words as data, and the Mango varieties as plant rows.

Live 05-11-2026: "Kesar par daag che, have shu karu?" returned 5 notes, among them a Dadam note.
Two causes, both data:
- "have" ("now") was not in the stop-word list that lived in code, so the keyword side matched
  the Dadam note's "have drip roj 30 min" and let it through the lower keyword gate. The list
  moves to a table; a new filler word is a row, not a deploy.
- "Kesar" was not a known plant name, so no plant filter was applied and every plant's notes
  were searched. Kesar is a VARIETY of Mango, so it gets its own row (Mango / Kesar), the shape
  resolve_plants already understands; count 0 keeps the Mango total at the owners' 150.

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The old in-code list (rag_index.STOP), unchanged, plus the filler words of live questions.
STOPWORDS = [
    "ma", "na", "ni", "nu", "no", "ne", "thi", "to", "ke", "k", "ane", "che", "chhe", "hatu",
    "hati", "hata", "shu", "su", "kyare", "kem", "ketlu", "ketla", "kevi", "rite", "karyu",
    "karyo", "kari", "karvu", "karvanu", "pehla", "pela", "aavi", "aavyu", "aavya", "tyare",
    "jyare", "mate", "par", "pan", "e", "a", "the", "is", "was", "what", "when", "how",
    "did", "we", "do", "for", "of", "in", "on", "and", "or", "it", "last", "time",
    "please", "batavo", "kaho", "aapo",
    # 05-11: "have shu karu?", "ketlu karwanu"
    "have", "hve", "karu", "karwanu", "karwu", "hamna", "hmna", "atyare", "atyre",
]

# (plant, variety, aliases) — the varieties the owner listed on 05-11-2026.
VARIETIES = [
    ("Mango", "Kesar", ["kesar", "કેસર"]),
    ("Mango", "Rajapuri", ["rajapuri", "રાજાપુરી"]),
    ("Mango", "Badam", ["badam", "બદામ"]),
    ("Mango", "Totapuri", ["tota", "totapuri", "તોતા", "તોતાપુરી"]),
    ("Mango", "Noorjahan", ["noorjahan", "nurjahan", "nurzahan", "નૂરજહાં"]),
    ("Mango", "Miyazaki", ["miyazaki", "મિયાઝાકી"]),
    ("Mango", "Sonpari", ["sonpari", "સોનપરી"]),
]


def upgrade() -> None:
    op.create_table("search_stopwords", sa.Column("word", sa.Text(), primary_key=True))
    conn = op.get_bind()
    for w in STOPWORDS:
        conn.execute(sa.text("INSERT INTO search_stopwords (word) VALUES (:w) "
                             "ON CONFLICT DO NOTHING"), {"w": w})
    for plant, variety, aliases in VARIETIES:
        conn.execute(sa.text(
            "INSERT INTO plants (plant, variety, aliases, count, count_date) "
            "VALUES (:p, :v, :a, 0, NULL) ON CONFLICT (plant, variety) DO NOTHING"),
            {"p": plant, "v": variety, "a": aliases})


def downgrade() -> None:
    conn = op.get_bind()
    for plant, variety, _ in VARIETIES:
        conn.execute(sa.text("DELETE FROM plants WHERE plant = :p AND variety = :v AND count = 0"),
                     {"p": plant, "v": variety})
    op.drop_table("search_stopwords")
