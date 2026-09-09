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

The current POST endpoint processes one unread inbox email:

1. **Filter auto-replies and bounces** — Skips emails from `mailer-daemon@`, `noreply@` or with subjects like "Out of Office" or "Delivery Failed"
2. **Detect sensitive content** — Escalates without auto-reply if email contains threats, legal language, or severe insults
3. **Check escalation triggers** — Escalates if customer mentions refunds, charges, cancellations, fraud, or payment providers (PayPal, FastSpring, etc.)
4. **Generate AI reply** — Fetches relevant KB files and sends email + KB to Claude API
5. **Validate Claude response** — If Claude returns fallback answer ("our team will review"), escalates to ops team
6. **Create draft reply** — Saves reply as a Gmail draft (never auto-sends in draft-only mode)
7. **Mark as read** — Only marks email as read after successful processing
8. **Return status** — JSON response with draft ID, escalation reason (if any), and KB files used

Retry logic: Claude API retries once after 3-second delay if request fails. If both attempts fail, email remains unread for next Cron run.

**Escalation priorities:**
- `[SENSITIVE]` — Threats, legal language, or abuse (no auto-reply sent)
- `[HIGH PRIORITY]` — Fraud or scam reports (escalate to ops immediately)
- `[ESCALATION]` — Billing/refund/cancellation requests, unknown topics, payment failures

Send a `POST` request to `/api/process_email` to run the test flow. A `GET`
request only checks that the function is available and does not expose the
configured mailbox address.

Required Vercel environment variables:

```text
GMAIL_USER_EMAIL
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
ANTHROPIC_API_KEY
ESCALATION_EMAIL_1
DRY_RUN=true
```
