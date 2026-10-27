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

## Open

- Google Calendar → bot sync: an event moved directly in the calendar is not read back.
- Meeting reminders (the `reminder_sent` column is ready; the job is not written yet).
- The similarity threshold (`RAG_MIN_SCORE`, 0.62 from version 1) should be re-checked with
  `evals/run_rag_evals.py` on this deployment.
