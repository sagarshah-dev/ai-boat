"""Farm journal evals against the REAL Gemini models. Not deployed.

tests/ fake the embeddings, so they prove the PIPELINE. This proves the MODELS:
  1. ROUTING   — does the chat model pick the right farm tool for real messages?
  2. RETRIEVAL — does the real embedding model put the right note in the top 5? (Gujarati
                 script, romanised, English, synonyms like daag / fodla / fungus)
  3. ANSWER    — is the reply grounded, cited, and "not found" when it should be?
  4. THRESHOLD — similarity of the right note vs the best wrong note, per case, and a suggested
                 RAG_MIN_SCORE. The threshold is set from this table, never by feel.
Retrieval and answer are scored separately: a wrong reply caused by search is fixed in chunking,
filters or the threshold; one caused by the model is fixed in the prompt, form or checks.

Run:  GEMINI_API_KEY=... EVAL_DATABASE_URL=postgresql://... python evals/run_rag_evals.py
Env:  EVAL_DELAY_S (default 4), RAG_MIN_SCORE (to test a candidate threshold)
"""
import json
import os
import re
import time
from datetime import date

import harness

TODAY = date(2026, 11, 15)


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (harness.HERE / name).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def seed(llm) -> dict:
    """The notes keep their IDs from the file (J-6 in a case means note 6)."""
    from sqlalchemy import text
    from app.core.formatting import parse_date
    from app.db.models import JournalEntry, Knowledge
    from app.db.session import session_scope
    from app.services import rag_index
    ids = harness.reset()
    with session_scope() as s:
        for n in load("rag_notes.jsonl"):
            who = ids[n.get("AddedBy", "Sagar")]
            if "KNOWLEDGE" in n:
                s.add(Knowledge(id=n["KNOWLEDGE"], entry_date=parse_date(n["Date"]), title=n["Title"],
                                text=n["Text"], source="voice", added_by=who))
                continue
            s.add(JournalEntry(
                id=n["ID"], entry_date=parse_date(n["Date"]), date_to=parse_date(n.get("DateTo", "")),
                plant=n["Plant"], variety=n.get("Variety", ""), location=n.get("Location", ""),
                plant_qty=int(n["PlantQty"]) if n.get("PlantQty") else None, activity=n["Activity"],
                product=n.get("Product", ""), dose=n.get("Dose", ""), time_of_day=n.get("TimeOfDay", ""),
                problem=n.get("Problem", ""), result=n.get("Result", ""), note=n.get("Note", ""),
                source="text", added_by=who))
        s.flush()
        # Explicit IDs were inserted: move the sequences past them, as a restore would.
        for table in ("journal_entries", "knowledge"):
            s.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                           f"(SELECT max(id) FROM {table}))"))
    with session_scope() as s:
        return rag_index.sync(s, llm)


def filters_for(q: str):
    """Exactly what the search tool derives from a question (plant only; no dates in cases)."""
    from app.db.session import session_scope
    from app.services import farm, rag
    with session_scope() as s:
        plant = farm.single_plant(farm.resolve_plants(farm.plants(s), q))
    return rag.Filters(plant=plant or "")


