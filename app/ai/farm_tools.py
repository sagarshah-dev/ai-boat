"""Farm journal tools for the agent, the checks on their arguments, and what happens after "yes".

Same rule as amounts: the model may only fill a field with something the USER wrote. A farm note
with an invented dose is worse than one with an empty field, because six months later someone
copies that dose onto a tree. So:
  - the plant comes from resolve_plants(user's words), never from the model's argument;
  - a dose is kept only if every number in it is in the message;
  - a product / problem / place is kept only if one of its words is in the message;
  - a date is kept only if the message says anything about WHEN;
  - every note is PROPOSED and saved only after Save / yes.
Ported from the old bot's main.py (build_journal_proposal, farm_activities_text, search_notes,
_apply_farm) and tools.py.
"""
import json
import logging
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING

from app.ai import proposals
from app.core.formatting import fmt_date, parse_date
from app.core.replies import Buttons, Out, Photo, Reply, yes_no
from app.core.text import strip_marks, words
from app.integrations.gemini import ToolSpec
from app.services import farm, pending, rag, rag_index
from app.services.farm import ACTIVITY_LABEL, ALL, OUTCOME_LABEL
from app.services.meetings import WEEKDAY_SHORT, datetime_signal

if TYPE_CHECKING:
    from app.ai.agent import Context

log = logging.getLogger(__name__)

LOG, ADVICE, RESULT, DELETE = ("log_farm_activity", "save_farm_advice", "record_treatment_result",
                               "delete_last_farm_note")
ACTIVITIES_Q, COUNTS, SEARCH = "get_farm_activities", "get_plant_counts", "search_farm_notes"
NAMES = {LOG, ADVICE, RESULT, DELETE, ACTIVITIES_Q, COUNTS, SEARCH}
GENERAL_TTL = timedelta(minutes=10)

# Where an answer came from, on every farm reply, so the user (and a reviewer) can see which
# answers were looked up exactly and which were found by meaning.
# One line under every farm answer: the concept used and how the rows were found. Exact rows
# (SQL) and meaning search (RAG) answer different questions; the user should see which one ran.
SRC_EXACT = "📒 How: exact match (SQL) — journal rows that name these words"
SRC_EXACT_EN = "📒 How: exact match (SQL) — words found in the English copy of the notes"
SRC_COUNT = "📒 How: counted by code (SQL) — no AI in the numbers"
SRC_HYBRID = "🔎 How: notes found by meaning (RAG), dates read from the journal rows (SQL)"
SRC_RAG = "🔎 How: RAG"      # prefix only; rag.how_line() writes the full line


# ── Word checks ─────────────────────────────────────────────────────────────────────────────

_TIME_OF_DAY = {
    "morning": {"savare", "saware", "sawar", "savar", "morning", "subah", "સવારે", "સવાર"},
    "afternoon": {"bapore", "bapor", "afternoon", "dopahar", "બપોરે", "બપોર"},
    "evening": {"saanje", "sanje", "saanj", "sanj", "evening", "shaam", "sham", "સાંજે", "સાંજ"},
    "night": {"ratre", "raat", "rat", "night", "રાત્રે", "રાત"},
}
_TIME_OF_DAY_LOOKUP = {strip_marks(w).lower(): k for k, ws in _TIME_OF_DAY.items() for w in ws}

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_PERIOD_WORDS = {strip_marks(w).lower() for w in (
    set(_MONTHS) | {m[:3] for m in _MONTHS} | {
        "sept", "mahine", "mahino", "mahina", "month", "months", "year", "varas", "varse", "saal",
        "week", "athvadiye", "athvadiya", "hafte", "pichhla", "chomasu", "chomasa", "chomasama",
        "unalo", "unala", "shiyalo", "shiyala", "diwali", "holi", "uttarayan",
        "જાન્યુઆરી", "ફેબ્રુઆરી", "માર્ચ", "એપ્રિલ", "મે", "જૂન", "જુલાઈ", "ઓગસ્ટ", "સપ્ટેમ્બર",
        "ઓક્ટોબર", "નવેમ્બર", "ડિસેમ્બર", "મહિને", "ચોમાસું", "ઉનાળો", "શિયાળો", "દિવાળી"})}

# Words that describe the QUESTION, not its topic: "chhelle spray kyare karyu?" has no topic.
_NOT_TOPIC = {strip_marks(w).lower() for w in (
    "chhelle", "chhelli", "chhello", "chelle", "chello", "chelli", "last", "latest", "recent",
    "badha", "badhu", "badhi", "farm", "khet", "wadi", "vadi", "note", "notes", "entry", "entries",
    "list", "aaje", "kale", "gaye", "gaya", "gai", "pachhi", "pehla", "pela", "aagal", "vakhat",
    "var", "vaar", "chhod", "chod", "zad", "plant", "plants", "tree", "trees", "kai", "kyu",
    "ketli", "aapyu", "aapi", "apyu", "api", "nakhyu", "naakhyu", "nakhi", "chhantyu", "kidhu",
    "kidhi", "thayu", "thai", "mukyu", "muki", "didhu", "lagavyu", "lidhu", "karo", "karvama",
    "karelu", "kareli", "done", "given", "gave", "used", "today", "yesterday", "ago", "hatu",
    "hato", "hati", "kya", "kab", "kiya", "tha", "thi", "che", "chhe", "ma", "kyre", "kyare",
    "ક્યારે", "કરી", "કર્યું", "કર્યો", "કરેલું", "હતું", "હતો", "હતી", "શું", "છે", "આપ્યું", "આપી",
    "નાખ્યું", "નાખી", "છેલ્લે", "છેલ્લી", "છેલ્લો", "વખત", "વાર", "બધા", "બધું", "છોડ", "ઝાડ", "આજે",
    "કાલે", "कब", "किया", "की", "का", "के", "को", "में", "है", "था", "थी", "क्या", "कैसे", "आखिरी",
    "बार", "पिछली", "kaise", "liye", "hai")}
