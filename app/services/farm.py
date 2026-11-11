"""Farm journal data: plants and the words people use for them, journal notes, treatment results,
the farmer's advice. Ported from the old bot's farm.py (Journal / Plants / Knowledge tabs).

No model calls here. Every function takes plain values and returns rows or plain data; the AI
layer decides when to call them, and only after its gates have checked the model's arguments.

Plant names are DATA, not code: the plants table holds each plant and its aliases ("aamba",
"keri", "કેરી"). Adding a plant is a row, not a deploy — which is also why the tool schema has no
plant enum: the model proposes, and resolve_plants() decides from what the user actually wrote.
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.formatting import fmt_date
from app.core.text import norm, strip_marks, words
from app.db.models import ACTIVITIES, JournalEntry, Knowledge, Member, Plant, RagChunk

TREATMENTS = {"spray", "fertilizer", "treatment"}          # dose question + 7-day follow-up
COUNTED = {"plantation": +1, "replant": +1, "dead": -1}    # change the live plant count
ALL = "All"
OWN_NOTE_HOURS = 48       # "undo farm note" reaches back this far, own notes only

ACTIVITY_LABEL = {"plantation": "Plantation", "fertilizer": "Fertilizer", "spray": "Spray",
                  "treatment": "Treatment", "irrigation": "Watering", "pruning": "Pruning",
                  "observation": "Observation", "harvest": "Harvest", "dead": "Plants died",
                  "replant": "Replanted", "other": "Other", "planned": "Planned (not done yet)"}
OUTCOME_LABEL = {"worked": "✅ Worked", "partly": "➖ Partly", "not_worked": "❌ Did not work"}

# "badha aamba" = every mango; "badha chhod" = every plant on the farm.
_ALL_WORDS = {strip_marks(w).lower() for w in (
    "badha", "badhe", "badhi", "badhu", "badhaj", "all", "sab", "sabhi", "saglu", "sagla",
    "every", "બધા", "બધે", "બધી", "સબ")}
_PLANT_GENERIC = {strip_marks(w).lower() for w in (
    "chhod", "chod", "chhodva", "chodva", "zad", "jhad", "plant", "plants", "tree", "trees", "ped",
    "chhodo", "chodo", "છોડ", "ઝાડ", "વૃક્ષ", "farm", "khetar", "વાડી", "wadi", "vadi")}
# Case endings glued to the word: "aambama", "limbuni". Only these, so "ambassador" never
# turns into a mango.
_ROMAN_SUFFIXES = ("mathi", "maa", "ma", "ne", "na", "ni", "nu", "no", "thi", "par", "o")
# Gujarati is matched on the ORIGINAL letters: with vowel signs stripped, કેરી (mango) and
# કરી ("did") are the same two letters.
_GU_WORD = re.compile(r"[઀-૿]+")


# ── Plants ──────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlantInfo:
    plant: str
    variety: str
    aliases: frozenset[str]          # normalised, for romanised / English matching
    raw_aliases: frozenset[str]      # Gujarati script, original letters
    count: int
    count_date: date | None


@dataclass(frozen=True)
class PlantMatch:
    plants: list[str]
    variety: str          # "Kesar", ALL ("badha aamba") or ""
    all_farm: bool        # "badha chhod": every plant on the farm


def plants(session: Session) -> list[PlantInfo]:
    out = []
    for p in session.scalars(select(Plant).order_by(Plant.id)):
        aliases = {norm(a) for a in p.aliases if norm(a)}
        aliases.add(norm(p.variety) if p.variety else norm(p.plant))
        raw = {a.strip() for a in p.aliases if a.strip() and not a.strip().isascii()}
        out.append(PlantInfo(p.plant, p.variety, frozenset(aliases), frozenset(raw), p.count,
                             p.count_date))
    return out


def plant_names(known: list[PlantInfo]) -> list[str]:
    seen: list[str] = []
    for p in known:
        if p.plant not in seen:
            seen.append(p.plant)
    return seen


def _suffixed(alias: str, toks: list[str]) -> bool:
    if " " in alias or len(alias) < 3 or not alias.isascii():
        return False
    return any(t.startswith(alias) and t[len(alias):] in _ROMAN_SUFFIXES for t in toks)


def _gu_suffixed(alias: str, raw_toks: list[str]) -> bool:
    return any(t == alias or (t.startswith(alias) and len(t) - len(alias) <= 5) for t in raw_toks)


def resolve_plants(known: list[PlantInfo], text: str) -> PlantMatch | None:
    """What the USER named — never what the model guessed. None when no plant is named at all
    (the caller then asks, with buttons).
        "badha aamba" -> Mango, variety All      "badha chhod"     -> all_farm
        "kesar aamba" -> Mango, variety Kesar    "aamba ane chiku" -> Mango + Chiku"""
    toks = words(text)
    joined = " " + " ".join(toks) + " "
    raw_toks = _GU_WORD.findall(str(text or ""))
    found: list[str] = []
    variety = ""
    for p in known:
        hit = any(_gu_suffixed(a, raw_toks) for a in p.raw_aliases)
        for alias in sorted(p.aliases, key=len, reverse=True):
            if hit or (alias.isascii() and (f" {alias} " in joined or _suffixed(alias, toks))):
                if p.plant not in found:
                    found.append(p.plant)
                if p.variety:
                    variety = p.variety
                break
    has_all = any(t in _ALL_WORDS for t in toks)
    if found:
        v = variety if len(found) == 1 else ""
        if has_all and not v:
            v = ALL
        return PlantMatch(found, v, False)
    generic = any(t in _PLANT_GENERIC or any(t.startswith(g) and len(g) >= 2 and len(t) - len(g) <= 3
                                             for g in _PLANT_GENERIC) for t in toks)
    if has_all and (generic or len(toks) <= 6):
        return PlantMatch([ALL], "", True)
    return None


def add_plant(session: Session, name: str, on: date) -> Plant:
    """A new plant is a row, not a deploy. Its own name is its first alias, so the next note
    that says it finds it."""
    p = Plant(plant=name.strip().title(), variety="", aliases=[name.strip().lower()], count=0,
              count_date=on)
    session.add(p)
    session.flush()
    return p


def single_plant(match: PlantMatch | None) -> str | None:
    """The one plant a question is about, or None (none named, several, or the whole farm)."""
    if match and not match.all_farm and len(match.plants) == 1:
        return match.plants[0]
    return None


def plant_label(names: list[str], variety: str = "") -> str:
    if names == [ALL]:
        return "All plants"
    base = ", ".join(names)
    if variety == ALL:
        return f"{base} (all)"
    return f"{base} ({variety})" if variety else base


# ── Journal ─────────────────────────────────────────────────────────────────────────────────

def plants_of(e: JournalEntry) -> list[str]:
    return [p.strip() for p in e.plant.split(",") if p.strip()]


def journal_line(e: JournalEntry, *, with_result: bool = True) -> str:
    """One note as one readable line."""
    when = fmt_date(e.entry_date) + (f"–{fmt_date(e.date_to)}" if e.date_to else "")
    bits = [when, plant_label(plants_of(e), e.variety), ACTIVITY_LABEL.get(e.activity, e.activity)]
    bits += [v for v in (e.product, e.dose, e.problem) if v]
    if e.plant_qty is not None:
        bits.append(f"{e.plant_qty} plants")
    line = " · ".join(bits) + f" (J-{e.id})"
    if with_result and e.result:
        line += f"\n   ↳ {e.result}"
    return line


def add_entry(session: Session, p: dict, added_by: Member) -> JournalEntry:
    """`p` is an already-checked proposal. Indexed later (indexed_at stays NULL until then)."""
    e = JournalEntry(
        entry_date=date.fromisoformat(p["date"]),
        date_to=date.fromisoformat(p["date_to"]) if p.get("date_to") else None,
        plant=", ".join(p["plants"]), variety=p.get("variety", ""), location=p.get("location", ""),
        plant_qty=p.get("qty"), activity=p["activity"], product=p.get("product", ""),
        dose=p.get("dose", ""), time_of_day=p.get("time_of_day", ""), problem=p.get("problem", ""),
        note=p.get("note", ""), photo=p.get("photo", ""), source=p.get("source", "text"),
        added_by=added_by.id)
    session.add(e)
    session.flush()
    return e


def get_entry(session: Session, jid: int) -> JournalEntry | None:
    return session.get(JournalEntry, jid)


def last_entry_by(session: Session, member: Member, now: datetime | None = None) -> JournalEntry | None:
    """The sender's own newest note from the last 48 hours — what "undo farm note" removes.
    Own notes only, recent only: the model can never pick which row is deleted."""
    now = now or datetime.now(timezone.utc)
    return session.scalar(select(JournalEntry).where(
        JournalEntry.added_by == member.id,
        JournalEntry.added_at >= now - timedelta(hours=OWN_NOTE_HOURS))
        .order_by(JournalEntry.id.desc()).limit(1))


def delete_entry(session: Session, jid: int) -> bool:
    e = session.get(JournalEntry, jid)
    if e is None:
        return False
    session.delete(e)
    # The search copy goes with it, in the same transaction: a deleted note must never be
    # found by a search afterwards.
    session.execute(delete(RagChunk).where(RagChunk.kind == "journal", RagChunk.row_id == jid))
    session.flush()
    return True


def set_result(session: Session, jid: int, text: str, day: date) -> JournalEntry | None:
    """What happened after a treatment. The note's search text changes (the result is what makes
    'what worked last time?' answerable), so it is marked for re-indexing."""
    e = session.get(JournalEntry, jid)
    if e is None:
        return None
    e.result, e.result_date, e.follow_up, e.indexed_at = text, day, "done", None
    session.flush()
    return e


def matches_plant(e: JournalEntry, plant: str | None, variety: str | None = None) -> bool:
    """'All plants' notes match every plant; 'Mango (All)' notes match every mango variety."""
    if not plant:
        return True
    ps = [p.lower() for p in plants_of(e)]
    if ALL.lower() in ps:
        return True
    if plant.lower() not in ps:
        return False
    if variety and variety != ALL:
        v = e.variety.strip()
        return v in ("", ALL) or v.lower() == variety.lower()
    return True


def mentions(e: JournalEntry, word: str) -> bool:
    """The note names this word in Product, Problem or Note (normalised; a case ending like
    'udhaine' still counts). Problem matters: 'udhai' is often written only there."""
    w = norm(word)
    return bool(w) and w in f"{norm(e.product)} {norm(e.problem)} {norm(e.note)}"


def mentions_in_english(e: JournalEntry, word: str) -> bool:
    english = e.note_english.strip()
    w = norm(word)
    return bool(english) and english != NO_ENGLISH and bool(w) and w in norm(english)


def query(session: Session, *, plant: str | None = None, variety: str | None = None,
          activity: str | None = None, date_from: date | None = None,
          date_to: date | None = None) -> list[JournalEntry]:
    """Exact filter, newest first. Code filters, never the model."""
    q = select(JournalEntry)
    if activity:
        q = q.where(JournalEntry.activity == activity)
    if date_from:
        q = q.where(JournalEntry.entry_date >= date_from)
    if date_to:
        q = q.where(JournalEntry.entry_date <= date_to)
    rows = session.scalars(q.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc()))
    return [e for e in rows if matches_plant(e, plant, variety)]


def open_treatments(session: Session, today: date, plant: str | None = None,
                    days: int = 90) -> list[JournalEntry]:
    """Treatments with no result yet — what a result message ('copper pachhi daag band')
    belongs to."""
    return [e for e in query(session, plant=plant, date_from=today - timedelta(days=days), date_to=today)
            if e.activity in TREATMENTS and not e.result]


def followups_due(session: Session, today: date, days: int = 7) -> list[JournalEntry]:
    """Treatments done 7–30 days ago, no result, not asked yet."""
    rows = session.scalars(select(JournalEntry).where(
        JournalEntry.activity.in_(TREATMENTS), JournalEntry.result == "",
        JournalEntry.follow_up == "",
        JournalEntry.entry_date <= today - timedelta(days=days),
        JournalEntry.entry_date >= today - timedelta(days=30)).order_by(JournalEntry.id))
    return list(rows)


PLAN_REMINDER_DAYS = 2      # owner's choice (11-11-2026): ask 2 days after a plan was saved


def plans_due(session: Session, today: date, days: int = PLAN_REMINDER_DAYS) -> list[JournalEntry]:
    """Farm Salah plans saved `days`+ days ago (at most 30), not asked about yet."""
    rows = session.scalars(select(JournalEntry).where(
        JournalEntry.activity == "planned", JournalEntry.follow_up == "",
        JournalEntry.entry_date <= today - timedelta(days=days),
        JournalEntry.entry_date >= today - timedelta(days=30)).order_by(JournalEntry.id))
    return list(rows)


def plan_done(session: Session, jid: int, day: date) -> JournalEntry | None:
    """The plan was carried out: it becomes a spray ON THAT DAY, with no follow-up yet — so the
    normal 7-day "how is it now?" question asks for its result, like any spray."""
    e = session.get(JournalEntry, jid)
    if e is None or e.activity != "planned":
        return None
    e.activity, e.entry_date, e.follow_up, e.indexed_at = "spray", day, "", None
    e.note = (e.note + f" · Done on {fmt_date(day)}").strip(" ·")
    session.flush()
    return e


def mark_followup(session: Session, jid: int, state: str) -> None:
    e = session.get(JournalEntry, jid)
    if e is not None:
        e.follow_up = state


@dataclass(frozen=True)
class Count:
    plant: str
    baseline: int
    added: int
    dead: int
    unknown_qty: int

    @property
    def alive(self) -> int:
        return self.baseline + self.added - self.dead


def live_counts(session: Session) -> list[Count]:
    """Alive = baseline + planted − died, counting only events AFTER the baseline date (older
    ones are already inside the baseline). Computed every time, never stored: a stored total
    drifts from the events the day someone edits one."""
    base: dict[str, dict] = {}
    for p in plants(session):
        b = base.setdefault(p.plant, {"count": 0, "date": p.count_date})
        b["count"] += p.count
    changes = {k: {"added": 0, "dead": 0, "unknown": 0} for k in base}
    events = session.scalars(select(JournalEntry).where(JournalEntry.activity.in_(COUNTED)))
    for e in events:
        sign = COUNTED[e.activity]
        for name in plants_of(e):
            if name not in base:
                continue
            since = base[name]["date"]
            if since and e.entry_date <= since:
                continue
            if e.plant_qty is None:
                changes[name]["unknown"] += 1
            else:
                changes[name]["added" if sign > 0 else "dead"] += e.plant_qty
    return [Count(name, base[name]["count"], c["added"], c["dead"], c["unknown"])
            for name, c in changes.items()]


# ── Farmer's advice ─────────────────────────────────────────────────────────────────────────

ADVICE_MIN_CHARS = 20


def add_knowledge(session: Session, title: str, text: str, member: Member, source: str,
                  day: date) -> Knowledge:
    k = Knowledge(entry_date=day, title=title, text=text, source=source, added_by=member.id)
    session.add(k)
    session.flush()
    return k


_SENT_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")


def chunk_text(text: str, target_words: int = 90, max_words: int = 140) -> list[str]:
    """Sentence-aligned pieces of ~90 words, each carrying the previous sentence as overlap so
    'pachhi athvadiye be vaar' keeps the sentence that says AFTER WHAT. Deterministic: the same
    text always gives the same pieces, so an unchanged note is never re-embedded."""
    text = str(text or "")
    sents = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    if len(sents) <= 1 and len(text.split()) > max_words:
        # Voice transcripts often have no punctuation at all: fixed word windows instead.
        w = text.split()
        step = target_words - 15
        return [" ".join(w[i:i + target_words]) for i in range(0, len(w), step) if w[i:i + target_words]]
    chunks: list[str] = []
    cur: list[str] = []
    prev_last = None
    for s in sents:
        cur.append(s)
        if sum(len(x.split()) for x in cur) >= target_words:
            chunks.append(" ".join(cur))
            prev_last = cur[-1]
            cur = [prev_last] if len(prev_last.split()) < 40 else []
    if cur and (not chunks or cur != [prev_last]):
        chunks.append(" ".join(cur))
    return chunks or ([text.strip()] if text.strip() else [])


# ── What gets embedded ──────────────────────────────────────────────────────────────────────

# Searchable text = the user's own fields + " ⟦English⟧ " + an English translation. The
# translation is there ONLY so a question in any language finds the note; every answer and every
# number check uses the part before the mark. See rag.original_text.
EN_MARK = " ⟦English⟧ "
NO_ENGLISH = "-"     # "nothing to translate", so the sync doesn't ask again


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Doc:
    id: str           # J-12 or K-3#1
    row_id: int
    kind: str         # journal | knowledge
    text: str
    meta: dict

    @property
    def hash(self) -> str:
        return content_hash(self.text)


def journal_doc_text(e: JournalEntry) -> str:
    """Validated fields + the raw note. The note alone ('pan par fodla, neem spray') does not
    say 'mango', so a question about mango would miss it; the plant field does."""
    parts = [fmt_date(e.entry_date) + (f" to {fmt_date(e.date_to)}" if e.date_to else ""),
             plant_label(plants_of(e), e.variety), e.activity]
    for label, v in (("Location", e.location), ("Product", e.product), ("Dose", e.dose),
                     ("Time", e.time_of_day), ("Problem", e.problem), ("Result", e.result)):
        if v.strip():
            parts.append(f"{label}: {v.strip()}")
    if e.plant_qty is not None:
        parts.append(f"Plants: {e.plant_qty}")
    if e.note.strip():
        parts.append(f"Note: {e.note.strip()}")
    text = " · ".join(p for p in parts if p)
    english = e.note_english.strip()
    if english and english != NO_ENGLISH:
        text += EN_MARK + english
    return text


def journal_doc(e: JournalEntry, added_by: str) -> Doc:
    return Doc(f"J-{e.id}", e.id, "journal", journal_doc_text(e), {
        "plants": plants_of(e), "variety": e.variety, "location": e.location,
        "activity": e.activity, "date": e.entry_date.isoformat(), "date_str": fmt_date(e.entry_date),
        "added_by": added_by, "photo": e.photo, "result": e.result})


def knowledge_docs(k: Knowledge, added_by: str) -> list[Doc]:
    pieces = chunk_text(k.text)
    english = k.text_english.strip()
    docs = []
    for n, piece in enumerate(pieces, start=1):
        text = f"Farmer advice — {k.title}: {piece}"
        # Short advice (one piece) carries its translation like a journal note; long transcripts
        # are split into pieces and not translated.
        if len(pieces) == 1 and english and english != NO_ENGLISH:
            text += EN_MARK + english
        docs.append(Doc(f"K-{k.id}#{n}", k.id, "knowledge", text, {
            "plants": [], "variety": "", "location": "", "activity": "",
            "date": k.entry_date.isoformat(), "date_str": fmt_date(k.entry_date),
            "added_by": added_by, "photo": "", "title": k.title, "result": ""}))
    return docs


def documents(session: Session, *, kind: str | None = None,
              row_ids: set[int] | None = None) -> list[Doc]:
    """Every searchable piece, built from the tables. The ONLY definition of the search copy;
    the index sync compares it with rag_chunks."""
    names = dict(session.execute(select(Member.id, Member.name)).all())
    docs: list[Doc] = []
    if kind in (None, "journal"):
        q = select(JournalEntry)
        if row_ids is not None:
            q = q.where(JournalEntry.id.in_(row_ids))
        docs += [journal_doc(e, names.get(e.added_by, "")) for e in session.scalars(q.order_by(JournalEntry.id))]
    if kind in (None, "knowledge"):
        q = select(Knowledge)
        if row_ids is not None:
            q = q.where(Knowledge.id.in_(row_ids))
        for k in session.scalars(q.order_by(Knowledge.id)):
            docs += knowledge_docs(k, names.get(k.added_by, ""))
    return docs


__all__ = ["ACTIVITIES"]
