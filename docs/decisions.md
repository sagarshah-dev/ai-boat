# Design decisions

Short records of choices that someone reading the code would otherwise question. Each one says
what was chosen, why, and what it costs.

## 1. One service instead of a Node gateway + Python AI service

**Chosen:** a single FastAPI service (a modular monolith).
**Why:** version 1 had two services for historical reasons, not by design: the Node bot came
first, the Python AI service later. That split caused real problems — two cold starts per
message, a wake-up ping and a ladder of timeouts between the services, and two writers creating
IDs in the same sheet (duplicate-ID race). Inside one process, layers (`telegram/ → ai/ →
services/ → db/`) give the same separation without the network hop.
**Cost:** the services can't be scaled or deployed separately. At this size that is not needed;
split when load or team size makes it so.

## 2. Postgres (Neon) instead of Google Sheets

**Chosen:** Postgres on Neon's free plan, with pgvector for search.
**Why:** auto-increment IDs, transactions, foreign keys and NUMERIC money remove whole bug classes
version 1 had (ID race, ₹1 float rounding, a blank row shifting columns). Neon was picked over
Supabase (free projects pause after a week idle) and Render Postgres (free database deleted after
30 days). Branches give a separate `test` database at no cost.
**Cost:** owners can no longer edit data in a spreadsheet; reports and bot commands replace that.

## 3. Region: Singapore for both database and app

**Chosen:** Neon `aws-ap-southeast-1` and Cloud Run `asia-southeast1`.
**Why:** Neon has no India region; Singapore is the closest. The app is placed next to the
database, not next to the user: one message makes several database round trips but only one
trip to Telegram.
**Cost:** data is stored outside India. India's DPDP Act allows this unless the government
restricts a country; a client project with stricter needs would change only `DATABASE_URL`.

## 4. Identity by Telegram user ID, access by approval

**Chosen:** a `members` table keyed by Telegram user ID; `/start` creates a request that only the
admin can approve as Owner or Guest.
**Why:** version 1 matched people by first name (breaks when a name changes or two people share
one) and kept the allowlist in an environment variable (a redeploy to add someone).
**Cost:** the admin must be online to let someone in.

## 5. Duplicate Telegram updates: at-most-once

**Chosen:** each `update_id` is written to `processed_updates` in its own committed transaction
before any work starts. A Telegram retry of the same update is skipped.
**Why:** for money, an expense saved twice is worse than a message lost. If processing fails, the
user is told to send the message again. Version 1 kept this list in memory, so a restart forgot
it and retries could double-save.
**Cost:** a crash mid-processing drops that one message (the user is asked to resend).

## 6. Replies are sent after the database commit

**Chosen:** handlers return a list of reply actions; they are sent only after the transaction
commits.
**Why:** the bot must never say "approved" or "saved" for a change that was rolled back.

## 7. Webhook secret header

**Chosen:** Telegram's `secret_token` is checked on every webhook call (constant-time compare).
**Why:** the Cloud Run URL is public; without the check anyone could post fake updates.

## 8. Local development by polling

**Chosen:** `scripts/dev_polling.py` long-polls Telegram and feeds updates to the same
`handle_update()` the webhook uses.
**Why:** a home PC has no public HTTPS address. Version 1 used a Cloudflare quick tunnel whose URL
changed on every restart. Polling needs nothing extra and runs the identical code path.

## 9. Migrations run when the container starts

**Chosen:** the container runs `alembic upgrade head` before starting the server.
**Why:** one command per deploy, and the schema can never lag behind the code.
**Cost:** only safe with one instance (`max-instances=1`); with more, migrations move to a
separate Cloud Run job.

## 10. Tests on real Postgres

**Chosen:** pytest against Postgres with pgvector (a container in CI), never SQLite.
**Why:** the app relies on `ON CONFLICT`, JSONB, arrays and vectors; a test on another database
would prove little. `test_migrations_match_models` fails if a model changes without a migration.

## 11. The model chooses, code decides — and money is always a template

**Chosen:** the model only picks a tool and its arguments. Code then checks them against the
user's own words (amount written in digits — `2 lakh` included, not a quantity like `5 kg`, an
item named, payer one of the sharing members) and writes every reply that contains money from a
fixed template. Search and recent replies are templates too; in version 1 the model totalled
search results itself.
**Why:** a model always produces *some* number — version 1 once saved "tea by sagar" as ₹10.
A template also means the same question gets the same answer, and no second model call.
**Cost:** template replies are English, while free-text answers follow the user's language.

## 12. Each tool runs in a savepoint