_ACTIVITY_WORDS = {strip_marks(w).lower() for w in (
    set(farm.ACTIVITIES) | {"sprays", "dava", "dawa", "khatar", "khaatar", "fertiliser", "paani",
                            "pani", "piyat", "kapani", "chhatni", "ropya", "ropyu", "ropan",
                            "treatments", "harvesting", "watering", "ખાતર", "દવા", "સ્પ્રે", "પાણી"})}
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def period_signal(text: str) -> bool:
    """Does the message say anything about WHEN — a digit, a time word, a month, a season?
    'October ma shu karyu?' has no digit; before the month words it silently lost its period."""
    return datetime_signal(text) or any(w in _PERIOD_WORDS for w in words(text))


def time_of_day_in(text: str) -> str:
    found = {_TIME_OF_DAY_LOOKUP[w] for w in words(text) if w in _TIME_OF_DAY_LOOKUP}
    return found.pop() if len(found) == 1 else ""


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(str(text or ""))}


def grounded_numbers(field: str, message: str) -> bool:
    return numbers_in(field) <= numbers_in(message)


def grounded_words(field: str, message: str, min_len: int = 3) -> bool:
    """At least one real word of the field is in the message: 'neem' for 'neem oil' passes,
    'copper oxychloride' when the user wrote 'dava' does not."""
    msg = set(words(message))
    return any(w in msg for w in words(field) if len(w) >= min_len)


def topic_words(known: list[farm.PlantInfo], message: str, stop: frozenset[str]) -> list[str]:
    """What a question is ABOUT beyond plant, activity and period: 'udhai', 'neem'. These decide
    the lookup, so a topic is never silently dropped and the search never widens to everything."""
    alias_words = {w for p in known for a in p.aliases for w in a.split()}
    out = []
    for t in rag_index.keyword_terms(message, stop):
        if (t.isdigit() or len(t) < 3 or t in _NOT_TOPIC or t in _ACTIVITY_WORDS
                or t in _PERIOD_WORDS or t in alias_words or datetime_signal(t)):
            continue
        out.append(t)
    return out


def is_details_reply(known: list[farm.PlantInfo], text: str) -> bool:
    """'50 ml per 15 litre, savare' completes the previewed note; 'badha aamba ma neem spray 50 ml'
    is a NEW note and must not be glued on. Details are short, carry a number or a time-of-day
    word, and name no plant."""
    if len(text.split()) > 8 or farm.resolve_plants(known, text) is not None:
        return False
    return bool(re.search(r"\d", text)) or bool(time_of_day_in(text))


_NOT_A_PLANT = {"all", "badha", "badhu", "badhi", "chhod", "chod", "zad", "plant", "plants",
                "tree", "trees", "farm", "khet", "wadi", "vadi", "fal", "fruit", "pan", "leaf",
                "leaves", "phool", "flower", "mul", "root", "dal", "branch"}


def unknown_plant(known: list[farm.PlantInfo], named: str, message: str, args: dict) -> str:
    """The plant the user named that is NOT in the plants list ("dadam"), else "". The model only
    points at the word; it counts only if the user wrote it, and it is not a product, a part of a
    plant or a word the list already knows."""
    named = named.strip()
    w = words(named)
    if not named or len(w) > 2 or not grounded_words(named, message, min_len=3):
        return ""
    if farm.resolve_plants(known, named) is not None:
        return ""
    product = set(words(str(args.get("product") or "")))
    if any(t in _NOT_A_PLANT or t in _ACTIVITY_WORDS or t in product for t in w):
        return ""
    return " ".join(x for x in w if x in set(words(message)))[:30]


def _plant_warning(ctx: "Context", match) -> str:
    """A plant picked from the buttons while the note names a plant that is not in the list:
    said out loud, because this is how a pomegranate note became a guava note (29-10)."""
    named = getattr(ctx, "unknown_plant", "")
    if not named or not match or match.all_farm:
        return ""
    if farm.resolve_plants(farm.plants(ctx.session), named) is not None:
        return ""       # added to the list since the question: it is a real plant now
    return (f"⚠️ Your note says \"{named}\", but it will be saved under "
            f"{farm.plant_label(match.plants)}. Cancel and ask the admin to add {named} if that is wrong.")


def is_plant_reply(known: list[farm.PlantInfo], text: str) -> bool:
    return len(text.split()) <= 5 and farm.resolve_plants(known, text) is not None


def plant_buttons(known: list[farm.PlantInfo], add: str = "") -> Buttons:
    # Telegram refuses callback data over 64 bytes (and the whole message with it).
    names = [n for n in farm.plant_names(known) if len(f"plant:{n}".encode()) <= 64][:8]
    rows = [[(n, f"plant:{n}") for n in names[i:i + 3]] for i in range(0, len(names), 3)]
    # The name is NOT in the button: it is read back from the waiting question, so a forged
    # button can't add a plant called anything.
    extra = [[(f"➕ Add {add[:20]}", "plant:+add")]] if add else []
    return extra + rows + [[("All plants", "plant:all")]]


def unknown_plant_text(name: str, is_admin: bool) -> str:
    who = ("Tap ➕ to add it to the list" if is_admin
           else "Only the admin can add a plant — ask them")
    return f"🌳 \"{name}\" is not in the plant list. {who}, or pick the plant below."


ASK_PLANT = "🌳 Which plant is this about? Tap one below, or type it (e.g. aamba, chiku, badha chhod)."


# ── Tool descriptions ───────────────────────────────────────────────────────────────────────

def _s(type_: str, description: str = "", **extra) -> dict:
    out = {"type": type_, **extra}
    if description:
        out["description"] = description
    return out


