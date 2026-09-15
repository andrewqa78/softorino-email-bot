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