**Chosen:** every tool call is wrapped in a database savepoint.
**Why:** if a tool fails halfway (wrote a row, then crashed) only that tool's changes are rolled
back; the rest of the message — and the model's chance to explain — carry on.

## 13. One transaction per message, including the model call

**Chosen:** a message is handled inside one database transaction, model calls included.
**Why:** simple and safe — the reply is sent only after commit (decision 6).
**Cost:** a slow model answer keeps a database connection open for up to ~55 s. Fine for a few
users on one instance; with real traffic the model call moves outside the transaction.

## 14. Who shares expenses

**Chosen:** expenses are split equally between active **admin and owners**. Guests can use every
feature (and add expenses for an owner) but never pay a share. Names match by full Telegram name
or, when unique, by first name ("by sagar" → "Sagar Shah").

## 15. Propose, then confirm — stored in the database

**Chosen:** photos, voice notes, meetings and farm notes are shown as a preview and applied only
after **yes** (or ✅). The preview is stored in `pending_actions`; yes / no / a number are matched
by regex, never by the model. Apply checks everything again (the row may have changed since).
**Why:** a misread khata line or a misheard voice note is caught before it becomes data; a wrong
meeting lands on three phones. Version 1 kept previews in memory, so a restart between preview
and "yes" lost them. Typed expenses are NOT confirmed — one undo fixes them, and a confirmation
on every tea would stop people logging.

## 16. Buttons send what a person could type

**Chosen:** every button carries text a person could have typed ("yes", "2", "Mango") or one of a
few fixed actions (`menu:*`, `fu:<id>:<w|p|n|u>`); anything else is refused before it reaches the
AI. A button counts as the PRESSER's message.
**Why:** one code path for tap and typing; in a group, Biren tapping Sagar's Save finds nothing
waiting for Biren. Button data comes from the client, so it is treated as untrusted input.

## 17. One daily job, decided by date

**Chosen:** Cloud Scheduler calls `POST /jobs/daily` (header `X-Jobs-Secret`) once a day; the code
decides what is due (weekly report Mondays, monthly on the 1st, farm follow-ups, search sync).
**Why:** Cloud Scheduler is free for 3 jobs per billing account; one entry point keeps it at one.
Each task runs on its own and failures are reported, never hidden (version 1's email failed
silently for weeks).

## 18. Meetings: the bot's table is the record, the calendar is a copy

**Chosen:** a meeting is saved in `meetings`; Google Calendar gets a copy. A calendar failure is
reported ("saved here, add it by hand"), not treated as a failed booking.
**Cost:** an edit made directly in Google Calendar is not read back (no reverse sync yet).

## 19. Farm search: hybrid retrieval with code-built answers

**Chosen:** vector search (pgvector, exact scan) plus keyword overlap on our own term list,
fused by Reciprocal Rank Fusion, behind a similarity threshold. Plant / date / activity filters
are decided in code from the user's words. The model fills a fixed form (answer, cited ids,
missing); code rejects an answer that cites an id it was not given or contains a number not in a
cited note, and writes the source line itself.
**Why:** on a farm the dangerous hallucination is an invented dose. Every number in an answer
can be traced to a note. Keyword search keeps `0:52:34` from matching `13:0:45`.
**No vector index:** a few thousand rows scan exactly in milliseconds; an approximate index can
silently drop the right note. Add HNSW past ~50k chunks.

## 20. Translate once, embed both

**Chosen:** each note is translated to English once, stored (`note_english`), and embedded
together with the original. Answers and number checks use only the original words.
**Why:** notes are romanised Gujarati ("udhai"); embedding models handle that poorly. A question
in any language then meets the note in one language. A mistranslated "75 ml" can never reach an
answer.

## 21. Exact questions are answered from rows, not from search

**Chosen:** "when / how many" questions read the journal table directly; only when the user's
topic word matches nothing ("termite" vs "udhai") is meaning search used to FIND the rows — the
date printed still comes from the row, and a stale search copy is skipped, never trusted.
**Why:** dates and counts must be exact. Version 1's v15 bug: a topic the model "corrected"
widened the search to every note.

## 22. Evals with the real model, on demand

**Chosen:** `evals/` runs the real Gemini models through the real webhook on a throwaway Postgres
and grades outcomes in code (rows written, rows deleted, tools chosen, notes retrieved). A GitHub
Actions workflow runs it on demand; the one number that must stay 0 is "wrong writes".
**Why:** unit tests script the model, so they prove the checks work IF the model does X. Evals
prove what it does. Not on every push: it uses the free-tier quota.

## 23. The payer is read from the user's words; the three owners are seeded