def specs() -> list[ToolSpec]:
    # "planned" is written only by Farm Salah after ✅, never chosen by the model.
    acts = [a for a in farm.ACTIVITIES if a != "planned"]
    return [
        ToolSpec(LOG,
                 "PROPOSE saving something that was DONE or SEEN on the farm: planting, spraying, "
                 "fertilizer/khatar, treatment, watering, pruning, harvest, a plant that died, a "
                 "problem noticed on leaves or fruit. This does NOT save — the user taps Save first. "
                 "Use for 'aaje humic acid aapyu badha chhod ma', '2 aamba sukai gaya', 'pan par "
                 "fodla, neem spray karyu', 'south side 10 lemon ropya'. Copy only what the user "
                 "wrote: never invent a plant, product, dose, time or date. Leave a field empty if "
                 "it was not said — the system asks for what matters. If the message is ONLY money "
                 "spent, use add_expenses instead. Not for a plan or advice — use save_farm_advice. "
                 "A plain STATEMENT of a problem seen, with no question in it, is a note to save "
                 "here, even when nothing was done yet: 'limbu na pan pila padya che', 'aamba par "
                 "daag dekhay che'.",
                 _s("OBJECT", properties={
                     "activity": _s("STRING", "What happened. 'observation' for a problem only seen, "
                                              "'dead' for plants that died.", enum=acts),
                     "plant": _s("STRING", "Plant(s) as the user named them. Empty if not named."),
                     "location": _s("STRING", "Where on the farm, if said. Empty otherwise."),
                     "plant_qty": _s("INTEGER", "How many plants, only if a number of plants is written."),
                     "product": _s("STRING", "Product used (neem, humic acid, DAP...), only if said."),
                     "dose": _s("STRING", "Dose exactly as written, e.g. '50 ml per 15 litre'. Empty if not said."),
                     "time_of_day": _s("STRING", "morning/afternoon/evening/night, only if said."),
                     "problem": _s("STRING", "Problem seen, in the user's words. Empty if none."),
                     "date": _s("STRING", "Date it happened, DD-MM-YYYY. Empty if not said (means today)."),
                     "date_to": _s("STRING", "End date for a range like '2 thi 8 oct'. Empty otherwise."),
                 }, required=["activity"])),
        ToolSpec(ADVICE,
                 "PROPOSE saving ADVICE, a PLAN or a general RULE for the farm that the user WROTE "
                 "out — something to follow, not something done on a particular day. E.g. "
                 "'watering plan: plantation pachhi pehla athvadiye roj paani...', 'farmer kahe "
                 "che phool aave tyare 0:52:34 aapvu'. Saved in the user's own words after they "
                 "tap Save. If it says what WAS done ('aaje paani aapyu'), use log_farm_activity "
                 "instead. If the user ASKS for a plan to be made ('spray plan banao'), that is "
                 "farm_salah, not this.",
                 _s("OBJECT", properties={
                     "title": _s("STRING", "A short title in English, at most 6 words."),
                 }, required=["title"])),
        ToolSpec(RESULT,
                 "PROPOSE recording how an earlier spray/treatment/fertilizer turned out: 'copper "
                 "pachhi daag band thai gaya', 'neem thi kai fark na padyo'. The system finds the "
                 "matching earlier treatment and asks the user which one.",
                 _s("OBJECT", properties={
                     "outcome": _s("STRING", "worked = better, partly = some change, not_worked = no change.",
                                   enum=["worked", "partly", "not_worked"]),
                     "result": _s("STRING", "What happened, in the user's words, short."),
                     "plant": _s("STRING", "Plant as the user named it, if named."),
                 }, required=["outcome", "result"])),
        ToolSpec(DELETE,
                 "PROPOSE deleting the sender's own most recent farm note (last 48 hours): 'farm "
                 "note undo', 'chhelli farm note delete karo'. Not for expenses — that is "
                 "delete_last_expense."),
        ToolSpec(ACTIVITIES_Q,
                 "EXACT lookup in the farm journal: when something was last done, or a list of what "
                 "was done. Use for 'humic chhelle kyare aapyu?', 'October ma shu shu karyu?', "
                 "'lemon ma kyare spray karyo?'. Pass date_from/date_to (DD-MM-YYYY) for any period. "
                 "Not for 'what did we do when leaves had spots' — that is search_farm_notes. If "
                 "the same message also asks what was SPENT on that day ('chhelle kyare paani "
                 "aapyu? e divase majuri ketli?'), that is farm_salah, not this.",
                 _s("OBJECT", properties={
                     "plant": _s("STRING", "Plant as the user named it. Empty for all."),
                     "activity": _s("STRING", "Only this activity, if asked.", enum=acts),
                     "product": _s("STRING", "Only notes with this product, e.g. 'humic'."),
                     "date_from": _s("STRING", "Start DD-MM-YYYY, inclusive."),
                     "date_to": _s("STRING", "End DD-MM-YYYY, inclusive."),
                     "last_only": _s("BOOLEAN", "True for 'when was it LAST done'."),
                     "topic_en": _s("STRING", "What the question is about, in plain English, without "
                                              "plant or time words: 'udhai kyare karyu?' -> 'termite'. "
                                              "Empty if there is no topic."),
                 })),
        ToolSpec(COUNTS,
                 "How many plants are alive, per plant: 'ketla aamba che?', 'plant count'.",
                 _s("OBJECT", properties={"plant": _s("STRING", "Plant as named, or empty for all.")})),
        ToolSpec(SEARCH,
                 "MEANING search in the farm notes and the farmer's advice, for questions answered "
                 "by past experience or advice: 'pehla daag padya tyare shu karyu?', 'fungus mate "
                 "shu kaam karyu?', 'nava chhod ne paani ketlu aapvu?'. Only for a QUESTION: a "
                 "statement of what is seen ('limbu na pan pila padya che') is log_farm_activity. "
                 "Pass the user's question "
                 "unchanged as query. Add plant / dates / activity only if the user said them.",
                 _s("OBJECT", properties={
                     "query": _s("STRING", "The user's question, unchanged."),
                     "query_en": _s("STRING", "The same question in plain English."),
                     "plant": _s("STRING", "Plant as the user named it, if named."),
                     "activity": _s("STRING", "Only if the user restricted it.", enum=acts),
                     "date_from": _s("STRING", "Start DD-MM-YYYY, only if a period was said."),
                     "date_to": _s("STRING", "End DD-MM-YYYY, only if a period was said."),
                 }, required=["query"])),
    ]


