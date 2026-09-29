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
│   ├── check_kb_routing.py
│   ├── check_message_rules.py
│   └── diagnose_agent_replies.py
├── .github/workflows/
│   ├── check-kb-routing.yml
│   ├── check-message-rules.yml
│   └── run-bot.yml
├── requirements.txt
├── vercel.json
└── README.md
```

The endpoint accepts both GET and POST (Vercel Cron calls scheduled
endpoints with GET, manual/curl testing typically uses POST — both run
the same processing and return the same JSON response) and processes
up to 10 unread **threads** per run (1 second delay between each):

1. **Filter at fetch time** — the Gmail query is bounded by `newer_than:`, derived from `MAX_MESSAGE_AGE_HOURS` (see below), and excludes senders (`noreply@`, `no-reply@`, `mailer-daemon@`) and subjects (unsubscribe, newsletter, notification, invoice, receipt, order confirmation, auto-reply, out of office) that are never real support requests
2. **Filter auto-replies and bounces** — Skips emails from `mailer-daemon@`, `noreply@` or with subjects like "Out of Office" or "Delivery Failed"
3. **Detect sensitive content** — Threats, legal language or severe insults escalate to the ops team and the customer gets a fixed template (`SENSITIVE_ESCALATION_REPLY`), never a generated reply
4. **Check escalation triggers** — Refunds, charges, cancellations, fraud or payment providers escalate to the ops team, and the customer still gets a reply generated from the escalation template for the matching category
5. **Generate AI reply** — Fetches relevant KB files and sends email + KB to Claude API
6. **Validate Claude response** — If Claude returns fallback answer ("our team will review"), escalates to ops team
7. **Create draft reply** — Saves reply as a Gmail draft (never auto-sends in draft-only mode)
8. **Mark as read** — Only marks email as read after successful processing
9. **Return status** — JSON response with `processed_count` and a `results` array (one entry per email) with draft ID, escalation reason (if any), and KB files used

Retry logic: Claude API retries once after 3-second delay if request fails. If both attempts fail, email remains unread for next Cron run.

## One reply per thread

Unread messages are grouped by `threadId` and only the newest message of each
thread is answered. The others are marked read, given the same label as the
answered message, and never reach Claude. Nothing is lost: the newest message
quotes the whole conversation underneath it.

Without this, a customer who wrote five times between runs got five separate
replies in ninety seconds -- and the angrier the customer, the more likely they
were to write repeatedly. It happened in production.

`MAX_EMAILS_PER_RUN` counts threads, not messages, so one talkative customer can
no longer eat the run and leave everyone else waiting. The run lists
`MAX_MESSAGES_SCANNED_PER_RUN` (5x the thread limit) messages before grouping,
because several of them can collapse into one thread.

Gmail lists newest first, but for a thread with more than one unread message the
order is confirmed against `internalDate` before picking. Answering the wrong
message means replying to a stale request while the real one sits unread. Single
message threads skip that lookup.

The run result reports `skipped_older_in_thread`, so the logs show the grouping
working rather than leaving it to be inferred.

If processing the newest message fails, its older siblings stay unread on
purpose -- the next run retries the whole thread instead of losing messages
behind a label.

## Escalation categories

Every escalation reply is written from a template in `tone_of_voice.md`, picked
by category. The code decides only which category applies and passes it to
Claude; the wording lives in the knowledge base so it can change without a
deploy.

| Category | Picked when |
|---|---|
| `billing` | `Refund Request (Customer Refuses Help)`, `Unauthorized Charge` |
| `general` | `Fraud/Scam Report`, Claude's no-answer fallback, anything unmapped |
| `account`, `technical` | Claude names them in its own `[[ESCALATE: ...]]` reason |

Sensitive content is the one case with no generated text at all. It answers with
`SENSITIVE_ESCALATION_REPLY`, a fixed constant in `api/process_email.py`.

Duplicate ops notifications are suppressed by checking whether any message in
the thread carries the `AI_ESCALATED` label. That check used to grep message
bodies for one fixed English sentence, which stopped working once the wording
became per-category and per-language.

The customer is answered every time. Only the notification to the team is
deduplicated -- three "just refund me" messages in a row get three replies and
one page.

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

## Message rule regression tests

`detect_escalation_triggers()` and `split_latest_message()` run before Claude is
called, so a false positive in either one means the customer gets no reply at
all. The run still reports success and the ticket just becomes an escalation,
which makes these failures easy to miss in production.

Both have already shipped that bug. `detect_escalation_triggers()` matched
`no support` as a plain substring, so it fired inside our own company name --
`Softori[no Suppor]t Team`. Every "Hello Softorino Support" and every quoted bot
signature escalated as a refund refusal. `split_latest_message()` only knew
English quote markers, so a Gmail account with a non-English interface left its
attribution line, company name included, inside the customer's new message and
fed the first bug.

`scripts/check_message_rules.py` covers both, plus the escalation category
mapping, the label-based dedup, and an end-to-end run of
`process_single_message()` against a fake Gmail service. That last group is the
one worth keeping: it checks whether the customer is answered at all, whether
the team is paged, and which label the message ends up carrying -- none of which
a single-function test can see.

```bash
python scripts/check_message_rules.py
```

It stubs the bot's third-party imports and only calls pure rule functions, so it
needs no dependencies, no credentials and no network.

`.github/workflows/check-message-rules.yml` runs it on push and pull request. No
daily run here -- everything it tests lives in this repo, so nothing can drift
without a commit.

**When you add or widen a trigger pattern,** add a case for it. Word boundaries
matter: bare substrings are what caused both bugs.

## Never talking over a person

Five gates run before anything else, in this order. Each one labels the message
and returns. Nothing is sent to the customer and Claude is never called.

1. **Mail from our own mailbox.** `From` matching the authenticated account is
   dropped immediately. This covers our own autoresponder and makes a reply loop
   impossible. The address comes from the Gmail profile rather than
   `GMAIL_USER_EMAIL`, so a typo in the env var cannot silently disable the check.
2. **Service mail.** Subjects starting with a `SERVICE_SUBJECT_PREFIXES` entry
   (`[AI Chat]`, Quidget's agent notifications) or containing a
   `SERVICE_SUBJECT_TERMS` entry (our autoresponder). Both lists are meant to
   grow. Leading `Re:` / `RE:` / `Fwd:` / `FW:` / `Re[2]:` markers, including
   chains of them, are stripped first: without that, `Re: [AI Chat] License
   Recovery` reads as a customer request the moment anyone replies, which is
   what happened on a live run. Subjects that merely start with those letters
   (`Refund`, `Reply needed:`, `Regarding:`, `Forward my licence`) are left
   alone.
3. **The message is older than `MAX_MESSAGE_AGE_HOURS` (12).** The check reads
   `internalDate` locally, so it runs before the gate below, which costs an API
   call. A missing or unparseable `internalDate` counts as stale. This is a
   backstop, not the main filter -- see below.
4. **A human agent already replied in the thread.** Every message the bot sends
   carries `X-Softorino-Bot: 1`. An outgoing message in the thread without that
   header, and without a service subject, was written by a person.
5. **A human agent wrote to this customer recently, in any thread.** Searches
   `from:<us> to:<customer> newer_than:14d`, up to 20 results, and stops at the
   first agent message. Last gate before the Claude call. Results are cached per
   address for the run, so two messages from one customer cost one lookup. A
   failed lookup skips the message and is reported as `human_check_failed`, kept
   apart from real hits so a broken query cannot pass for the gate working.

### The unread flag means nothing here

`UNREAD` is not a queue in this mailbox. Groove collects the mail and nothing
marks it read, so the count sits around 80,000 and only grows. Building the work
queue on that flag alone meant the bot was always looking at whatever Gmail
happened to return first.

The queue is bounded by date instead. `build_unread_query()` adds
`newer_than:<n>d`, where `n` comes from `MAX_MESSAGE_AGE_HOURS` rounded **up** to
whole days -- Gmail's `newer_than:` has no hour unit. Rounding up keeps the query
window from ever being narrower than the age gate, so the gate stays the precise
cut and the query never silently does the cutting instead. Both come from one
constant, so they cannot drift apart.

Stale mail is labelled but keeps its `UNREAD` flag. The date-bounded query
already keeps it out of the next run, and with 80,000 unread messages the flag
is plainly not the bot's to manage. Every other gate does clear it: those
messages are inside the date window and would otherwise be re-listed every run.

### What counts as an agent

`is_agent_message()` is the one place that decides, and both gate 4 and gate 5
use it. An outgoing message is an agent's only when it has **no**
`X-Softorino-Bot` header **and** its subject is not service mail.

The second condition is not optional. The autoresponder ("Your Softorino support
request has been received") goes out from the same address, unstamped, to every
customer. On "unstamped means agent" alone, every ticket would look
human-handled and the bot would answer nobody -- three of the six apparent agent
replies the mailbox diagnostic found were exactly this. It reuses
`SERVICE_SUBJECT_PREFIXES` and `SERVICE_SUBJECT_TERMS`, the same lists gate 2
matches on.

Gate 4 exists because thread grouping cannot see this. A Groove ticket and a
Gmail thread are not the same thing: an agent replying through Groove often
lands outside the original conversation, so Gmail shows several threads where
Groove shows one. Deduplication looked correct while the customer collected
three different answers from "one" team.

Two things follow from gate 4 and are intentional:

- Mail the bot sent before this header existed carries no stamp, so those
  threads now read as human-handled and the bot stays quiet in them.
- A thread that cannot be read, or an own-address lookup that fails, counts as
  human-handled. One unanswered email is cheaper than a reply written over an
  agent in front of the customer.

`unread_total` in the run result is the whole unread queue, read in one call
off the `UNREAD` label rather than by paging messages. A run only ever looks at
`MAX_MESSAGES_SCANNED_PER_RUN` of them, so without it there is no telling a
nearly empty backlog from a growing one. It is `null` if the lookup fails, which
does not stop the run.

The run result reports `skipped_human_handled`, `skipped_service_mail`,
`skipped_own_mail`, `skipped_stale` and `human_check_failed`, plus
`human_gate_hits`: one entry per human-gate hit naming the message that
triggered it, which gate matched and the id and subject it matched against.
Without that list there is no way to tell the gate working from the gate
silencing everything.

### Diagnostic mode (temporary)

Whether a Gmail-side check can work at all depends on one unanswered question:
do Groove-sent agent replies reach this mailbox? Setting `DIAGNOSTIC_ADDRESSES`
in Vercel to a comma-separated list of customer addresses turns the next run
into a read-only report instead of a processing run.

In that mode the function lists `to:<address>` and `from:<address>` over the
last 14 days, pulls `Date`, `From`, `To`, `Subject`, `Message-ID`,
`X-Softorino-Bot` and `threadId` for each hit, and returns the lot in the JSON
response, where the `run-bot` workflow log already shows it. It never lists the
unread queue, calls Claude, sends anything, applies a label or clears `UNREAD`.
The check runs before the `ALLOWED_SENDERS` guard, so the report does not depend
on how that switch is set.

To run it: set `DIAGNOSTIC_ADDRESSES` in Vercel, Redeploy, then Actions →
`run-bot` → Run workflow. **Clear the variable and Redeploy afterwards**, or the
bot will keep reporting instead of answering mail.

This is temporary. Delete `DIAGNOSTIC_ADDRESSES`, `run_mailbox_diagnostic()` and
the branch at the top of `process_unread_emails()` once the question is settled.
`scripts/diagnose_agent_replies.py` does the same thing from a terminal, for
anyone who has the credentials to hand.

## DRY_RUN

`DRY_RUN=true` drafts the reply instead of sending it. Two things follow from
that, and both matter when reading a run.

**A processed message is left exactly as it was.** No `AI_REPLIED`, no
`AI_ESCALATED`, not even `AI_PROCESSING`, and `UNREAD` stays on. The reply never
reached the customer, so marking the message handled would drop it out of the
queue for good: no later run would list it and nobody would ever answer it.

**Gate skips still label and mark read.** Those decisions -- own mailbox,
service mail, stale, human-handled -- do not depend on the mode, and leaving
them unread would have them re-listed on every run and crowd out fresh mail.

**Each processed message carries `draft_reply`**, the generated text in full,
alongside `escalated` and `escalation_category`. The drafts land in a mailbox
the person reviewing the run cannot open, so without this a run is only
counters. The field is absent in live mode.

**The ops notification is not sent either.** Because nothing is marked
handled, dry runs are idempotent: the same messages are reprocessed on every
run, so a real notification would reach the team once per press of Run workflow.
After the third identical page nobody reads them. What it would have said goes
into `suppressed_ops_notifications` instead -- message id, subject, customer,
escalation reason, priority, category and the recipients it would have gone to.
`draft_reply` and `escalation_reason` already carry everything the notification
carried.

## Gmail label-based state tracking

The bot creates and manages 7 Gmail labels to track processing state per
message and avoid duplicate customer replies across runs (e.g. if a run
crashes after sending a reply but before marking the email read):

- `AI_PROCESSING` — added the moment a message starts processing, removed once it reaches a final state
- `AI_REPLIED` — set once a reply has been successfully delivered to the customer with no escalation
- `AI_ESCALATED` — set once the ticket has been escalated (with or without an accompanying customer reply)
- `AI_FAILED` — set if processing raises an error; the email is left unread so the next run retries it
- `AI_SKIPPED_HUMAN` — a human agent had already replied in the thread
- `AI_SKIPPED_SERVICE` — service mail, or mail from our own mailbox
- `AI_SKIPPED_STALE` — older than `MAX_MESSAGE_AGE_HOURS` when the run reached it

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

## Who the bot is allowed to answer

`ALLOWED_SENDERS` decides whose mail the bot even sees. It is read on every run,
so the blast radius changes in the Vercel dashboard without a deploy.

| Value | Effect |
|---|---|
| `andrewsupport78@gmail.com` | only that address -- the test setup |
| `a@example.com,b@example.com` | only those addresses |
| `all` | no sender filter -- live mode, every customer |
| missing, blank, or only commas | **processes nothing** and logs a `[SAFETY]` warning |

Values are trimmed and lowercased, and several addresses become
`from:(a@x.com OR b@y.com)` in the Gmail query.

The last row is deliberate. The other reading of an unset variable is "no
filter", and deleting the variable by accident would then mail every customer in
the inbox. A bot that goes quiet is visible in the run history within the hour;
sent email is not recoverable. The check runs before the mailbox is opened.

## Scheduled runs

Vercel Cron on the Hobby plan allows one run a day and fails the deployment on a
more frequent expression, so `vercel.json` has no `crons` section. The schedule
lives in `.github/workflows/run-bot.yml` instead: every 30 minutes, plus
`workflow_dispatch` for a manual run from the Actions tab. It POSTs to the
endpoint with `Authorization: Bearer`, so it needs a repository secret named
`CRON_SECRET` holding the same value as the Vercel environment variable.

Do not shorten the interval. Runs are billed rounded up to the whole minute
against 2000 free minutes a month on a private repo. Every 30 minutes is ~1440;
every 20 would be ~2160 and would run out before the month ends.

Required Vercel environment variables:

```text
ALLOWED_SENDERS
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

`ALLOWED_SENDERS` — see above. Without it the bot processes nothing.

`GITHUB_TOKEN` — the `Softorino_Support_AI` knowledge base repo is private,
so KB file fetches need a GitHub Personal Access Token (fine-grained,
read-only `Contents` access to that repo is enough) sent as
`Authorization: Bearer <token>`. Without it, KB fetches will fail once the
repo is private — the bot logs a warning and still attempts the request
unauthenticated for backward compatibility, but it will 404/403 against a
private repo.
