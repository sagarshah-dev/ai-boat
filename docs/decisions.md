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

## Open

- `processed_updates` grows forever; a scheduled job (step 8) will delete rows older than 7 days.
- Member names come from Telegram's first and last name; renaming comes with step 5, where names
  matter for "paid by".