SYSTEM_RULES = (
    "FARM JOURNAL: log_farm_activity, save_farm_advice, record_treatment_result and "
    "delete_last_farm_note only PROPOSE — never say a farm note is saved. Something done or seen "
    "on the farm -> log_farm_activity (and also add_expenses only if money was spent); a "
    "statement of a problem seen, with no question in it ('limbu na pan pila padya che'), is "
    "such a note, never a search. Exact "
    "questions (when, how many times, which product, how many plants) -> get_farm_activities / "
    "get_plant_counts. Questions answered by past experience or the farmer's advice -> "
    "search_farm_notes. A request to MAKE a plan, or a question whose second part needs the "
    "first part's answer (which medicine worked AND when it was bought; WHEN something was done "
    "AND what was spent that day, for any farm work: spray, watering, fertilizer) -> farm_salah. Never answer a farm question from your own knowledge, "
    "and never invent a plant, product, dose, time or date. "
)


# ── Tools ───────────────────────────────────────────────────────────────────────────────────

def execute(ctx: "Context", name: str, args: dict, message: str) -> dict:
    known = farm.plants(ctx.session)
    if name == LOG:
        return _log(ctx, known, args, message)
    if name == ADVICE:
        return _advice(args, message, ctx.source)
    if name == RESULT:
        return _result(ctx, known, args, message)
    if name == DELETE:
        e = farm.last_entry_by(ctx.session, ctx.sender)
        if e is None:
            return {"text_reply": f"No farm note by {ctx.sender.name} in the last 48 hours. Older "
                                  "notes can't be deleted from chat."}
        return {"proposal": {"kind": "farm_delete", "id": e.id, "line": farm.journal_line(e)}}
    if name == ACTIVITIES_Q:
        return _activities(ctx, known, args, message)
    if name == COUNTS:
        return {"text_reply": counts_text(ctx, known, message)}
    if name == SEARCH:
        return _search(ctx, known, args, message)
    return {"error": f"Unknown tool: {name}"}


def _log(ctx: "Context", known, args: dict, message: str) -> dict:
    activity = str(args.get("activity") or "").strip().lower()
    if activity not in farm.ACTIVITIES:
        return {"error": f"activity must be one of {', '.join(farm.ACTIVITIES)}. Call again."}
    match = farm.resolve_plants(known, message)
    if match is None:
        unknown = unknown_plant(known, str(args.get("plant") or ""), message, args)
        out = {"error": "The user did not name a plant. Nothing was saved; the system asks "
                        "which plant.", "needs_plant": True}
        if unknown:
            out["unknown_plant"] = unknown
        return out

    raw_date = str(args.get("date") or "").strip()
    if raw_date and not period_signal(message):
        raw_date = ""            # the message says nothing about when: the model made a date up
    on = ctx.today
    if raw_date:
        d = parse_date(raw_date)
        if d is None:
            return {"error": f"date '{raw_date}' is not DD-MM-YYYY. Call again with a valid date."}
        if d > ctx.today:
            return {"error": f"{raw_date} is in the future (today is {fmt_date(ctx.today)}). Farm "
                             "notes are about things already done. If the user meant last year, "
                             "call again with last year's date."}
        if (ctx.today - d).days > 3 * 365:
            return {"error": f"{raw_date} is more than 3 years ago — almost certainly a wrong "
                             "year. Call again with the correct date."}
        on = d
    raw_to = parse_date(str(args.get("date_to") or "").strip())
    date_to = raw_to if raw_to and on < raw_to <= ctx.today else None

    qty = None
    try:
        q = int(float(args.get("plant_qty") or 0))
        if q > 0 and str(q) in numbers_in(message):
            qty = q
    except (TypeError, ValueError):
        pass
    dose = str(args.get("dose") or "").strip()
    if dose and (not numbers_in(dose) or not grounded_numbers(dose, message)):
        dose = ""
    product = str(args.get("product") or "").strip()
    if product and not grounded_words(product, message):
        product = ""
    problem = str(args.get("problem") or "").strip()
    if problem and not grounded_words(problem, message, min_len=2):
        problem = ""
    location = str(args.get("location") or "").strip()
    if location and not grounded_words(location, message, min_len=2):
        location = ""

    p = {"kind": "farm_note", "activity": activity, "plants": match.plants,
         "warning": _plant_warning(ctx, match),
         "variety": match.variety, "location": location, "qty": qty, "product": product,
         "dose": dose, "time_of_day": time_of_day_in(message), "problem": problem,
         "date": on.isoformat(), "date_to": date_to.isoformat() if date_to else "",
         "note": (ctx.note_text or message).strip(), "photo": ctx.photo, "source": ctx.source,
         "source_message": message.strip()}
    missing = []
    if activity in farm.TREATMENTS:
        if not dose:
            missing.append("dose")
        if not p["time_of_day"]:
            missing.append("time")
    if activity in farm.COUNTED and qty is None and not match.all_farm:
        missing.append("qty")
    p["missing"] = missing
    # Asked ONCE. A second round of questions turns a 5-second note into a chore, and a chore is
    # how a journal stops being written.
    p["ask_details"] = bool(missing) and not ctx.details_asked
    return {"proposal": p}


def _advice(args: dict, message: str, source: str) -> dict:
    """The saved text is the USER's message, never the model's rewrite (a rewrite could turn
    '3-4 divase' into '3 days'). Only the short title is the model's, and it is checked."""
    t = message.strip()
    if len(t) < farm.ADVICE_MIN_CHARS:
        return {"error": "Too short to be advice. Nothing was saved. Ask the user to write the full advice."}
    return {"proposal": advice_proposal(t, str(args.get("title") or ""), source)}


