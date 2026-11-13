"""Answers from the farm notes and the farmer's advice (retrieval-augmented generation).

    question -> filters (plant / dates / activity, decided in CODE) -> vector + keyword search
    -> score gate -> rank (RRF) -> de-duplicate -> model fills a fixed 3-field form
    -> code checks citations and numbers -> code writes the reply and the source line.

What the model is NOT allowed to do here: choose which notes exist (code filters), decide whether
anything was found (the score gate does), write a source line (code builds it from the rows), or
add a number that is not written in a note it cited (code rejects the answer).

Ported from the old bot's rag.py / rag_store.py, with one store (Postgres) instead of two.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.formatting import parse_date
from app.integrations.gemini import LLM, LlmError
from app.services import farm
from app.services.rag_index import catch_up, keyword_terms, stopwords, vec_literal

log = logging.getLogger(__name__)

KW_MARGIN = 0.08          # a keyword hit may pass slightly below the meaning threshold
MAX_HITS = 5
CANDIDATES = 20
RRF_K = 60
DUPLICATE_SIM = 0.96      # two notes this similar are one note twice: keep the better-ranked


@dataclass
class Filters:
    plant: str = ""
    variety: str = ""
    activity: str = ""
    date_from: date | None = None
    date_to: date | None = None
    journal_only: bool = False


@dataclass
class Hit:
    id: str
    kind: str
    text: str
    meta: dict
    sim: float
    vec: list[float] = field(repr=False, default_factory=list)
    kw: float = 0.0


@dataclass
class Found:
    hits: list[Hit]
    relaxed: str = ""          # what was loosened to find these ("" = the user's own filters)
    mode: str = "vector"       # vector | basic (embedding model unavailable)
    trace: dict = field(default_factory=dict)


def original_text(t: str) -> str:
    """The user's own words only — what the answering model reads and numbers are checked
    against. A mistranslated '75 ml' in the English copy can never reach an answer."""
    return str(t or "").split(farm.EN_MARK)[0]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))     # both are unit length


# ── Candidates from Postgres ────────────────────────────────────────────────────────────────

def _where(f: Filters) -> tuple[str, dict]:
    """Filters apply to journal notes only: farmer advice is general, not about one tree on one
    date, so it is never filtered out by plant or date."""
    conds, args = [], {}
    if f.plant:
        conds.append("('All' = ANY(plants) OR lower(:plant) = ANY(SELECT lower(x) FROM unnest(plants) x))")
        args["plant"] = f.plant
        if f.variety and f.variety != farm.ALL:
            conds.append("(lower(variety) IN ('', 'all') OR lower(variety) = lower(:variety))")
            args["variety"] = f.variety
    if f.date_from:
        conds.append("day >= :date_from")
        args["date_from"] = f.date_from
    if f.date_to:
        conds.append("day <= :date_to")
        args["date_to"] = f.date_to
    if f.activity:
        conds.append("activity = :activity")
        args["activity"] = f.activity
    journal = " AND ".join(["kind = 'journal'"] + conds)
    if f.journal_only:
        return f"({journal})", args
    return f"(kind = 'knowledge' OR ({journal}))", args


def _shape(row, kw: bool = False) -> Hit:
    meta = row.meta if isinstance(row.meta, dict) else json.loads(row.meta)
    vec = [float(x) for x in row.vec.strip("[]").split(",")] if row.vec else []
    return Hit(row.id, row.kind, row.text, meta, float(row.sim) if row.sim is not None else 0.0,
               vec, float(row.kw) if kw else 0.0)


def candidates(session: Session, qvec: list[float] | None, terms: list[str], f: Filters,
               limit: int = CANDIDATES) -> tuple[list[Hit], list[Hit]]:
    """(closest by meaning, best by shared words). qvec None = keyword side only."""
    where, args = _where(f)
    vec_hits: list[Hit] = []
    sim_sql = "NULL::float"
    if qvec is not None:
        args["q"] = vec_literal(qvec)
        sim_sql = "1 - (embedding <=> CAST(:q AS vector))"
        rows = session.execute(text(
            f"SELECT id, kind, text, meta, {sim_sql} AS sim, embedding::text AS vec "
            f"FROM rag_chunks WHERE {where} ORDER BY embedding <=> CAST(:q AS vector) LIMIT :lim"),
            {**args, "lim": limit})
        vec_hits = [_shape(r) for r in rows]
    kw_hits: list[Hit] = []
    if terms:
        rows = session.execute(text(
            f"SELECT id, kind, text, meta, {sim_sql} AS sim, embedding::text AS vec, "
            "cardinality(ARRAY(SELECT unnest(terms) INTERSECT SELECT unnest(CAST(:terms AS text[]))))"
            "::float / :nterms AS kw "
            f"FROM rag_chunks WHERE {where} AND terms && CAST(:terms AS text[]) "
            "ORDER BY kw DESC, id LIMIT :lim"),
            {**args, "terms": terms, "nterms": len(terms), "lim": limit})
        kw_hits = [_shape(r, kw=True) for r in rows]
    return vec_hits, kw_hits


# ── Ranking ─────────────────────────────────────────────────────────────────────────────────

def relaxations(f: Filters) -> list[tuple[Filters, str]]:
    """Filter sets to try, strictest first. Each step says what was loosened, because a relaxed
    answer may be about a different plant and the user must be told so."""
    steps = [(f, "")]
    cur = Filters(**vars(f))
    if cur.variety and cur.variety != farm.ALL:
        cur = Filters(**{**vars(cur), "variety": ""})
        steps.append((cur, f"no note for {f.variety}; showing all {f.plant} notes"))
    if cur.activity:
        cur = Filters(**{**vars(cur), "activity": ""})
        steps.append((cur, f"no {f.activity} note found; showing other activities"))
    if cur.date_from or cur.date_to:
        cur = Filters(**{**vars(cur), "date_from": None, "date_to": None})
        steps.append((cur, "nothing in that period; showing other dates"))
    if cur.plant:
        cur = Filters(**{**vars(cur), "plant": "", "variety": ""})
        steps.append((cur, f"no {f.plant} note found; showing other plants"))
    return steps


def rank(vec_hits: list[Hit], kw_hits: list[Hit], min_score: float) -> list[Hit]:
    """Gate, then fuse. A candidate survives only if it is close in meaning, or shares the exact
    words AND is nearly close enough. Survivors are ordered by Reciprocal Rank Fusion: being
    high in BOTH lists beats being top of one."""
    kw_ids = {h.id for h in kw_hits}
    passed = {}
    for h in vec_hits + kw_hits:
        if h.sim >= min_score or (h.id in kw_ids and h.sim >= min_score - KW_MARGIN):
            passed[h.id] = h
    scores: dict[str, float] = {}
    for lst in (vec_hits, kw_hits):
        for pos, h in enumerate(lst):
            if h.id in passed:
                scores[h.id] = scores.get(h.id, 0) + 1 / (RRF_K + pos + 1)
    ordered = sorted(passed.values(), key=lambda h: (-scores.get(h.id, 0), h.id))
    chosen: list[Hit] = []
    for h in ordered:
        # Ten "neem spray again" notes must not push the one copper note out of the top 5.
        if h.vec and any(c.vec and cosine(h.vec, c.vec) >= DUPLICATE_SIM for c in chosen):
            continue
        chosen.append(h)
        if len(chosen) >= MAX_HITS:
            break
    return chosen


def _merge(lists: list[list[Hit]], key: str) -> list[Hit]:
    """Union over several phrasings of one question; best score per note."""
    best: dict[str, Hit] = {}
    for lst in lists:
        for h in lst:
            prev = best.get(h.id)
            if prev is None:
                best[h.id] = h
            else:
                prev.sim = max(prev.sim, h.sim)
                prev.kw = max(prev.kw, h.kw)
    return sorted(best.values(), key=lambda h: (-getattr(h, key), h.id))


def retrieve(session: Session, llm: LLM, question: str, f: Filters, min_score: float,
             alt: str = "") -> Found:
    """The user's words AND an English version (alt): romanised matches romanised, English
    matches the stored translation. The trace is logged for every search — it is how a wrong
    answer gets debugged in two minutes (filter? threshold? model?)."""
    catch_up(session, llm)      # a note saved while indexing failed is findable from now on
    queries = [question]
    if alt.strip() and alt.strip().lower() != question.strip().lower():
        queries.append(alt.strip()[:300])
    trace: dict = {"queries": queries, "steps": []}
    stop = stopwords(session)
    terms = [keyword_terms(q, stop) for q in queries]
    try:
        qvecs = [llm.embed([q], task="RETRIEVAL_QUERY")[0] for q in queries]
    except LlmError as err:
        # Meaning search is down (quota, outage): exact words still work, and the reply says so.
        trace["embed_error"] = err.kind
        for ff, note in relaxations(f):
            hits = _merge([candidates(session, None, t, ff, MAX_HITS)[1] for t in terms], "kw")[:MAX_HITS]
            trace["steps"].append({"relaxed": note, "found": len(hits)})
            if hits:
                return Found(hits, note, "basic", trace)
        return Found([], "", "basic", trace)

    for ff, note in relaxations(f):
        found = [candidates(session, v, t, ff) for v, t in zip(qvecs, terms)]
        vec_hits = _merge([v for v, _ in found], "sim")
        kw_hits = _merge([k for _, k in found], "kw")
        hits = rank(vec_hits, kw_hits, min_score)
        trace["steps"].append({"relaxed": note,
                               "vec_top": [(h.id, round(h.sim, 3)) for h in vec_hits[:3]],
                               "kw_top": [h.id for h in kw_hits[:3]], "passed": [h.id for h in hits]})
        if hits:
            return Found(hits, note, "vector", trace)
    return Found([], "", "vector", trace)


# ── Answering ───────────────────────────────────────────────────────────────────────────────

ANSWER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING"},
        "cited_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
        "missing": {"type": "STRING"},
    },
    "required": ["answer", "cited_ids", "missing"],
}

ANSWER_RULES = (
    "You answer questions about a family farm using ONLY the NOTES below. "
    "Rules: "
    "1) Use only facts written in the notes. Never add advice, products, doses, numbers, dates, "
    "times or reasons that are not written there. "
    "2) If several notes match, mention each with its date. Put a treatment whose Result says it "
    "worked first, then the others from oldest to newest. Never call a treatment best unless a "
    "Result in the notes says it worked. "
    "3) Do not calculate, convert or total any number. "
    "4) Put the id of every note you used in cited_ids, exactly as written (e.g. J-12, K-3#1). "
    "Do not write ids inside the answer. "
    "5) If the question asks for something that is not written in the notes (a quantity, a time, "
    "who did it), put a short phrase for it in 'missing'; otherwise leave 'missing' empty. "
    "6) If the notes do not answer the question at all, return answer '' and cited_ids []. "
    "7) Notes whose id starts with K are the farmer's advice: introduce them with 'Farmer advice:'. "
    "8) Reply in the SAME language and script as the question (romanized Gujarati stays romanized). "
    "Plain text, no markdown, at most 6 short lines. Never use the words owe, owes or owed."
)

_ID_IN_TEXT = re.compile(r"\b[JK]-\d+(?:#\d+)?\b")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(t: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(_ID_IN_TEXT.sub(" ", str(t or "")))}


def ungrounded_numbers(answer: str, cited_texts: list[str]) -> list[str]:
    """Every number in the answer must be written in a cited note. Catches the most dangerous
    hallucination on a farm — an invented dose — without a second model call."""
    allowed: set[str] = set()
    for t in cited_texts:
        allowed |= _numbers(t)
    return sorted(n for n in _numbers(answer) if n not in allowed)


def _notes_block(hits: list[Hit]) -> str:
    return "\n".join(f"[{h.id}] {original_text(h.text)}"
                     for h in sorted(hits, key=lambda h: h.meta.get("date") or ""))


def source_line(cited: list[str], hits: list[Hit]) -> str:
    """Built by code from the rows, never by the model: a date or name here is the stored one."""
    by_id = {h.id: h for h in hits}
    seen, parts = set(), []
    for cid in cited:
        h = by_id.get(cid)
        if h is None:
            continue
        m = h.meta
        if h.kind == "knowledge":
            key = cid.split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            parts.append(f"Farmer advice {m.get('date_str', '')} ({key})")
        else:
            who = (m.get("added_by") or "").strip()
            parts.append(f"{who + chr(39) + 's' if who else 'Farm'} note {m.get('date_str', '')} ({cid})")
    return "📎 " + "; ".join(parts) if parts else ""


def fallback_list(hits: list[Hit]) -> str:
    """Used when the model's answer fails a check: the notes themselves, word for word."""
    # Best match first, the order the search ranked them in. Live Q5 (29-10): sorted by date, the
    # one note that answered the question came last, under two unrelated ones.
    rows = hits[:MAX_HITS]
    return "Matching notes (best match first):\n" + "\n".join("• " + original_text(h.text)[:220]
                                                             for h in rows)


