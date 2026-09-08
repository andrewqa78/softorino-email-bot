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

1. Finds the first unread message in the inbox.
2. Creates a reply draft with `Test draft from Softorino Bot`.
3. Marks the source message as read.
4. Returns the email subject and Gmail draft ID as JSON.

Send a `POST` request to `/api/process_email` to run the test flow. A `GET`
request only checks that the function is available and does not expose the
configured mailbox address.

Required Vercel environment variables:

```text
GMAIL_USER_EMAIL
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
```