def advice_proposal(text: str, title: str = "", source: str = "text", *,
                    title_from_model: bool = True) -> dict:
    title = " ".join(title.split())[:60]
    if not title or (title_from_model and any(ch.isdigit() for ch in title)):
        title = " ".join(text.split()[:6])[:60]      # no invented numbers in a title either
    return {"kind": "farm_advice", "title": title, "text": text,
            "source": "voice" if source == "voice" else "text"}


def _result(ctx: "Context", known, args: dict, message: str) -> dict:
    outcome = str(args.get("outcome") or "").strip()
    if outcome not in OUTCOME_LABEL:
        return {"error": "outcome must be worked, partly or not_worked. Call again."}
    result = str(args.get("result") or "").strip() or message.strip()
    if not grounded_words(result, message, min_len=2):
        result = message.strip()      # the model's summary isn't the user's words: keep theirs
    plant = farm.single_plant(farm.resolve_plants(known, message))
    cands = farm.open_treatments(ctx.session, ctx.today, plant)[:3]
    if not cands:
        where = f" for {plant}" if plant else ""
        return {"text_reply": f"📒 No spray, treatment or fertilizer without a result in the last 90 "
                              f"days{where}. Nothing was saved — send it as a farm note instead."}
    return {"proposal": {"kind": "farm_result", "outcome": outcome, "result": result,
                         "choices": [{"id": e.id, "line": farm.journal_line(e, with_result=False)}
                                     for e in cands]}}


def _period(args: dict, message: str, today: date):
    if not period_signal(message):
        return None, None, "", ""
    return rag.resolve_dates(args.get("date_from"), args.get("date_to"), today)


def _meaning_lookup(ctx: "Context", topic: list[str], topic_en: str, plant, variety, activity,
                    df, dt) -> tuple[list, str]:
    """Exact words found nothing for this topic: find notes by MEANING ('termite' finds the
    'udhai' note), then answer from the journal rows. The model never writes a date here."""
    f = rag.Filters(plant=plant or "", variety=variety or "", activity=activity or "",
                    date_from=df, date_to=dt, journal_only=True)
    # The TOPIC only, not the whole question: "aamba ma termite kyare?" would otherwise match any
    # mango note on the word "aamba" — the plant is already a filter.
    found = rag.retrieve(ctx.session, ctx.llm, " ".join(topic), f, _min_score(ctx), alt=topic_en)
    log.info("farm lookup by meaning", extra={"fields": {"q": " ".join(topic)[:120],
                                                         "mode": found.mode, "trace": found.trace}})
    if found.mode != "vector":
        return [], "meaning search is unavailable right now"
    if found.relaxed:
        return [], ""        # matched only after dropping the user's plant/date: not an answer
    rows = []
    for h in found.hits:
        if not h.id.startswith("J-"):
            continue
        e = farm.get_entry(ctx.session, int(h.id[2:]))
        # The search copy can lag the table (edited, not yet re-indexed). The date printed must
        # belong to the same text that matched, so a stale hit is skipped, never trusted.
        if e and farm.journal_doc_text(e) == h.text and (not activity or e.activity == activity):
            rows.append(e)
    rows.sort(key=lambda e: (e.entry_date, e.id), reverse=True)
    return rows, ""


# "When was it LAST done" — only these words make the answer one row. Live bug 29-10: "neem oil
# kyare chhantyu?" got the model's last_only=True, so 1 of 2 sprays was shown and the failed
# chiku spray was hidden.
_LAST_WORDS = re.compile(r"\b(chhell\w*|chell\w*|chhel\w*|last|latest|recent\w*|most recent|"
                         r"aakhri|akhri|chhevte|chevte)\b|છેલ્લ|આખરી", re.IGNORECASE)


def asks_for_last(message: str) -> bool:
    return bool(_LAST_WORDS.search(message or ""))


def _activities(ctx: "Context", known, args: dict, message: str) -> dict:
    match = farm.resolve_plants(known, message)
    plant = farm.single_plant(match)
    variety = match.variety if plant and match and match.variety not in ("", ALL) else None
    several = match.plants if match and not match.all_farm and len(match.plants) > 1 else []
    activity = str(args.get("activity") or "").strip().lower() or None
    if activity not in farm.ACTIVITIES:
        activity = None
    topic = topic_words(known, message, rag_index.stopwords(ctx.session))
    product = str(args.get("product") or "").strip()
    if product and not grounded_words(product, message):
        product = ""       # the model's word isn't the user's; the user's own topic words decide
    if product:
        topic = [product] + [t for t in topic if t not in words(product)]
    df, dt, note, err = _period(args, message, ctx.today)
    if err:
        return {"error": err + ". Call again with DD-MM-YYYY dates."}
    return {"text_reply": activities_text(ctx, plant=plant, variety=variety, several=several,
                                          activity=activity, topic=topic, df=df, dt=dt, note=note,
                                          topic_en=str(args.get("topic_en") or "").strip()[:120],
                                          last_only=bool(args.get("last_only"))
                                          and asks_for_last(message))}


