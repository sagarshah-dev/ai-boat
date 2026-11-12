"""🌱 Farm Salah: farm tasks where one answer feeds the next step, as a LangGraph graph.

Why a graph and not one more tool for the free agent: live tests on 06-11 and 11-11-2026
(docs/decisions.md, 26) asked "which medicine worked, and when did we buy it?". The free agent
searched expenses for the word "dava" instead of the medicine it had just found, and on the same
data the next time did not search expenses at all. "On the day we sprayed copper, what else did
we spend?" lost its second half, because the loop ends early on a ready template. In both, step
two needs step one's ANSWER. Here that order is an edge in code, and step two reads its input
from the state, never from the model's memory of the question.

    understand (U) -> journal (A) -+-> dose (B) -> calc (C) -> approve (E) -> answer (F)
                                   +-> expense (D) ---------------------------> answer (F)
                                   +-> answer (F)   (nothing usable found: say so, stop)

Who decides what:
  - Gemini (the agent) picks this tool and fills need / topic / trees; code grounds every one of
    them in the user's words (U).
  - Gemini reads the candidate notes and says which are about the SAME problem (A): "kala"
    is also in the copper note about black leaf spots, a word match is not enough.
  - Gemini reads the dose out of the farmer's advice (B); code checks both numbers are written
    in that advice.
  - Everything else is code: which note worked (its Result), the arithmetic, the expense lookup,
    the reply text and the How line.
  - Nothing is saved without the asker's ✅ (E). The graph waits with interrupt(); its state is
    checkpointed in Postgres, inside the same transaction as the rest of the request, so the
    wait survives Cloud Run sleeping (24 hours, pending.SALAH).
"""
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from sqlalchemy import select

from app.core.formatting import fmt_date, inr
from app.core.replies import Buttons
from app.core.text import words
from app.db.models import Knowledge
from app.integrations.gemini import LlmError, ToolSpec
from app.services import expenses, farm, pending, rag, rag_index
from app.services.farm import ALL

if TYPE_CHECKING:
    from app.ai.agent import Context

log = logging.getLogger(__name__)

NAME = "farm_salah"
WAIT = timedelta(hours=24)        # how long a plan waits for ✅ (owner's decision, 11-11-2026)
MAX_CANDIDATES = 8                # notes shown to the relevance check
NEEDS = ("plan", "purchase", "same_day")
_TREES_MAX = 5000

SPEC = ToolSpec(
    NAME,
    "A farm task in SEVERAL steps where one answer is needed for the next. Use for exactly these: "
    "(plan) the user asks to MAKE a spray/treatment plan for a problem: 'Kesar na 100 zhad par mor "
    "kala che, spray plan banao'; (purchase) which medicine worked for a problem AND when it was "
    "bought: 'mor kala padya tyare kai dava kaam kari, ane e dava chhelle kyare kharidi?'; "
    "(same_day) WHEN some farm work was done AND what was spent on that day — any work: spray, "
    "watering, fertilizer, pruning: 'je divase copper chhantyu, e divase biju kai kharch thayo?', "
    "'chhelle kyare paani aapyu? e divase majuri ketli thai?'. The shape matters, not the words: "
    "a farm date that the expense question depends on. Not for a single question about notes or "
    "expenses — use the other tools for those. Not for saving a plan the user wrote — that is "
    "save_farm_advice.",
    {"type": "OBJECT", "properties": {
        "need": {"type": "STRING", "enum": list(NEEDS),
                 "description": "plan, purchase or same_day, as above."},
        "topic": {"type": "STRING",
                  "description": "The problem or product, in the user's own words, without the "
                                 "plant: 'mor kala', 'copper'."},
        "topic_en": {"type": "STRING", "description": "The same topic in plain English."},
        "trees": {"type": "INTEGER",
                  "description": "Number of trees, only if the user wrote a number of trees."},
        "activity": {"type": "STRING", "enum": [a for a in farm.ACTIVITIES if a != "planned"],
                     "description": "The kind of farm work the question is about, if it names one: "
                                    "'paani aapyu' -> irrigation, 'khatar nakhyu' -> fertilizer, "
                                    "'spray/chhantyu' -> spray. Empty for a problem ('mor kala')."},
        "expense_words": {"type": "STRING",
                          "description": "same_day only: the kind of expense asked about, in the "
                                         "user's words ('majuri', 'diesel'). Empty when the user "
                                         "asks about all spending that day."},
    }, "required": ["need", "topic"]})