@dataclass
class Answer:
    status: str                 # found | not_found | fallback
    reply: str = ""
    cited: list[str] = field(default_factory=list)
    photos: list[tuple[str, str]] = field(default_factory=list)   # (file_id, caption)
    missing: str = ""


def answer(llm: LLM, question: str, found: Found) -> Answer:
    hits = found.hits
    if not hits:
        return Answer("not_found")
    hit_ids = {h.id for h in hits}
    prompt = f"QUESTION: {question}\n\nNOTES:\n{_notes_block(hits)}"
    feedback = ""
    for attempt in range(2):
        try:
            raw = llm.complete(ANSWER_RULES, prompt + feedback, json_schema=ANSWER_SCHEMA,
                               temperature=0.1, budget_s=25)
            form = json.loads(raw or "{}")
            if not isinstance(form, dict):
                raise ValueError("answer is not an object")
        except (LlmError, ValueError) as err:
            log.warning("rag answer failed", extra={"fields": {
                "error": type(err).__name__, "kind": getattr(err, "kind", ""),
                "detail": str(err)[:300]}})
            return Answer("fallback", fallback_list(hits), [h.id for h in hits])
        text_ = str(form.get("answer") or "").strip()
        cited_raw = [str(c).strip() for c in (form.get("cited_ids") or [])]
        cited = [c for c in cited_raw if c in hit_ids]
        missing = str(form.get("missing") or "").strip()
        if any(c not in hit_ids for c in cited_raw):
            log.warning("rag answer cited ids it was never given",
                        extra={"fields": {"ids": [c for c in cited_raw if c not in hit_ids]}})
        if not text_ or not cited:
            return Answer("not_found", missing=missing)
        bad = ungrounded_numbers(text_, [original_text(h.text) for h in hits if h.id in cited])
        if not bad:
            break
        log.warning("rag answer had numbers not in the notes",
                    extra={"fields": {"numbers": bad, "attempt": attempt + 1}})
        feedback = (f"\n\nYOUR LAST ANSWER WAS REJECTED: it contained {', '.join(bad)}, which is "
                    "not written in the cited notes. Use only numbers written in the notes.")
    else:
        return Answer("fallback", fallback_list([h for h in hits if h.id in cited] or hits), cited)

    text_ = _ID_IN_TEXT.sub("", text_).replace("()", "").strip()
    lines = []
    if found.relaxed:
        lines.append(f"ℹ️ {found.relaxed[0].upper() + found.relaxed[1:]}.")
    lines.append(text_)
    # The answer, then a blank line, then where it came from: one block read as noise on a phone
    # (owner, 06-11-2026).
    tail = []
    if missing:
        tail.append(f"ℹ️ Not written in the notes: {missing}")
    src = source_line(cited, hits)
    if src:
        tail.append(src)
    if tail:
        lines += ["", *tail]
    photos = [(h.meta["photo"], f"{h.id} · {h.meta.get('date_str', '')}")
              for h in hits if h.id in cited and h.meta.get("photo")]
    return Answer("found", "\n".join(lines), cited, photos[:3], missing)