**Chosen:** Sagar, Lalo (jignesh) and Biren (bako) are seeded as payers by migration 0003, with
aliases. The agent decides `paid_by` from the names written in the message
(`agent.resolve_payer`), not from the model's argument. A name that is not a payer, a typo,
"lalo ne" (given TO Lalo) or two names for one amount -> nothing saved, the user is asked.
After the model's tool calls, code compares them with the message's comma-parts
(`agent.unsaved_parts`): missing parts get one more model round, then a "Not saved" line; an
item with no amount is reported; "500 sevsad, coffee" becomes a one-tap "together or separate?".
**Why:** live bug, 28/29-10-2026: "… all by lalo" saved 1 of 4 entries, all as Sagar, with no
warning. The model's `paid_by` is an enum of members; when the named person was not in it, it
picked the sender, and the old fallback to the sender hid that. 117 evals had no list + other
payer case.
**Cost:** a seeded payer has no Telegram ID until they /start (linked on approval as Owner).
Names written another way ("bakabhai") are asked about instead of guessed.

## 24. One add_expenses call with an array, not one call per expense

**Chosen:** the model gets one tool, `add_expenses(items=[...])`. The agent expands it into one
`add_expense` run per item, so every item passes the same gates in its own savepoint, and the
result goes back to the model as one response per call. If parts of the list are still missing,
the agent asks once more with the fallback (stronger) model, then tells the user. An item with
no amount waits in `pending_actions` like "tea by sagar", so a plain "50" completes it for the
right payer. Each model round is logged (tool, amount, payer, result).
**Why:** live, 29-10-2026, the small model made one call for a three-item list and stopped,
even after a retry. Whether a model makes parallel calls is up to the model; an array is part
of the schema. Same function-calling pattern — no second parser in code.
**Cost:** the model can still send fewer items than written, so the code check of the list
stays. A few more tokens per call for the larger schema.

## 25. Search stop words and plant varieties are data; each model call gets the time left

**Chosen:** the keyword side's stop words moved from `rag_index.STOP` (code) to the
`search_stopwords` table, read with a 5-minute cache. Mango varieties (Kesar, Rajapuri, …) are
rows in `plants` (count 0, so the Mango total stays 150). In the Gemini wrapper each attempt is
given the time left in the budget (minimum 8 s) instead of a fixed 20 s, and every failure is
logged with its kind, code and detail.
**Why:** live, 05-11-2026, "Kesar par daag che, have shu karu?" returned 5 notes. "have" was a
search word, so a Dadam note ("have drip roj 30 min") passed the lower keyword gate; "Kesar"
was not a known plant, so no plant filter was applied. The AI summary failed, and the log said
only `LlmError`. A 25 s RAG budget with fixed 20 s calls also meant no retry and no fallback
model was ever possible after one failed call.
**Cost:** a new filler word or variety is an SQL row, not a deploy, but someone has to add it.
A shorter last attempt can time out where 20 s would not have; below 8 s no call is started.

## 26. Farm Salah: LangGraph for tasks where step two needs step one's answer

**Chosen:** a new tool, `farm_salah`, runs a LangGraph `StateGraph` (`app/ai/farm_salah.py`):
understand → journal → dose → calc → approve, or journal → expense, or straight to answer when
nothing usable was found. The agent's model picks the tool and fills need / topic / trees; code
grounds them in the user's words. In the graph, Gemini does two small jobs (which candidate notes
are about the SAME problem; reading the dose out of the farmer's advice, both numbers checked
against that advice). Which note worked (its Result), the arithmetic, the expense lookup and the
reply are code. A plan is saved only after the asker's ✅ (`interrupt()`); the paused graph is
checkpointed in Postgres (`langgraph-checkpoint-postgres`, tables made by migration 0005) through
the request's own connection, so the checkpoint, the journal row and the pending question commit
or roll back together. The plan waits 24 hours (`pending.SALAH`, keyed by chat AND member: only
the asker can approve). ✏️ changes the number of trees and the graph goes back to `calc` only.
A saved plan is a `planned` journal row; 2 days later the daily job asks "was it done?", and
"done" turns it into a spray, which then gets the usual 7-day result check. LangChain is not
used: the nodes call our own Gemini client.
**Why:** live baseline 06-11 and 11-11-2026 (0/3). "Which medicine worked, and when did we buy
it?": the free agent searched expenses for the word "dava" instead of the medicine it had just
found, and on the same data the next time did not search expenses at all. "On the day we sprayed
copper, what else did we spend?": the second half was dropped, because the loop ends early when a
tool returns a ready template. "Make a spray plan for 100 trees": notes were listed, no dose, no
amount, and "100 trees" was reported as "not written in the notes". All three need a fixed order
and step one's answer handed to step two — an edge and a state field, not the model's choice.
**Cost:** two new dependencies (langgraph, langgraph-checkpoint-postgres) and four library tables.
Checkpoints are written once per run (`durability="exit"`): step-by-step writes come from a
background thread, and on the shared connection they raced the request's commit (1 test run in
~100). Two extra Gemini calls for a plan, one for the other two tasks. The worked/not-worked
decision reads the label code wrote at the start of Result (✅ / ❌ / ➖); a result typed in SQL
without it counts as "no result". Dose edits and plan cost are not in this version.