RULES = (
    "FARM SALAH: farm_salah runs a multi-step task (plan / medicine + its purchase / expenses on "
    "the day of a farm task). Call it once; it writes its own reply. "
)


# ── State: the graph's notebook. Plain JSON values only — it is checkpointed. ──────────────

class Note(TypedDict, total=False):
    id: int
    date: str
    product: str
    problem: str
    result: str
    outcome: str          # worked | partly | not_worked | ""
    activity: str         # Watering, Spray, ... (as the journal shows it)


class State(TypedDict, total=False):
    question: str
    args: dict                    # what the agent's model filled in; U grounds it
    need: str
    plant: str
    variety: str
    topic: str
    topic_en: str
    activity: str                 # irrigation / spray / fertilizer... ("" = not named)
    trees: int | None
    notes: list[Note]             # relevant notes, newest first
    dava: str                     # the product that worked (newest ✅)
    dava_note: int
    failed: list[Note]            # products that did not work: said out loud
    tarikh: str                   # date of the task (same_day), ISO
    expense_words: str            # same_day: only expenses with this word ("majuri"); "" = all
    kharch_that_day: int          # same_day: how many expenses that day in all
    tarikh_note: int
    dose_ml: float                # ml of product per litre of water
    litres_per_tree: float
    dose_from: str                # K-4
    total_ml: float
    water_l: float
    kharch: list[dict]
    stop: str                     # why the graph ended early (no_notes, no_worked, no_dose, ...)
    decision: str                 # saved | cancelled
    saved_id: int
    path: list[str]               # nodes that ran, for the How line


@dataclass
class Deps:
    """What the nodes need that is not data: the request's session, model and member. Passed as
    LangGraph's runtime context, so it is never written into a checkpoint."""
    ctx: "Context"


def _ran(state: State, node: str) -> list[str]:
    return [*state.get("path", []), node]


# ── U: understand (Gemini filled the tool args; code grounds them) ──────────────────────────

_NUM = re.compile(r"\d+")


def ground(known, args: dict, message: str) -> dict:
    """Only what the user wrote survives. The plant comes from resolve_plants(message), never
    from the model; trees only if that number is in the message; the topic only if one of its
    words is, else the message itself."""
    need = str(args.get("need") or "").strip()
    match = farm.resolve_plants(known, message)
    plant = farm.single_plant(match) or ""
    variety = match.variety if plant and match and match.variety not in ("", ALL) else ""
    topic = " ".join(str(args.get("topic") or "").split())[:80]
    msg_words = set(words(message))
    if not topic or not any(w in msg_words for w in words(topic)):
        topic = message.strip()[:120]
    trees = None
    try:
        t = int(float(args.get("trees") or 0))
        if 0 < t <= _TREES_MAX and str(t) in _NUM.findall(message):
            trees = t
    except (TypeError, ValueError):
        pass
    # Words like "paani" / "khatar" are not search words in this app (farm_tools._ACTIVITY_WORDS):
    # the kind of work is the activity column. The model names it; code keeps it only if it is a
    # real activity, so a typo can't empty the search.
    activity = str(args.get("activity") or "").strip().lower()
    activity = activity if activity in farm.ACTIVITIES and activity != "planned" else ""
    # An expense filter the user did not write would hide money: keep only the words they did.
    kind = " ".join(w for w in words(str(args.get("expense_words") or "")) if w in msg_words)
    return {"need": need, "plant": plant, "variety": variety, "topic": topic,
            "topic_en": " ".join(str(args.get("topic_en") or "").split())[:120], "trees": trees,
            "expense_words": kind[:40], "activity": activity}


def understand(state: State, runtime: Runtime[Deps]) -> dict:
    known = farm.plants(runtime.context.ctx.session)
    return {**ground(known, state.get("args", {}), state["question"]),
            "path": _ran(state, "understand")}