def how_line(found: Found, f: Filters, status: str = "ok") -> str:
    """How this answer was found, in one line the user can read: the concept, the filters, and
    what was loosened. Built by code from the search itself, never written by the model."""
    if found.mode == "basic":
        head = "📒 How: exact words only (keyword search) — meaning search is down right now"
    else:
        n = len(found.hits)
        head = (f"🔎 How: RAG — your words searched by meaning + exact words, "
                f"{n} note{'s' if n != 1 else ''} passed the match check")
    kept = [x for x in (f.plant, f.variety, f.activity) if x]
    if f.date_from or f.date_to:
        kept.append(f"{f.date_from:%d-%m-%Y} to {f.date_to:%d-%m-%Y}" if f.date_from and f.date_to
                    else "dates")
    if kept and not found.relaxed:
        head += " · filter: " + ", ".join(kept)
    if found.relaxed:
        head += f" · {found.relaxed}"
    if status == "fallback":
        head += " · notes shown word for word (the AI summary failed a check)"
    return head


def basic_reply(found: Found) -> str:
    rows = [f"• {original_text(h.text)[:220]}" for h in found.hits]
    src = source_line([h.id for h in found.hits], found.hits)
    return "\n".join(["🔎 Basic search (exact words only — meaning search is unavailable):"]
                     + rows + ([src] if src else []))