**Follow-up (11-11-2026, live test after merge).** Three routing gaps, one heading bug:
(1) "limbu na pan pila padya che" went to search instead of a note to save — the new "plan /
problem" wording pulled observations away from log_farm_activity (eval and live, 2/2). Owner's
decision: a plain statement of a problem seen is a note; the rules and both tool descriptions
now say so, and a question about it ("..., shu karu?") is still a search. (2) "Chhelle kyare
paani aapyu? E divase majuri ketli?" never reached the graph: the description had only the
copper example. It now describes the SHAPE (a farm date the expense question depends on) with
watering and fertilizer examples. Lesson: the graph guarantees the order once a question
reaches it; routing into it is still the model's choice and needs its own eval cases.
(3) same_day takes `expense_words` ("majuri"), kept only if the user wrote it, and says how many
other expenses that day were left out. (4) The lookup heading showed the search words
("Watering · divase / majuri / kharch / ketlo / thayo"): they are not the user's words and
suggested the expense half had been looked at. The heading is now plant + activity only; the
words appear only in the "not found" line.

**Follow-up 2 (12-11-2026, live).** The watering question now reached the graph but found no
note: "paani" is an activity word, which this app never uses as a search word (the kind of work
is the activity column), and the graph had no activity field; the relevance check also spoke
only of problems and products. `farm_salah` now takes `activity` (checked against the app's list)
and treats notes of that activity as candidates, and the check accepts "the same kind of farm
work". The unit test had passed because its note repeated the question's words and its model
answer was scripted: test data must look like live data. Evals now use `GEMINI_EVAL_API_KEY`
(a key from a separate Google project, so they never use up the live bot's daily quota) and
fall back to the bot's key only if it is missing; the run summary says which key was used.

## 27. Live bugs of 06-11 / 13-11-2026: ask instead of guessing, say what was not used

**Chosen:**
- A Farm Salah question that names no problem ("Kesar na 100 zhad mate plan banao") or no farm
  work ("E divase biju kai kharch thayo?") is asked "which problem / which work?" with an
  `interrupt()` in `understand`, like the tree count. The answer is taken only if the journal
  contains its words; buttons offer the problems already written for that plant.
- A dose the user writes ("2 ml thi") is reported next to the advice dose. The plan still uses
  only a dose written in the farmer's advice.
- Plan answers carry "📝 Save as a farm note": the same preview, and the same checks, as a typed
  observation (owner: "Kesar par mor kala che" may be a new problem on a new plant).
- When the model gives no amount (or one the user never wrote) and the message has exactly one
  amount in one phrase, that amount is offered for ✅ instead of asking. "sorry" / "ok" are not
  items.
- The journal → expense hop runs the Farm Salah relevance check on the notes that share words;
  "last" needs a note about the whole topic.
- A note whose indexing failed says so, and the next search indexes it (`catch_up`), so "Saved"
  never means "unfindable until a daily sync" that needs Cloud Scheduler.
- Money answers have a How line; every How line has a blank line before it.
**Why:** each was seen live (screenshots 13-11-2026; baseline doc section 8.3).
**Cost:** one more model call for a hop (only when an expense search found nothing); a search
may embed up to 10 late rows first.

## 28. Short-term memory: a sliding window per chat and member

**Chosen:** the last 3 exchanges (the user's message and the bot's final reply) of one person in
one chat, from the last 30 minutes, kept in `conversation_turns` and sent to the model as earlier
turns before the new message. Old rows are deleted when a new one is written (no scheduled job).
Grounding may take the subject (plant, topic) from them, never a number. A settle PIN is never
stored. Corrections ("na, 150 hata") are a new tool, `edit_last_expense`, shown for ✅; an undo
chosen by the model is shown for ✅ too (the typed command `undo` stays direct).
**Why:** live baseline 13-11-2026, 0 of 3 follow-ups understood; one of them ("Na, 150 hata")
deleted an entry with no question.
**Considered:** LangGraph's checkpointer with the chat as the thread — the same idea, but the main
agent is not a graph; rebuilding it for memory alone would be a rewrite. Summary memory and
token-based trimming — not needed for three short exchanges. Per chat only — in the group,
Biren's last message would explain Sagar's "e".
**Cost:** no extra model call; a few hundred tokens per message.

## 29. Farm review: multi-agent (a supervisor and two helper agents)

**Chosen:** `farm_review` is a LangGraph graph. `gather` (code) reads the month's expenses and
journal once and decides what rules can decide: reminders (a result pending 7+ days, a plan saved
twice) and LINKS (an expense whose item names a treatment's product). `Send` starts a money
helper and a farm helper at the same time; each is an agent with its own prompt, its own
read-only tools over those facts, and its own loop (max 4 rounds). A writer adds at most 2 lines;
it may join money and farm only through a LINK. A model line with a number not in the facts, an
invented link, or "owe/owes" is dropped. A failed helper or writer drops only its lines.
**Why:** live baseline 13-11-2026: "Aa mahina nu farm review aapo" searched notes for the word
"review" and showed no money. One agent picks one tool; a review needs two areas and a join.
Live 16-11-2026 (first version): an invented link, "+8826%" against a month with no data, a
November plan in the October review, and a reply too long for a phone. Comparison and % were
removed; the reply is short.
**Considered:** one big tool that returns everything to the main agent — works for the numbers,
but one prompt then reads money and farm facts together. Helpers that query the database
themselves — they run on two threads, and one SQLAlchemy session must not be shared across
threads; prefetching keeps their tools pure. Same-day expense = link — tea is bought on spray
days too. AI-written reminders — a fixed rule is right every time in code.
**Cost:** 3–5 model calls per review, in parallel: about the slowest helper plus the writer.

## 30. MCP server: four read-only tools for Claude, behind our own OAuth server

**Chosen:** `/mcp` in the same Cloud Run service, built on the official `mcp` Python SDK (2.3.0),
Streamable HTTP, stateless, JSON responses. Four tools — `search_farm_notes`,
`last_farm_activity`, `search_expenses`, `get_balance` — that call the same `services/` as the
bot; no new business logic. Every tool returns one envelope: `{"ok": true, ...}` or
`{"ok": false, "error": {"code", "message", "hint"}}` with `isError` set, so the model can fix
its arguments (INVALID_INPUT) or tell the user it failed (INTERNAL, no internals in the text).
"Nothing found" is ok with an empty list. The farm tool returns the notes, not an answer: the
client's model writes the answer, so there is no second Gemini call. Versioned as `ai-boat 1.0.0`;
a breaking change gets a new tool name.
Sign-in is OAuth 2.1, with this service as the authorization server: DCR (`/register`), PKCE S256,
a password page for the one owner (`MCP_OWNER_PASSWORD`), access token 1 hour, refresh token 30
days, rotated on every use. Only Claude's callback (and Claude Code's loopback) may register.
Secrets are stored as SHA-256. A spent code or rotated refresh token used again cancels every
token of that sign-in. Five wrong passwords in 15 minutes lock the page. All of it in Postgres
(`oauth_clients`, `oauth_tokens`, `oauth_login_failures`).
**Why:** two of three matching job posts ask for MCP servers ("tool schemas, auth, error contracts,
versioning"). Claude's custom connector, checked 16-11-2026 in its docs, supports OAuth or no
auth for a personal account; a fixed API key in a header is a beta for some organisations only.
No auth on money data is not an option, so OAuth.
**Considered:** write tools (add expense, settle) — the bot guards them with a preview, ✅ and the
settle PIN; a chat client has none of that, so read-only by design. A separate MCP service — a
second deploy and a second copy of the services for no gain. Google as the sign-in (delegated
OAuth) — more moving parts for one user; a password page is enough and can be swapped later.
Keeping OAuth state in memory — a Cloud Run restart would sign Claude out mid-day.
**Found while testing:** the SDK's `TokenError` is a frozen dataclass; raised inside
`with session_scope()` it turns into `FrozenInstanceError` (a 500). Errors are raised after the
database block closes; a race test covers it.
**Cost:** one Gemini embedding call per farm search (the bot's key and quota). Nothing else.

## Open

- Google Calendar → bot sync: an event moved directly in the calendar is not read back.
- Meeting reminders (the `reminder_sent` column is ready; the job is not written yet).
- MCP: a second client (ChatGPT) needs its callback URL added to the redirect allow-list.
- The similarity threshold (`RAG_MIN_SCORE`, 0.62 from version 1) should be re-checked with
  `evals/run_rag_evals.py` on this deployment.