# ── A: journal — candidates by exact words + meaning, then an AI relevance check ────────────

GRADE_RULES = (
    "You check farm notes against ONE question. For each note, decide whether it is about what "
    "the question asks: the SAME problem (same plant part and same symptom), the same product it "
    "names, or the same kind of farm work it names (watering, fertilizer, pruning...). A shared "
    "word is not enough: black spots on leaves is not black flowers. "
    "Return the ids of the notes that are about it, exactly as written. Return [] if none."
)
GRADE_SCHEMA = {"type": "OBJECT", "properties": {
    "relevant_ids": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["relevant_ids"]}


def outcome_of(result: str) -> str:
    """The Result field starts with the label code wrote (farm.OUTCOME_LABEL); read it back."""
    r = (result or "").strip()
    for key, label in farm.OUTCOME_LABEL.items():
        if r.startswith(label.split()[0]):
            return key
    return ""


def _note(e) -> Note:
    return {"id": e.id, "date": e.entry_date.isoformat(), "product": e.product.strip(),
            "problem": e.problem.strip(), "result": e.result.strip(), "outcome": outcome_of(e.result),
            "activity": farm.ACTIVITY_LABEL.get(e.activity, e.activity)}


def candidates(ctx: "Context", state: State) -> list:
    """Journal rows that may be about the topic: every word of it written in the note, or found
    by meaning (the English copy connects 'mor kala' to 'flowers black'). Newest first."""
    from app.ai import farm_tools   # here: farm_tools is imported by the agent, which imports us
    s = ctx.session
    plant, variety = state.get("plant") or None, state.get("variety") or None
    rows = [e for e in farm.query(s, plant=plant, variety=variety) if e.activity != "planned"]
    terms = farm_tools.topic_words(farm.plants(s), state["topic"], rag_index.stopwords(s))
    exact = [e for e in rows if terms and all(farm.mentions(e, t) for t in terms)]
    # "Chhelle kyare paani aapyu?": "paani" is dropped as a search word (it is an activity), so
    # without this a watering note is found by neither words nor meaning (live, 12-11-2026).
    act = state.get("activity", "")
    by_kind = [e for e in rows if act and e.activity == act][:MAX_CANDIDATES]
    f = rag.Filters(plant=plant or "", variety=variety or "", journal_only=True)
    found = rag.retrieve(s, ctx.llm, state["topic"], f, farm_tools._min_score(ctx),
                         alt=state.get("topic_en", ""))
    meant = []
    if found.mode == "vector":
        for h in found.hits:
            if not h.id.startswith("J-"):
                continue
            e = farm.get_entry(s, int(h.id[2:]))
            # A hit found only after the plant filter was dropped is about another plant.
            if e and e.activity != "planned" and (not plant or farm.matches_plant(e, plant)):
                meant.append(e)
    seen, out = set(), []
    for e in exact + by_kind + meant:
        if e.id not in seen:
            seen.add(e.id)
            out.append(e)
    out.sort(key=lambda e: (e.entry_date, e.id), reverse=True)
    return out[:MAX_CANDIDATES]


def grade(ctx: "Context", question: str, rows: list) -> list:
    """Gemini reads the notes; only ids it was given count. On a model failure the error goes
    up: a plan built on unchecked notes is worse than 'try again'."""
    if not rows:
        return []
    block = "\n".join(f"[J-{e.id}] {rag.original_text(farm.journal_doc_text(e))}" for e in rows)
    raw = ctx.llm.complete(GRADE_RULES, f"QUESTION: {question}\n\nNOTES:\n{block}",
                           json_schema=GRADE_SCHEMA, temperature=0.0, budget_s=25)
    try:
        ids = {str(i).strip() for i in (json.loads(raw or "{}").get("relevant_ids") or [])}
    except (ValueError, AttributeError):
        raise LlmError("unknown", "relevance check was not JSON") from None
    return [e for e in rows if f"J-{e.id}" in ids]


def journal(state: State, runtime: Runtime[Deps]) -> dict:
    ctx = runtime.context.ctx
    rows = grade(ctx, state["question"], candidates(ctx, state))
    notes = [_note(e) for e in rows]
    out: dict = {"notes": notes, "path": _ran(state, "journal")}
    if not notes:
        return {**out, "stop": "no_notes"}
    if state["need"] == "same_day":
        e = rows[0]
        return {**out, "tarikh": e.entry_date.isoformat(), "tarikh_note": e.id}
    with_product = [n for n in notes if n["product"]]
    worked = [n for n in with_product if n["outcome"] == "worked"]
    out["failed"] = [n for n in with_product if n["outcome"] == "not_worked"]
    if not worked:
        return {**out, "stop": "no_worked"}
    return {**out, "dava": worked[0]["product"], "dava_note": worked[0]["id"]}


def after_journal(state: State) -> Literal["dose", "expense", "answer"]:
    if state.get("stop"):
        return "answer"
    return "dose" if state["need"] == "plan" else "expense"


# ── B: dose from the farmer's advice ────────────────────────────────────────────────────────

DOSE_RULES = (
    "Read the farmer's advice and copy the dose for the product named. ml_per_litre = ml of the "
    "product in one litre of water. litres_per_tree = litres of spray for one tree. Copy the "
    "numbers exactly as written; use 0 for anything not written. Never estimate."
)
DOSE_SCHEMA = {"type": "OBJECT", "properties": {
    "ml_per_litre": {"type": "NUMBER"}, "litres_per_tree": {"type": "NUMBER"},
    "advice_id": {"type": "STRING"}}, "required": ["ml_per_litre", "litres_per_tree", "advice_id"]}


def _num_written(n: float, text: str) -> bool:
    found = {Decimal(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", text)}
    try:
        return Decimal(str(n)).normalize() in {f.normalize() for f in found}
    except ArithmeticError:
        return False


def advice_for(ctx: "Context", product: str) -> list[Knowledge]:
    """Farmer advice that names the product (its first word is enough: 'hexaconazole 5% SC')."""
    key = (product.split() or [""])[0].lower()
    if len(key) < 4:
        return []
    rows = ctx.session.scalars(select(Knowledge).order_by(Knowledge.id.desc()))
    return [k for k in rows if key in f"{k.title} {k.text}".lower()][:3]


def dose(state: State, runtime: Runtime[Deps]) -> dict:
    ctx = runtime.context.ctx
    out: dict = {"path": _ran(state, "dose")}
    advice = advice_for(ctx, state["dava"])
    if not advice:
        return {**out, "stop": "no_dose"}
    block = "\n".join(f"[K-{k.id}] {k.title}: {k.text}" for k in advice)
    raw = ctx.llm.complete(DOSE_RULES, f"PRODUCT: {state['dava']}\n\nADVICE:\n{block}",
                           json_schema=DOSE_SCHEMA, temperature=0.0, budget_s=25)
    try:
        form = json.loads(raw or "{}")
        ml, litres = float(form.get("ml_per_litre") or 0), float(form.get("litres_per_tree") or 0)
    except (ValueError, TypeError, AttributeError):
        raise LlmError("unknown", "dose form was not JSON") from None
    src = next((k for k in advice if f"K-{k.id}" == str(form.get("advice_id", "")).strip()), None)
    # Both numbers must be written in the advice it came from: an invented dose burns a tree.
    if (src is None or ml <= 0 or litres <= 0 or not _num_written(ml, src.text)
            or not _num_written(litres, src.text)):
        return {**out, "stop": "no_dose"}
    return {**out, "dose_ml": ml, "litres_per_tree": litres, "dose_from": f"K-{src.id}"}


def after_dose(state: State) -> Literal["calc", "answer"]:
    return "answer" if state.get("stop") else "calc"


# ── C: arithmetic (code only); asks for the number of trees if it was not given ──────────────

_TREE_WORDS = {"zhad", "jhad", "zad", "jad", "tree", "trees", "chhod", "chod", "j", "ja", "fakt",
               "only", "che", "chhe", "karo", "kar", "ma", "na", "kesar", "aamba", "mango"}


def trees_from_reply(text: str) -> int | None:
    """'60', '60 zhad j': one number and nothing but tree words. '100 tea' is an expense, not an
    answer, so it is not taken (it goes on to the normal path)."""
    toks = re.findall(r"[^\W_]+", str(text or "").lower())
    nums = [t for t in toks if t.isdigit()]
    if len(nums) != 1 or any(t not in _TREE_WORDS for t in toks if not t.isdigit()):
        return None
    n = int(nums[0])
    return n if 0 < n <= _TREES_MAX else None


def calc(state: State, runtime: Runtime[Deps]) -> dict:
    trees = state.get("trees")
    if not trees:
        trees = interrupt({"ask": "trees"})          # resumed with a checked int
    water = trees * state["litres_per_tree"]
    return {"trees": trees, "water_l": round(water, 2),
            "total_ml": round(water * state["dose_ml"], 2), "path": _ran(state, "calc")}


# ── E: wait for the asker; save only on ✅ ───────────────────────────────────────────────────

def approve(state: State, runtime: Runtime[Deps]) -> Command[Literal["calc", "answer"]]:
    choice = interrupt({"ask": "approve"})
    if choice.get("action") == "trees":
        # Only the number changed: the notes and the dose are already in the state, so the
        # graph goes back to the arithmetic, not to the start.
        return Command(goto="calc", update={"trees": int(choice["trees"]),
                                            "path": _ran(state, "approve:edit")})
    if choice.get("action") != "save":
        return Command(goto="answer", update={"decision": "cancelled",
                                              "path": _ran(state, "approve")})
    ctx = runtime.context.ctx
    e = farm.add_entry(ctx.session, plan_entry(state, ctx.today), ctx.sender)
    rag_index.index_rows(ctx.session, ctx.llm, "journal", {e.id})
    return Command(goto="answer", update={"decision": "saved", "saved_id": e.id,
                                          "path": _ran(state, "approve")})


def plan_entry(state: State, today: date) -> dict:
    ml = _n(state["dose_ml"])
    return {"date": today.isoformat(), "plants": [state["plant"] or ALL],
            "variety": state.get("variety", ""), "activity": "planned", "qty": state["trees"],
            "product": state["dava"], "dose": f"{ml} ml per litre, {_n(state['litres_per_tree'])} L per tree",
            "problem": state["topic"],
            "note": (f"Farm Salah plan: {state['dava']} {_n(state['total_ml'])} ml in "
                     f"{_n(state['water_l'])} L water for {state['trees']} trees "
                     f"(worked: J-{state['dava_note']}, dose: {state['dose_from']})"),
            "source": "salah"}


# ── D: expenses, by the product found or the date found ─────────────────────────────────────

def expense(state: State, runtime: Runtime[Deps]) -> dict:
    s = runtime.context.ctx.session
    if state["need"] == "same_day":
        d = date.fromisoformat(state["tarikh"])
        day = expenses.search(s, date_from=d, date_to=d)
        kind = words(state.get("expense_words", ""))
        rows = [r for r in day if any(w in words(f"{r.description} {r.merchant}") for w in kind)] \
            if kind else day
        return {"kharch": _kharch(rows), "kharch_that_day": len(day), "path": _ran(state, "expense")}
    else:
        rows = expenses.search(s, text=state["dava"])
        parts = state["dava"].split()
        # "wettable sulphur" is often written as just "sulphur" in the expense: one word of
        # the product name, at least 5 letters, is enough. Never a fuzzy match.
        for word in (parts if len(parts) > 1 and not rows else []):
            if len(word) >= 5:
                rows = expenses.search(s, text=word)
                if rows:
                    break
    return {"kharch": _kharch(rows), "path": _ran(state, "expense")}


def _kharch(rows) -> list[dict]:
    return [{"id": r.id, "date": r.date.isoformat(), "amount": str(r.amount),
             "description": r.description, "paid_by": r.paid_by} for r in rows]


# ── F: the reply, written by code from the state ────────────────────────────────────────────

def answer(state: State, runtime: Runtime[Deps]) -> dict:
    return {"path": _ran(state, "answer")}


def build():
    g = StateGraph(State, context_schema=Deps)
    for name, fn in (("understand", understand), ("journal", journal), ("dose", dose),
                     ("calc", calc), ("approve", approve), ("expense", expense), ("answer", answer)):
        g.add_node(name, fn)
    g.add_edge(START, "understand")
    g.add_edge("understand", "journal")
    g.add_conditional_edges("journal", after_journal)
    g.add_conditional_edges("dose", after_dose)
    g.add_edge("calc", "approve")
    g.add_edge("expense", "answer")
    g.add_edge("answer", END)
    return g


GRAPH = build()


# ── Running it inside a request ─────────────────────────────────────────────────────────────

def _saver(session) -> PostgresSaver:
    """Checkpoints go through the request's own connection, so they commit or roll back with the
    journal row and the pending question — never a plan saved without its checkpoint or the
    reverse. No pipeline mode: Neon's pooled URL is PgBouncer in transaction mode."""
    saver = PostgresSaver(session.connection().connection.driver_connection)
    saver.supports_pipeline = False
    return saver


def _invoke(ctx: "Context", tid: str, value) -> tuple[State, dict | None]:
    saver = _saver(ctx.session)
    graph = GRAPH.compile(checkpointer=saver)
    try:
        # durability="exit": the checkpoint is written once, when the graph pauses or ends.
        # LangGraph writes step checkpoints from a background thread; on this ONE shared
        # connection that thread raced our node's own queries and the request's commit
        # ("commit() forbidden within a Transaction context", about 1 run in 100 in tests,
        # with both "async" and "sync"). Writing at exit loses nothing here: if the request
        # fails midway, its transaction rolls back the whole step anyway.
        result = graph.invoke(value, {"configurable": {"thread_id": tid}}, context=Deps(ctx),
                              durability="exit")
    except LlmError:
        saver.delete_thread(tid)
        pending.drop(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH)
        raise
    waits = result.get("__interrupt__") or []
    state = {k: v for k, v in result.items() if k != "__interrupt__"}
    if not waits:
        saver.delete_thread(tid)       # finished: nothing left to resume
    return state, (waits[0].value if waits else None)


def start(ctx: "Context", args: dict, message: str) -> dict:
    """Called by the agent as a tool. Returns a ready reply (template): no second model call."""
    if str(args.get("need") or "").strip() not in NEEDS:
        return {"error": f"need must be one of {', '.join(NEEDS)}. Call again."}
    old = pending.get(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH)
    if old:
        _saver(ctx.session).delete_thread(old.payload["thread"])   # a new task replaces it
    tid = f"salah-{ctx.chat_id}-{ctx.sender.id}-{uuid.uuid4().hex[:12]}"
    clean = {k: v for k, v in args.items() if isinstance(v, (str, int, float, bool))}
    try:
        state, wait = _invoke(ctx, tid, {"question": message.strip(), "args": clean, "path": []})
    except LlmError as err:
        return {"text_reply": _llm_failed(err)}
    return _respond(ctx, tid, state, wait)


def _llm_failed(err: LlmError) -> str:
    from app.ai.agent import LLM_ERROR_TEXT     # here: agent imports this module
    return LLM_ERROR_TEXT.get(err.kind, LLM_ERROR_TEXT["unknown"])


def resume(ctx: "Context", value: dict) -> dict:
    """A tap (✅ / ✏️ / ❌) or the number of trees. Only the asker's own waiting task is found:
    the pending row is keyed by chat AND member, so Biren's tap can't approve Sagar's plan."""
    w = pending.get(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH)
    if w is None:
        return {"text_reply": EXPIRED}
    try:
        state, wait = _invoke(ctx, w.payload["thread"], Command(resume=value))
    except LlmError as err:
        return {"text_reply": _llm_failed(err)}
    return _respond(ctx, w.payload["thread"], state, wait)


def waiting(ctx: "Context") -> str:
    """What the asker's task waits for: approve | trees | edit | '' (nothing)."""
    w = pending.get(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH)
    return w.payload.get("waiting", "") if w else ""


def ask_edit(ctx: "Context") -> dict:
    w = pending.get(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH)
    if w is None or w.payload.get("waiting") != "approve":
        return {"text_reply": EXPIRED}
    pending.put(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH,
                {**w.payload, "waiting": "edit"}, ttl=WAIT)
    return {"text_reply": "✏️ How many trees? Send just the number, e.g. 60."}


def drop(ctx: "Context") -> None:
    w = pending.get(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH)
    if w:
        _saver(ctx.session).delete_thread(w.payload["thread"])
        pending.drop(ctx.session, ctx.chat_id, ctx.sender.id, pending.SALAH)


def _respond(ctx: "Context", tid: str, state: State, wait: dict | None) -> dict:
    s, chat, who = ctx.session, ctx.chat_id, ctx.sender.id
    if wait is None:
        pending.drop(s, chat, who, pending.SALAH)
        return {"text_reply": final_text(state), "salah": state}
    kind = wait["ask"]
    pending.put(s, chat, who, pending.SALAH, {"thread": tid, "waiting": kind}, ttl=WAIT)
    if kind == "trees":
        return {"text_reply": head(state) + "\n\n🌳 How many trees? Send just the number, e.g. 60.",
                "salah": state}
    return {"text_reply": plan_text(state), "buttons": PLAN_BUTTONS, "salah": state}


# ── Reply templates ─────────────────────────────────────────────────────────────────────────

EXPIRED = "⌛ No plan is waiting for you (a plan waits 24 hours). Ask again to make a new one."
PLAN_BUTTONS: Buttons = [[("✅ Save plan", "fs:save"), ("✏️ Change trees", "fs:edit")],
                         [("❌ Cancel", "fs:no")]]
_STEP = {"journal": "journal (meaning + exact words, AI-checked)", "dose": "farmer advice",
         "calc": "calculation (code)", "expense": "expenses (SQL)",
         "approve": "your ✅", "approve:edit": "trees changed → calculation again"}


def _n(x) -> str:
    """1.0 -> '1', 2.5 -> '2.5'."""
    d = Decimal(str(x)).normalize()
    return f"{d:f}"


def _d(iso: str) -> str:
    return fmt_date(date.fromisoformat(iso))


def where(state: State) -> str:
    return farm.plant_label([state["plant"]], state.get("variety", "")) if state.get("plant") else "Farm"


def head(state: State) -> str:
    lines = []
    for n in state.get("notes", []):
        if n["product"] and n["product"] == state.get("dava") and n["id"] == state.get("dava_note"):
            lines.append(f"🧪 Worked: {n['product']} — {_d(n['date'])} (J-{n['id']}: {n['result']})")
    for n in state.get("failed", []):
        lines.append(f"⚠️ Did not work: {n['product']} — {_d(n['date'])} (J-{n['id']})")
    return "\n".join(lines)


def how(state: State) -> str:
    steps = [_STEP[p] for p in state.get("path", []) if p in _STEP]
    return "🔎 How: LangGraph — " + " → ".join(steps) if steps else ""


def plan_text(state: State) -> str:
    lines = [f"🌱 Spray plan — {where(state)} · {state['topic']} · {state['trees']} trees", "",
             head(state),
             f"💧 Dose: {_n(state['dose_ml'])} ml per litre, {_n(state['litres_per_tree'])} L per "
             f"tree (farmer advice {state['dose_from']})",
             f"🧮 {state['trees']} trees × {_n(state['litres_per_tree'])} L = {_n(state['water_l'])} L "
             f"water → {_n(state['total_ml'])} ml {state['dava']}",
             "", how(state) + " → waiting for you",
             "Nothing is saved until you tap ✅. The plan waits 24 hours."]
    return "\n".join(lines)


def final_text(state: State) -> str:
    stop, need = state.get("stop", ""), state.get("need", "")
    topic, place = state.get("topic", ""), where(state)
    if stop == "no_notes":
        body = (f"📒 No note about \"{topic}\" for {place} in the journal. Nothing to build on — "
                "ask the farmer or an agriculture expert.")
    elif stop == "no_worked":
        tried = "\n".join(f"⚠️ Tried: {n['product']} — {_d(n['date'])} (J-{n['id']})"
                          + (f": {n['result']}" if n["result"] else ": no result written")
                          for n in state.get("notes", []) if n["product"])
        body = (f"📒 No treatment for \"{topic}\" is written as worked (✅) in the journal."
                + (f"\n{tried}" if tried else "") + "\nAsk the farmer or an agriculture expert.")
    elif stop == "no_dose":
        body = (head(state) + f"\n\n💧 The dose of {state['dava']} is not written in the farmer's "
                "advice, so no plan was calculated. Save the dose as farmer advice (menu → 📚), "
                "or ask an agriculture expert.")
    elif need == "plan":
        if state.get("decision") == "saved":
            body = (f"✅ Plan saved as J-{state['saved_id']} (planned, not done yet)\n"
                    f"{state['dava']} · {_n(state['total_ml'])} ml · {state['trees']} trees · {place}\n"
                    "🔔 In 2 days I'll ask whether the spray was done.")
        else:
            body = "❌ Plan cancelled — nothing was saved."
    elif need == "purchase":
        rows = state.get("kharch", [])
        if rows:
            r = rows[0]
            buy = (f"💰 Last bought: {_d(r['date'])} — ₹{inr(Decimal(r['amount']))} — "
                   f"{r['description']} — {r['paid_by']} (#{r['id']})")
            if len(rows) > 1:
                buy += f"\n   ({len(rows) - 1} earlier purchase{'s' if len(rows) > 2 else ''} too)"
        else:
            buy = f"💰 No expense with \"{state['dava']}\" in its description — the purchase is not written."
        body = head(state) + "\n" + buy
    else:   # same_day
        n = next((x for x in state.get("notes", []) if x["id"] == state.get("tarikh_note")), None)
        # A watering note has no product or problem: name it by what was done.
        what = (n["product"] or n["problem"] or n.get("activity") or "note") if n else "note"
        rows, kind, day = state.get("kharch", []), state.get("expense_words", ""), _d(state["tarikh"])
        label = f"{kind} expenses" if kind else "Expenses"
        lines = [f"📒 {what} — {day} (J-{state['tarikh_note']})"]
        if rows:
            total = sum(Decimal(r["amount"]) for r in rows)
            lines.append(f"💰 {label[:1].upper() + label[1:]} on {day}:")
            lines += [f"• #{r['id']} — ₹{inr(Decimal(r['amount']))} — {r['description']} — "
                      f"{r['paid_by']}" for r in rows]
            lines.append(f"💵 Total: ₹{inr(total)}")
        else:
            lines.append(f"💰 No {kind + ' ' if kind else ''}expense written on {day}.")
        others = state.get("kharch_that_day", 0) - len(rows)
        if kind and others > 0:
            lines.append(f"   ({others} other expense{'s' if others > 1 else ''} that day — ask "
                         "without the word to see all)")
        body = "\n".join(lines)
    h = how(state)
    return f"{body}\n{h}" if h else body


# ── 2 days later: "was the spray done?" (a tapped button: one meaning, no model) ───────────

PLAN_BUTTON = re.compile(r"^pl:(\d{1,9}):([yn])$")


def plan_buttons(jid: int) -> Buttons:
    return [[("✅ Done", f"pl:{jid}:y"), ("❌ Not done", f"pl:{jid}:n")]]


def plan_reminder_text(e) -> str:
    return f"🔔 Plan check (J-{e.id}):\n{farm.journal_line(e)}\n\nWas this spray done?"


def plan_answer(ctx: "Context", jid: int, done: bool) -> str:
    e = farm.get_entry(ctx.session, jid)
    if e is None or e.activity != "planned":
        return f"J-{jid} is no longer a waiting plan. Nothing was changed."
    if not done:
        farm.mark_followup(ctx.session, jid, "skipped")
        return f"👍 OK — J-{jid} stays a plan (not done). Nothing else was changed."
    farm.plan_done(ctx.session, jid, ctx.today)
    rag_index.index_rows(ctx.session, ctx.llm, "journal", {jid})
    return (f"✅ J-{jid} is now a spray done on {fmt_date(ctx.today)}.\n"
            "🔔 In 7 days I'll ask how it went.")


# ── Mermaid picture of the graph, for the README / interview ────────────────────────────────

def mermaid() -> str:
    return GRAPH.compile().get_graph().draw_mermaid()
