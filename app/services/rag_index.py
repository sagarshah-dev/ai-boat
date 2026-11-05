"""The search copy of the farm journal and the farmer's advice (table rag_chunks, pgvector).

    note saved -> translate once (stored, editable) -> fields + note + English -> embed -> upsert

The tables are the record; rag_chunks is a copy that can always be rebuilt from them. So an
indexing failure never fails a save: the row keeps indexed_at = NULL and the daily sync picks it
up. Ported from the old bot's rag.py (sync, index_rows, translate) and rag_store.py (PgStore).

Content hashes decide what is re-embedded: an unchanged note is never sent to the embedding
model twice (free-tier quota), and a note whose text changed is re-embedded on the next sync.
"""
import logging
import re
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.text import strip_marks
from app.db.models import JournalEntry, Knowledge, RagChunk, SearchStopword
from app.integrations.gemini import LLM
from app.services import farm
from app.services.farm import Doc

log = logging.getLogger(__name__)

# Notes are romanised Gujarati ("udhai", "fodla"); embedding models handle that poorly (spellings
# vary, little training data). Each note is translated ONCE, stored, and embedded together with
# the original, so a question in any language meets the note in one language.
TRANSLATE_RULES = (
    "Translate this farm journal entry into plain English. It may be romanized Gujarati, Gujarati "
    "script, Hindi, English, or a mix. Translate everything: pests, diseases, products, plant parts, "
    "actions, people, places. Keep every number and unit exactly as written. Do not add, explain or "
    "summarise. Return only the English translation, one line."
)


def clean_translation(raw: str) -> str:
    t = " ".join(str(raw or "").replace("```", " ").split()).strip().strip('"')
    return t[:600]


def _translate(llm: LLM, text: str) -> str:
    return clean_translation(llm.complete(TRANSLATE_RULES, text, temperature=0, budget_s=20))


def translate_journal(session: Session, llm: LLM, row_ids: set[int] | None = None) -> int:
    """Fill note_english where empty. A failure leaves it empty (indexed with the original
    words only) and the next sync tries again."""
    q = select(JournalEntry).where(JournalEntry.note_english == "")
    if row_ids is not None:
        q = q.where(JournalEntry.id.in_(row_ids))
    done = 0
    for e in session.scalars(q):
        source = " · ".join(f"{k}: {v.strip()}" for k, v in (
            ("Problem", e.problem), ("Product", e.product), ("Note", e.note)) if v.strip())
        if not source:
            e.note_english = farm.NO_ENGLISH
            continue
        try:
            e.note_english = _translate(llm, source) or farm.NO_ENGLISH
            done += 1
        except Exception:  # noqa: BLE001 — retried by the next sync
            log.warning("translation failed", extra={"fields": {"row": f"J-{e.id}"}})
    session.flush()
    return done


def translate_knowledge(session: Session, llm: LLM, row_ids: set[int] | None = None) -> int:
    """Short advice only (one search piece). Long voice transcripts are left as they are: their
    pieces would not line up with one translation."""
    q = select(Knowledge).where(Knowledge.text_english == "")
    if row_ids is not None:
        q = q.where(Knowledge.id.in_(row_ids))
    done = 0
    for k in session.scalars(q):
        if len(farm.chunk_text(k.text)) != 1:
            continue
        try:
            k.text_english = _translate(llm, k.text) or farm.NO_ENGLISH
            done += 1
        except Exception:  # noqa: BLE001
            log.warning("translation failed", extra={"fields": {"row": f"K-{k.id}"}})
    session.flush()
    return done


# ── Store ───────────────────────────────────────────────────────────────────────────────────

# Words that appear in almost every question carry no signal ("have shu karu?"). They live in
# the search_stopwords table (seeded by migration 0004). Live 05-11: "have" was not in the old
# in-code list, so "Kesar par daag che, have shu karu?" matched a Dadam note that says "have drip".
STOP_TTL_S = 300
_stop_cache: dict = {"words": frozenset(), "at": None}
_TOKEN_RE = re.compile(r"\d+(?::\d+)+|[^\W_]+")


def stopwords(session: Session) -> frozenset[str]:
    """Read once per few minutes, not per question: the list changes rarely."""
    now = time.monotonic()
    if _stop_cache["at"] is None or now - _stop_cache["at"] > STOP_TTL_S:
        _stop_cache["words"] = frozenset(
            w.lower() for w in session.scalars(select(SearchStopword.word)))
        _stop_cache["at"] = now
    return _stop_cache["words"]


