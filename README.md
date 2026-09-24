# Softorino Email Bot

Automated email support for a configurable Gmail mailbox, built as a Python 3.12 Vercel serverless function.

## Mailbox configuration

For testing, set `GMAIL_USER_EMAIL` to your personal Gmail address and authorize
that same account with Google OAuth. Do not use `support@softorino.app` until the
bot has been verified in draft-only mode.

When testing is complete, change only `GMAIL_USER_EMAIL` to:

```text
support@softorino.app
```

The OAuth credentials and refresh token must belong to the mailbox configured in
`GMAIL_USER_EMAIL`. Never commit them to GitHub.

## Generate a Gmail refresh token locally

Keep `credentials.json` in the project root. It is ignored by Git. Then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python get_token.py
```

Sign in with the Gmail account being tested and approve the requested Gmail
permissions. The script saves `token.json` locally. Copy only its
`refresh_token` value into the Vercel environment variable
`GMAIL_REFRESH_TOKEN`; never commit either JSON file.

## Project structure

```text
softorino-email-bot/
├── api/
│   └── process_email.py
├── scripts/
│   └── check_kb_routing.py
├── .github/workflows/
│   └── check-kb-routing.yml
├── requirements.txt
├── vercel.json
└── README.md
```

The endpoint accepts both GET and POST (Vercel Cron calls scheduled
endpoints with GET, manual/curl testing typically uses POST — both run
the same processing and return the same JSON response) and processes
up to 5 unread inbox emails per run (1 second delay between each):

1. **Filter at fetch time** — Gmail query only returns mail from the last 7 days, and excludes senders (`noreply@`, `no-reply@`, `mailer-daemon@`) and subjects (unsubscribe, newsletter, notification, invoice, receipt, order confirmation, auto-reply, out of office) that are never real support requests
2. **Filter auto-replies and bounces** — Skips emails from `mailer-daemon@`, `noreply@` or with subjects like "Out of Office" or "Delivery Failed"
3. **Detect sensitive content** — Escalates without auto-reply if email contains threats, legal language, or severe insults
4. **Check escalation triggers** — Escalates if customer mentions refunds, charges, cancellations, fraud, or payment providers (PayPal, FastSpring, etc.)
5. **Generate AI reply** — Fetches relevant KB files and sends email + KB to Claude API
6. **Validate Claude response** — If Claude returns fallback answer ("our team will review"), escalates to ops team
7. **Create draft reply** — Saves reply as a Gmail draft (never auto-sends in draft-only mode)
8. **Mark as read** — Only marks email as read after successful processing
9. **Return status** — JSON response with `processed_count` and a `results` array (one entry per email) with draft ID, escalation reason (if any), and KB files used

Retry logic: Claude API retries once after 3-second delay if request fails. If both attempts fail, email remains unread for next Cron run.

## Keeping KB routing in sync

`relevant_kb_files()` picks knowledge base files from a hardcoded map. The files
themselves live in the separate `Softorino_Support_AI` repo, and nothing links
the two. Renaming or adding a KB file there silently breaks routing here.

This is not hypothetical: when Beamer cases moved out of `other_products.md`
into `beamer.md`, the map still pointed the "beamer" keyword at
`other_products.md`. Every Beamer email would have been answered with no Beamer
knowledge at all, with no error anywhere.

`scripts/check_kb_routing.py` fails on two kinds of drift:

- a routed filename that no longer exists in the knowledge base
- a knowledge base file that no keyword can ever reach

Run it against a local checkout:

```bash
python scripts/check_kb_routing.py --local ../Softorino_Support_AI
```

Or against GitHub, which needs `GITHUB_TOKEN` set to a PAT that can read the
private KB repo:

```bash
GITHUB_TOKEN=... python scripts/check_kb_routing.py
```

It reads the routing map with `ast` instead of importing the bot, so it needs no
dependencies installed.

**CI setup:** `.github/workflows/check-kb-routing.yml` runs it on push, on pull
request and daily at 07:00 UTC. The daily run is the one that matters -- KB-side
changes do not touch this repo, so nothing else would catch them. It needs a
repository secret named `KB_GITHUB_TOKEN` holding a PAT with read access to
`Softorino_Support_AI`; the default `GITHUB_TOKEN` is scoped to this repo only
and cannot read another private one. Until that secret is set, the workflow
fails on the GitHub lookup.

**When you add a KB file,** add a route for it in `relevant_kb_files()` in the
same change.

## Gmail label-based state tracking

The bot creates and manages 4 Gmail labels to track processing state per
message and avoid duplicate customer replies across runs (e.g. if a run
crashes after sending a reply but before marking the email read):

- `AI_PROCESSING` — added the moment a message starts processing, removed once it reaches a final state
- `AI_REPLIED` — set once a reply has been successfully delivered to the customer with no escalation
- `AI_ESCALATED` — set once the ticket has been escalated (with or without an accompanying customer reply)
- `AI_FAILED` — set if processing raises an error; the email is left unread so the next run retries it

Any message that already carries `AI_REPLIED` or `AI_ESCALATED` is skipped
entirely on future runs, even if it somehow reappears as unread.

**Escalation priorities:**
- `[SENSITIVE]` — Threats, legal language, or abuse (no auto-reply sent)
- `[HIGH PRIORITY]` — Fraud or scam reports (escalate to ops immediately)
- `[ESCALATION]` — Billing/refund/cancellation requests, unknown topics, payment failures

Send a `GET` or `POST` request to `/api/process_email` to run the processing
flow — for example `curl -s -X POST .../api/process_email` for manual
testing, or let Vercel Cron trigger it on schedule via GET.

## Endpoint authentication

The endpoint checks every request for `Authorization: Bearer <CRON_SECRET>`.
If `CRON_SECRET` is set in the environment and the header is missing or
doesn't match, the endpoint returns `401 {"error": "Unauthorized"}` without
touching the mailbox or calling Claude. If `CRON_SECRET` is not set, the
endpoint logs a warning and allows the request (only meant as a transition
period — set it before relying on this in production).

To configure it:

1. Generate a random secret, e.g. `openssl rand -hex 32`.
2. Add it to the Vercel project as environment variable `CRON_SECRET`.
3. Vercel automatically sends this same value as the `Authorization: Bearer`
   header on every Cron-triggered request to your project, so scheduled runs
   need no extra setup once the env var is set.
4. For manual/curl testing, add the header yourself:
   ```bash
   curl -s -X POST https://your-deployment.vercel.app/api/process_email \
     -H "Authorization: Bearer $CRON_SECRET"
   ```

Required Vercel environment variables:

```text
GMAIL_USER_EMAIL
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
ANTHROPIC_API_KEY
ESCALATION_EMAIL_1
CRON_SECRET
GITHUB_TOKEN
DRY_RUN=true
```

`GITHUB_TOKEN` — the `Softorino_Support_AI` knowledge base repo is private,
so KB file fetches need a GitHub Personal Access Token (fine-grained,
read-only `Contents` access to that repo is enough) sent as
`Authorization: Bearer <token>`. Without it, KB fetches will fail once the
repo is private — the bot logs a warning and still attempts the request
unauthenticated for backward compatibility, but it will 404/403 against a
private repo.