def main() -> None:
    harness.configure()
    from app.config import get_settings
    from app.db.session import session_scope
    from app.integrations.gemini import GeminiLLM
    from app.services import rag
    from app.services.rag_index import keyword_terms
    settings = get_settings()
    min_score = float(os.getenv("RAG_MIN_SCORE") or settings.rag_min_score)
    rec = harness.Recorder(GeminiLLM(settings.gemini_api_key, settings.gemini_model,
                                     settings.gemini_fallback_model))
    tg = harness.Telegram()
    client = harness.app_client(rec, tg)
    harness.pin_today(TODAY)
    delay = float(os.getenv("EVAL_DELAY_S", "4"))

    print("indexing:", seed(rec))
    report = ["# Farm RAG eval", "", f"Embedding model: `{rec.embed_model}` · chat model: "
              f"`{settings.gemini_model}` · threshold under test: {min_score}", ""]

    # 1. Routing. Proposals are never confirmed, so nothing is saved.
    routing = load("farm_routing.jsonl")
    report += ["## 1. Routing", "", "| case | expected tool | got | ok |", "|---|---|---|---|"]
    route_ok = 0
    for case in routing:
        rec.calls.clear()
        try:
            harness.send(client, tg, "Sagar", case["text"])
            harness.clear_waiting()      # drop any preview / question: the next case starts clean
            tools = [c["name"] for c in rec.calls]
        except RuntimeError as err:
            tools = [f"ERROR {err}"[:60]]
        ok = case["tool"] in tools
        route_ok += ok
        report.append(f"| {case['id']} | {case['tool']} | {', '.join(tools) or '-'} | {'✅' if ok else '❌'} |")
        time.sleep(delay)

    # 2–4. Retrieval, answer, calibration.
    cases = load("rag_cases.jsonl")
    report += ["", "## 2-3. Retrieval and answer", "",
               "| case | retrieval | answer | right-note sim | best wrong sim | reply |",
               "|---|---|---|---|---|---|"]
    ret_ok = ans_ok = 0
    right_sims, wrong_sims = [], []
    for case in cases:
        q, f = case["q"], filters_for(case["q"])
        want = set(case.get("expect_ids", [])) | set(case.get("any_of", []))
        with session_scope() as s:
            # Calibration uses the raw candidate scores, BEFORE the threshold.
            qvec = rec.embed([q], task="RETRIEVAL_QUERY")[0]
            vec_hits, _ = rag.candidates(s, qvec, keyword_terms(q), f)
            found = rag.retrieve(s, rec, q, f, min_score, alt=case.get("q_en", ""))
        right = max((h.sim for h in vec_hits if h.id in want), default=None)
        wrong = max((h.sim for h in vec_hits if h.id not in want), default=None)
        if right is not None:
            right_sims.append(right)
        if wrong is not None:
            wrong_sims.append(wrong)
        ids = [h.id for h in found.hits]
        if case["not_found"]:
            r_ok = True       # judged at the answer: the gate OR the model may stop it
        else:
            r_ok = all(i in ids for i in case.get("expect_ids", [])) and \
                (not case.get("any_of") or any(i in ids for i in case["any_of"]))
        ret_ok += r_ok
        ans = rag.answer(rec, q, found)
        low = ans.reply.lower()
        if case["not_found"]:
            a_ok = ans.status == "not_found"
        else:
            a_ok = ans.status == "found" and all(m.lower() in low for m in case.get("must", [])) \
                and (not case.get("expect_missing") or "not written in the notes" in low)
        a_ok = a_ok and not any(re.search(r"\b" + re.escape(x.lower()) + r"\b", low)
                                for x in case.get("forbid", []))
        ans_ok += a_ok
        short = (ans.reply or ans.status).replace("\n", " ⏎ ")[:140].replace("|", "/")
        report.append(f"| {case['id']} | {'✅' if r_ok else '❌ ' + ','.join(ids)} | {'✅' if a_ok else '❌'} | "
                      f"{'' if right is None else round(right, 3)} | {'' if wrong is None else round(wrong, 3)} "
                      f"| {short} |")
        time.sleep(delay)

    report += ["", "## 4. Threshold calibration", ""]
    if right_sims and wrong_sims:
        lo_right, hi_wrong = min(right_sims), max(wrong_sims)
        report.append(f"- lowest similarity of a RIGHT note: **{lo_right:.3f}**")
        report.append(f"- highest similarity of a WRONG note: **{hi_wrong:.3f}**")
        if lo_right > hi_wrong:
            report.append(f"- clean gap → suggested `RAG_MIN_SCORE` ≈ **{(lo_right + hi_wrong) / 2:.2f}**")
        else:
            p75 = sorted(wrong_sims)[int(len(wrong_sims) * 0.75)]
            report.append(f"- ranges overlap → the score gate alone cannot separate them; keep it near "
                          f"the 75th percentile of wrong notes (**{p75:.2f}**) and rely on the answer "
                          "check (cited ids + 'not found') for the rest.")
    report.insert(3, f"**Routing {route_ok}/{len(routing)} · Retrieval {ret_ok}/{len(cases)} · "
                     f"Answers {ans_ok}/{len(cases)}**")
    harness.write_report("rag_report.md", report)
    print("\n".join(report))


if __name__ == "__main__":
    main()