def activities_text(ctx: "Context", *, plant=None, variety=None, several=(), activity=None,
                    topic=(), df=None, dt=None, note="", topic_en="", last_only=False) -> str:
    rows = farm.query(ctx.session, plant=plant, variety=variety, activity=activity,
                      date_from=df, date_to=dt)
    if several:      # "aamba ane chiku" -> notes for either plant
        rows = [e for e in rows if any(farm.matches_plant(e, p) for p in several)]
    source, unavailable = SRC_EXACT, ""
    if topic:
        exact = [e for e in rows if any(farm.mentions(e, t) for t in topic)]
        english = [e for e in rows if topic_en and farm.mentions_in_english(e, topic_en)]
        if exact:
            rows = exact
        elif english:
            rows, source = english, SRC_EXACT_EN
        else:
            meant, unavailable = _meaning_lookup(ctx, list(topic), topic_en, plant, variety,
                                                 activity, df, dt)
            if several:
                meant = [e for e in meant if any(farm.matches_plant(e, p) for p in several)]
            rows, source = meant, SRC_HYBRID

    # The heading is the user's own frame (plant, activity), never the search words: those come
    # from the model and can be the other half of the question ("divase / majuri / kharch"),
    # which reads as noise and suggests that half was looked at. They go in the "not found" line.
    what = " · ".join(x for x in [
        farm.plant_label(list(several)) if several else farm.plant_label([plant], variety or "") if plant else "",
        ACTIVITY_LABEL.get(activity, "") if activity else ""] if x) or "farm"
    period = ""
    if df or dt:
        period = f" ({fmt_date(df) if df else '…'} to {fmt_date(dt) if dt else 'today'}"
        period += f", {note})" if note else ")"
    if not rows:
        searched = "Searched the journal by exact words" + (" and your notes by meaning" if topic else "")
        searched += f" for: {', '.join(topic)}" if topic else ""
        why = f" ({unavailable})" if unavailable else ""
        return f"📒 No {what} note found{period}.\n{searched}{why}."
    if last_only:
        e = rows[0]
        ago = (ctx.today - e.entry_date).days
        when = " (today)" if ago == 0 else f" ({ago} day{'s' if ago != 1 else ''} ago)"
        older = len(rows) - 1
        more = (f"\n…{older} older note{'s' if older != 1 else ''} too — ask without \"last\" to "
                "see all." if older else "")
        return f"📒 Last {what}{period}{when}:\n• {farm.journal_line(e)}{more}\n{source}"
    # Newest first, at most 5: enough to see the pattern (what was tried, what worked) on a phone.
    shown = rows[:5]
    text = (f"📒 {what[:1].upper() + what[1:]} notes{period} — {len(rows)} found:\n"
            + "\n".join("• " + farm.journal_line(e) for e in shown))
    if len(rows) > len(shown):
        text += f"\n…and {len(rows) - len(shown)} older."
    return text + "\n" + source


def counts_text(ctx: "Context", known, message: str = "") -> str:
    match = farm.resolve_plants(known, message) if message else None
    only = set(match.plants) if match and not match.all_farm else None
    rows = [r for r in farm.live_counts(ctx.session) if not only or r.plant in only]
    if not rows:
        return "🌳 No plant counts are set yet."
    lines = []
    for r in rows:
        change = []
        if r.added:
            change.append(f"+{r.added} planted")
        if r.dead:
            change.append(f"−{r.dead} died")
        lines.append(f"• {r.plant}: {r.alive}" + (f"  ({r.baseline} {', '.join(change)})" if change else ""))
        if r.unknown_qty:
            lines.append(f"   ⚠️ {r.unknown_qty} note(s) without a number of plants are not counted")
    head = "🌳 Plants alive" + ("" if only else f" — {sum(r.alive for r in rows)} total")
    return head + ":\n" + "\n".join(lines) + "\n" + SRC_COUNT


def _min_score(ctx: "Context") -> float:
    return ctx.settings.rag_min_score if ctx.settings else 0.62


def _search(ctx: "Context", known, args: dict, message: str) -> dict:
    """The question searched is the USER's message, not the model's rewrite: a rewrite can drop
    the one word ('fodla') that finds the note."""
    question = message.strip() or str(args.get("query") or "").strip()
    match = farm.resolve_plants(known, question)
    f = rag.Filters()
    plant = farm.single_plant(match)
    if plant:
        f.plant = plant
        if match.variety not in ("", ALL):
            f.variety = match.variety
    act = str(args.get("activity") or "").strip().lower()
    if act in farm.ACTIVITIES and act in question.lower():
        f.activity = act
    if period_signal(question) and (args.get("date_from") or args.get("date_to")):
        df, dt, _note, err = rag.resolve_dates(args.get("date_from"), args.get("date_to"), ctx.today)
        if not err:
            f.date_from, f.date_to = df, dt

    found = rag.retrieve(ctx.session, ctx.llm, question, f, _min_score(ctx),
                         alt=str(args.get("query_en") or "").strip())
    log.info("farm search", extra={"fields": {"q": question[:120], "mode": found.mode,
                                              "trace": json.loads(json.dumps(found.trace, default=str))}})
    missing = ""
    if found.mode == "basic":
        if found.hits:
            return {"text_reply": rag.basic_reply(found) + "\n" + rag.how_line(found, f)}
    else:
        ans = rag.answer(ctx.llm, question, found)
        if ans.status != "not_found":
            outs = [Photo(fid, cap) for fid, cap in ans.photos]
            return {"text_reply": ans.reply + "\n" + rag.how_line(found, f, ans.status),
                    "outs": outs, "cited": ans.cited}
        missing = ans.missing

    text = "📒 No note found about this in the farm journal or the farmer's advice."
    if missing:
        text += f"\n(Not written anywhere: {missing})"
    if rag.is_dose_question(question):
        return {"text_reply": text + "\nFor doses, please follow the product label or ask the farmer."}
    pending.put(ctx.session, ctx.chat_id, ctx.sender.id, pending.GENERAL, {"question": question},
                ttl=GENERAL_TTL)
    return {"text_reply": text + "\nTap 💡 General advice for a general answer (not from your notes).",
            "buttons": [[("💡 General advice", "advice")]]}


# ── After "yes" ─────────────────────────────────────────────────────────────────────────────

def _weekday_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{WEEKDAY_SHORT[d.weekday()]} {fmt_date(d)}"