# ── General advice: only when the notes have nothing, and only when asked ───────────────────

_DOSE_WORDS = re.compile(
    r"\b(ml|litre|liter|ltr|lit|gram|grams|gm|kg|dose|doses|matra|maatra|praman|ketlu|ketla|"
    r"kitna|kitni|how much|quantity|mix)\b|ભેળવ|કેટલું|કેટલા|માત્રા|પ્રમાણ", re.IGNORECASE)
_CHEM_WORDS = re.compile(
    r"\b(dava|dawa|davai|spray|khatar|khaatar|fertili[sz]er|pesticide|insecticide|fungicide|"
    r"neem|copper|urea|dap|npk|potash|humic|chlorpyrifos|imidacloprid|mancozeb|bavistin|"
    r"roundup|glyphosate)\b|દવા|ખાતર|સ્પ્રે", re.IGNORECASE)


def is_dose_question(q: str) -> bool:
    """A chemical dose is never answered from general knowledge: a wrong number burns the tree
    or poisons the fruit. The label or the farmer decides."""
    return bool(_DOSE_WORDS.search(q or "")) and bool(_CHEM_WORDS.search(q or ""))


GENERAL_RULES = (
    "Give short general advice for a small fruit farm in Gujarat, India. At most 5 short lines, "
    "plain text, no markdown, same language and script as the question. Never give a chemical "
    "dose or quantity; say to follow the product label or ask a local agriculture expert instead."
)
DOSE_REFUSAL = ("💡 Not in your notes, and doses are not answered from general knowledge.\n"
                "Please follow the product label, or ask the farmer / agriculture officer.")