def reset_stopwords_cache() -> None:
    _stop_cache.update(words=frozenset(), at=None)


def keyword_terms(text: str, stop: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Search terms for the keyword side. NPK grades like 0:52:34 stay ONE term: split into
    0, 52, 34 they would also match 13:0:45 — exactly the confusion keyword search prevents."""
    terms: list[str] = []
    for t in _TOKEN_RE.findall(strip_marks(text).lower()):
        if t in stop or (len(t) < 2 and not t.isdigit()):
            continue
        if t not in terms:
            terms.append(t)
    return terms[:12]


def doc_terms(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(strip_marks(text).lower()))


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def hashes(session: Session, model: str) -> dict[str, str | None]:
    """A chunk embedded by a different model reports no hash, so it is re-embedded: vectors
    from two models are not comparable, and mixing them makes search quietly wrong."""
    rows = session.execute(select(RagChunk.id, RagChunk.hash, RagChunk.model)).all()
    return {i: (h if m == model else None) for i, h, m in rows}


def upsert(session: Session, docs: list[Doc], vectors: list[list[float]], model: str) -> None:
    for d, v in zip(docs, vectors, strict=True):
        m = d.meta
        values = {"id": d.id, "row_id": d.row_id, "kind": d.kind, "text": d.text, "hash": d.hash,
                  "model": model, "plants": m.get("plants") or [], "variety": m.get("variety") or "",
                  "location": m.get("location") or "", "activity": m.get("activity") or "",
                  "day": m.get("date"), "meta": m, "terms": sorted(doc_terms(d.text)),
                  "embedding": v}
        stmt = pg_insert(RagChunk).values(**values).on_conflict_do_update(
            index_elements=["id"], set_={k: values[k] for k in values if k != "id"}
            | {"updated_at": datetime.now(timezone.utc)})
        session.execute(stmt)


def remove(session: Session, ids: set[str]) -> None:
    if ids:
        session.execute(delete(RagChunk).where(RagChunk.id.in_(ids)))


def _mark_indexed(session: Session, docs: list[Doc]) -> None:
    now = datetime.now(timezone.utc)
    for kind, table in (("journal", JournalEntry), ("knowledge", Knowledge)):
        ids = {d.row_id for d in docs if d.kind == kind}
        if ids:
            session.execute(update(table).where(table.id.in_(ids)).values(indexed_at=now))


# ── Keeping the copy in step ────────────────────────────────────────────────────────────────

def sync(session: Session, llm: LLM) -> dict:
    """Make rag_chunks match the tables: embed new/changed pieces, delete removed ones."""
    translate_journal(session, llm)
    translate_knowledge(session, llm)
    docs = farm.documents(session)
    existing = hashes(session, llm.embed_model)
    changed = [d for d in docs if existing.get(d.id) != d.hash]
    removed = set(existing) - {d.id for d in docs}
    if changed:
        upsert(session, changed, llm.embed([d.text for d in changed]), llm.embed_model)
    remove(session, removed)
    _mark_indexed(session, changed)
    return {"embedded": len(changed), "deleted": len(removed), "total": len(docs)}


def index_rows(session: Session, llm: LLM, kind: str, row_ids: set[int]) -> bool:
    """Index rows right after they are saved. Never raises and never undoes the save: it runs
    in its own savepoint, and on failure the row stays un-indexed for the daily sync."""
    nested = session.begin_nested()
    try:
        if kind == "journal":
            translate_journal(session, llm, row_ids)
        else:
            translate_knowledge(session, llm, row_ids)
        docs = farm.documents(session, kind=kind, row_ids=row_ids)
        stale = {i for i, in session.execute(select(RagChunk.id).where(
            RagChunk.kind == kind, RagChunk.row_id.in_(row_ids)))} - {d.id for d in docs}
        if docs:
            upsert(session, docs, llm.embed([d.text for d in docs]), llm.embed_model)
        remove(session, stale)
        _mark_indexed(session, docs)
        nested.commit()
        return bool(docs)
    except Exception:  # noqa: BLE001 — the note is saved; only the search copy is late
        nested.rollback()
        log.warning("indexing failed; left for the daily sync",
                    extra={"fields": {"kind": kind, "rows": sorted(row_ids)}})
        return False