def note_summary(p: dict) -> str:
    """Every field the row will hold, because this is the last chance to catch a misheard plant
    or a wrong date before it becomes farm history."""
    lines = [f"📅 {_weekday_date(p['date'])}" + (f" – {_weekday_date(p['date_to'])}" if p.get("date_to") else "")]
    where = farm.plant_label(p["plants"], p.get("variety", ""))
    if p.get("qty") is not None:
        where += f" · {p['qty']} plants"
    if p.get("location"):
        where += f" · {p['location']}"
    lines.append(f"🌳 {where}")
    what = ACTIVITY_LABEL.get(p["activity"], p["activity"])
    for k in ("product", "dose", "time_of_day"):
        if p.get(k):
            what += f" — {p[k]}"
    lines.append(f"🧪 {what}")
    if p.get("problem"):
        lines.append(f"⚠️ Problem: {p['problem']}")
    if p.get("warning"):
        lines.append(p["warning"])
    if p.get("photo"):
        lines.append("📷 Photo attached")
    return "\n".join(lines)


_DETAIL_LABEL = {"dose": "dose", "time": "time (savare/saanje)", "qty": "number of plants"}


def _note_preview(p: dict) -> str:
    text = f"🌱 Save this farm note?\n{note_summary(p)}"
    if p.get("missing"):
        need = ", ".join(_DETAIL_LABEL.get(m, m) for m in p["missing"])
        if p.get("ask_details"):
            text += (f"\n\n➕ Not given: {need}.\nType it now (e.g. \"50 ml per 15 litre, savare\") "
                     "to add it, or tap ✅ Save as it is.")
        else:
            text += f"\n(Not given: {need})"
    return text + "\n\n" + proposals.FOOTER


def _note_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    e = farm.add_entry(ctx.session, p, ctx.sender)
    rag_index.index_rows(ctx.session, ctx.llm, "journal", {e.id})
    extra = "\n🔔 I'll ask how it went in 7 days." if p["activity"] in farm.TREATMENTS else ""
    return [Reply(f"✅ Saved farm note J-{e.id}\n{note_summary(p)}{extra}")]


def _note_other_text(ctx: "Context", p: dict, text: str) -> list[Out] | None:
    """A reply that gives the missing dose / time is glued to the original note and goes through
    every check again, once. Anything else is a new message."""
    if not p.get("ask_details") or not is_details_reply(farm.plants(ctx.session), text):
        return None
    from app.ai import agent     # here: agent imports this module
    ctx.details_asked, ctx.photo, ctx.source = True, p.get("photo", ""), p.get("source", "text")
    # The dose / time is the user's own words and belongs in the note; a picked plant does not.
    ctx.note_text = f"{p['note']} {text.strip()}"
    outcome = agent.handle(ctx, f"{p['source_message']} {text}")
    return [Reply(outcome.reply, outcome.buttons), *outcome.extras]


def _result_line(p: dict) -> str:
    return f"{OUTCOME_LABEL[p['outcome']]}: {p['result']}"


def _result_preview(p: dict) -> str:
    if len(p["choices"]) == 1:
        return (f"📝 Save this result?\nFor: {p['choices'][0]['line']}\n↳ {_result_line(p)}\n\n"
                + proposals.FOOTER)
    rows = "\n".join(f"{i}. {c['line']}" for i, c in enumerate(p["choices"], start=1))
    return f"📝 Which treatment is this result for?\n{rows}\n↳ {_result_line(p)}\n\nTap the number, or ❌ Cancel."


def _result_buttons(p: dict) -> Buttons:
    if len(p["choices"]) == 1:
        return yes_no()
    return [[(str(i), str(i)) for i in range(1, len(p["choices"]) + 1)], [("❌ Cancel", "no")]]


def _result_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    target = p["choices"][pick or 0]
    e = farm.get_entry(ctx.session, target["id"])
    if e is None:
        return [Reply(f"J-{target['id']} no longer exists. Nothing was saved.")]
    if e.result:
        return [Reply(f"J-{e.id} already has a result:\n↳ {e.result}\nNothing was changed.")]
    farm.set_result(ctx.session, e.id, _result_line(p), ctx.today)
    rag_index.index_rows(ctx.session, ctx.llm, "journal", {e.id})
    return [Reply(f"✅ Result saved on J-{e.id}\n{target['line']}\n↳ {_result_line(p)}")]


def _advice_preview(p: dict) -> str:
    shown = p["text"] if len(p["text"]) <= 600 else p["text"][:600] + "…"
    head = "🎤 Save this as farmer advice, so it can be searched later?" if p.get("source") == "voice" \
        else "📚 Save this as farmer advice?"
    return (f"{head}\n{p['title']}\n\"{shown}\"\n\nIt is saved in your own words, not as a farm "
            f"note for today.\n" + ("Tap a button below." if p.get("source") == "voice" else proposals.FOOTER))


def _advice_buttons(p: dict) -> Buttons:
    if p.get("source") == "voice":
        # A long voice note is shown and kept until a button decides — never saved, and never
        # run as commands, on a guess.
        return [[("📚 Save as farmer advice", "yes")],
                [("▶️ Treat as a normal message", "kn:msg"), ("❌ Cancel", "no")]]
    return yes_no()


def _advice_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    k = farm.add_knowledge(ctx.session, p["title"], p["text"], ctx.sender, p.get("source", "text"),
                           ctx.today)
    indexed = rag_index.index_rows(ctx.session, ctx.llm, "knowledge", {k.id})
    parts = len(farm.chunk_text(p["text"]))
    late = "" if indexed else "\n(The search index will pick it up in the daily sync.)"
    return [Reply(f"📚 Saved as farmer advice K-{k.id} ({parts} part{'s' if parts != 1 else ''})\n"
                  f"{p['title']}{late}")]


def _delete_preview(p: dict) -> str:
    return f"🗑️ Delete this farm note?\n{p['line']}\n\nTap ✅ Delete or ❌ Cancel (or reply yes / no)."


def _delete_apply(ctx: "Context", p: dict, pick: int | None) -> list[Out]:
    e = farm.get_entry(ctx.session, p["id"])
    if e is None or e.added_by != ctx.sender.id:
        return [Reply("That note is already gone. Nothing was changed.")]
    farm.delete_entry(ctx.session, e.id)
    return [Reply(f"🗑️ Deleted farm note J-{p['id']}\n{p['line']}")]