def general_advice(llm: LLM, question: str) -> str:
    if is_dose_question(question):
        return DOSE_REFUSAL
    try:
        t = llm.complete(GENERAL_RULES, question, temperature=0.3, budget_s=25).strip()
    except LlmError:
        t = ""
    if not t:
        return "💡 Couldn't get general advice right now. Please try again in a minute."
    return "💡 General advice (NOT from your notes):\n" + t


def resolve_dates(date_from: str | None, date_to: str | None,
                  today: date) -> tuple[date | None, date | None, str, str]:
    """Model date strings -> dates, with the rule people expect: a period in the future means
    last year ('October' asked in January). Returns (from, to, note, error)."""
    df = parse_date(date_from) if date_from else None
    dt = parse_date(date_to) if date_to else None
    if (date_from and df is None) or (date_to and dt is None):
        return None, None, "", "dates must be DD-MM-YYYY"
    if df and dt and df > dt:
        df, dt = dt, df
    note = ""
    if df and df > today:
        def back(d: date) -> date:
            try:
                return d.replace(year=d.year - 1)
            except ValueError:            # 29 Feb
                return d - timedelta(days=365)
        df = back(df)
        dt = back(dt) if dt else None
        note = f"assumed {df.year}"
    if dt and dt > today:
        dt = today
    return df, dt, note, ""