proposals.register("farm_note", proposals.Kind(_note_preview, _note_apply,
                                               on_other_text=_note_other_text))
proposals.register("farm_result", proposals.Kind(_result_preview, _result_apply, _result_buttons))
proposals.register("farm_advice", proposals.Kind(_advice_preview, _advice_apply, _advice_buttons))
proposals.register("farm_delete", proposals.Kind(_delete_preview, _delete_apply,
                                                 lambda p: yes_no("✅ Delete")))


# ── 7-day follow-up (a tapped button: one meaning, no model) ────────────────────────────────

FOLLOW_UP_BUTTONS = {"w": "worked", "p": "partly", "n": "not_worked", "u": "unknown"}


def followup_buttons(jid: int) -> Buttons:
    return [[("✅ Better", f"fu:{jid}:w"), ("➖ Partly", f"fu:{jid}:p")],
            [("❌ No change", f"fu:{jid}:n"), ("🤷 Don't know", f"fu:{jid}:u")]]


def followup_answer(ctx: "Context", jid: int, outcome: str) -> list[Out]:
    e = farm.get_entry(ctx.session, jid)
    if e is None:
        return [Reply(f"J-{jid} no longer exists. Nothing was saved.")]
    if e.result:
        return [Reply(f"J-{jid} already has a result:\n↳ {e.result}")]
    if outcome == "unknown":
        farm.mark_followup(ctx.session, jid, "skipped")
        return [Reply(f"👍 OK — no result recorded for J-{jid}.")]
    text = f"{OUTCOME_LABEL[outcome]} (7-day check by {ctx.sender.name})"
    farm.set_result(ctx.session, jid, text, ctx.today)
    rag_index.index_rows(ctx.session, ctx.llm, "journal", {jid})
    return [Reply(f"✅ Result saved on J-{jid}\n{farm.journal_line(e, with_result=False)}\n↳ {text}\n"
                  "Add details any time, e.g. \"copper pachhi daag band\".")]


def general_answer(ctx: "Context") -> list[Out]:
    """The 💡 button, valid only right after a search found nothing."""
    waiting, expired = pending.pop(ctx.session, ctx.chat_id, ctx.sender.id, pending.GENERAL)
    if waiting is None or expired:
        return [Reply("Ask the question again first, then tap 💡 General advice.")]
    return [Reply(rag.general_advice(ctx.llm, waiting.payload["question"]))]


# ── Journal -> expenses: the second hop, one tap ────────────────────────────────────────────
# "Kesar na daag ni dava no kharcho?" names the PROBLEM; the expense names the PRODUCT. The
# journal links the two. Until a graph does this hop on its own, code finds the link and the
# user confirms it with one tap — a wrong link costs a tap, not a wrong total.

_MONEY_WORDS = {strip_marks(w).lower() for w in (
    "kharcho", "kharch", "kharchi", "kharchu", "ketlo", "ketla", "ketlu", "paisa", "rupiya",
    "cost", "costs", "spent", "spend", "price", "total", "amount", "bill", "expense", "expenses",
    "dava", "dawa", "davani", "medicine", "kimat", "lagya", "lagyo", "thayo", "nu", "ni", "no",
    "ખર્ચ", "ખર્ચો", "કેટલો", "દવા")}

HOP = re.compile(r"^xs:(\d{1,9})$")


def expense_hop(ctx: "Context", message: str) -> tuple[str, Buttons] | None:
    """The journal note this money question is about, if its words point to exactly the notes
    that name a product. Buttons search expenses for that product."""
    known = farm.plants(ctx.session)
    topic = [t for t in topic_words(known, message, rag_index.stopwords(ctx.session))
             if t not in _MONEY_WORDS]
    if not topic:
        return None
    match = farm.resolve_plants(known, message)
    plant = farm.single_plant(match)
    scored = []
    for e in farm.query(ctx.session, plant=plant):
        if not e.product.strip():
            continue
        score = sum(1 for t in topic if farm.mentions(e, t))
        if score:
            scored.append((score, e))
    if not scored:
        return None
    best = max(sc for sc, _ in scored)
    seen, picks = set(), []
    for _, e in sorted(((sc, e) for sc, e in scored if sc == best), key=lambda x: -x[1].id):
        if e.product.lower() not in seen:
            seen.add(e.product.lower())
            picks.append(e)
    picks = picks[:2]
    lines = "\n".join(f"📒 J-{e.id} ({fmt_date(e.entry_date)}): {e.problem or e.note[:40]} → "
                      f"{e.product}" for e in picks)
    buttons = [[(f"🔎 {e.product[:28]} in expenses", f"xs:{e.id}")] for e in picks]
    return f"The journal links this to:\n{lines}\nTap to search expenses for it.", buttons


def hop_answer(ctx: "Context", entry_id: int) -> list[Out]:
    """The tapped hop: expenses for the product of journal note J-<id>. The product comes from the
    database, never from the button."""
    from app.ai import agent       # here: agent imports this module
    from app.services import expenses
    e = ctx.session.get(farm.JournalEntry, entry_id)
    if e is None or not e.product.strip():
        return [Reply("This button is no longer valid.")]
    rows = expenses.search(ctx.session, text=e.product)
    path = (f"🔎 Path: 📒 J-{e.id} ({e.problem or 'note'}) → 💊 {e.product} → 💰 expenses "
            "(journal: exact words, money: exact match)")
    if not rows:
        first = e.product.split()[0]
        rows = expenses.search(ctx.session, text=first) if first.lower() != e.product.lower() else []
        if rows:
            path = path.replace(f"💊 {e.product}", f"💊 {first}")
    body = (agent.search_text(rows, expenses.total(rows)) if rows
            else f"💰 No expense with \"{e.product}\" in its description.")
    return [Reply(f"{body}\n{path}")]

