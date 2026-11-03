#!/usr/bin/env python3
"""Regression tests for the rule-based checks that run before Claude is called.

These two functions decide whether a customer ever gets a reply at all, and both
of them have already shipped a bug that silently swallowed real support mail:

* ``detect_escalation_triggers`` matched ``no support`` as a plain substring, so
  it fired inside our own company name -- ``Softori[no Suppor]t Team``. Every
  "Hello Softorino Support" and every quoted bot signature escalated with
  "Refund Request (Customer Refuses Help)" before ``generate_reply`` was reached,
  so the customer got nothing back.
* ``split_latest_message`` only knew English quote markers, so a Gmail account
  with a non-English interface left its attribution line -- company name and all
  -- inside the text treated as the customer's new message, feeding the bug
  above.
* The trigger patterns are written in plain ASCII, but iOS and Apple Mail turn
  ``'`` into ``’`` as you type and most of our customers write from those. A
  real refusal worded "I don’t want support" matched nothing at all, so the
  customer got an autoreply instead of a human.

Both classes of failure are invisible in production: the run reports success and
the ticket just quietly becomes an escalation. Hence these tests.

The bot's third-party imports are stubbed out below, so this runs on a bare
Python 3.12 with nothing installed.

Usage::

    python scripts/check_message_rules.py
"""

import base64
import contextlib
import io
import os
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# Records every Claude call so the end-to-end checks can assert on what the
# prompt carried -- and, for sensitive content, that Claude was never called.
CLAUDE_CALLS = []

CLAUDE_STUB_REPLY = (
    "Hi Andrew,\n\nYour request is now with the team.\n\n"
    "Best Regards,\nSofi\nSoftorino Support Team"
)


class _StubBlock:
    type = "text"
    text = CLAUDE_STUB_REPLY


class _StubResponse:
    content = [_StubBlock()]


class _StubMessages:
    def create(self, **kwargs):
        CLAUDE_CALLS.append(kwargs)
        return _StubResponse()


class FakeAnthropic:
    def __init__(self, api_key=None):
        self.messages = _StubMessages()


def load_bot_module():
    """Import api/process_email.py without its third-party dependencies.

    Only pure-Python rule functions are exercised here; stubbing keeps CI free of
    a pip install and of credentials.
    """
    for name in (
        "google",
        "google.auth",
        "google.auth.transport",
        "google.auth.transport.requests",
        "google.oauth2",
        "google.oauth2.credentials",
        "googleapiclient",
        "googleapiclient.discovery",
        "anthropic",
    ):
        module = types.ModuleType(name)
        module.__path__ = []  # mark as a package so submodule imports resolve
        sys.modules.setdefault(name, module)
    sys.modules["google.auth.transport.requests"].Request = object
    sys.modules["google.oauth2.credentials"].Credentials = object
    sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
    sys.modules["anthropic"].Anthropic = FakeAnthropic

    sys.path.insert(0, str(REPO_ROOT / "api"))
    import process_email

    return process_email


bot = load_bot_module()


# --- escalation triggers -----------------------------------------------------
# (label, email text, should_escalate)
ESCALATION_CASES = [
    # The exact bug: our own signature, quoted back in a threaded reply.
    (
        "quoted bot signature does not escalate",
        "Best Regards,\nSofi\nSoftorino Support Team",
        False,
    ),
    (
        "customer greeting the company does not escalate",
        "Hello Softorino Support Team, my app crashes on startup",
        False,
    ),
    (
        "greeting variant: Dear Softorino Support",
        "Dear Softorino Support, WALTR PRO will not open on Windows 11.",
        False,
    ),
    # A real refusal must still escalate -- "no support" was dropped, so this
    # proves the remaining patterns still cover the case it was there for.
    (
        "explicit refusal escalates",
        "Just refund me, I don't want troubleshooting",
        True,
    ),
    (
        "refusal worded as 'don't want support' escalates",
        "Please cancel my licence. I don't want support, just my money back.",
        True,
    ),
    # Bounded gap: a question about the refund policy is not a refusal.
    (
        "'i just want' far from 'refund' does not escalate",
        "I just want to know how the activation works on a second computer, "
        "and while I am writing, could you point me at your refund policy page "
        "so I can read the terms before deciding anything?",
        False,
    ),
    (
        "'i just want a refund' still escalates",
        "I just want a refund, nothing else.",
        True,
    ),
    # Fraud patterns keep their word tails.
    ("fraud report escalates", "This is a scam, give me my money", True),
    ("'scammed' still matches", "You scammed me out of 40 dollars", True),
    ("'fraudulent' still matches", "This charge is fraudulent", True),
    # Smart punctuation: each pair is the same sentence typed on a keyboard and
    # typed on an iPhone. They have to behave identically.
    (
        "refusal with straight apostrophe escalates",
        "I don't want support, just a refund",
        True,
    ),
    (
        "refusal with curly apostrophe escalates",
        "I don’t want support, just a refund",
        True,
    ),
    (
        "unauthorized charge with curly apostrophe escalates",
        "I didn’t authorize this charge",
        True,
    ),
    (
        "technical email with a curly apostrophe does not escalate",
        "WALTR PRO won’t start on my Mac — it quits right after I open it. "
        "I’ve already reinstalled it twice.",
        False,
    ),
    (
        "ordinary technical email does not escalate",
        "Hi, WALTR PRO crashes immediately on launch on Windows 11. "
        "iTunes is installed from the Microsoft Store.",
        False,
    ),
]


# --- quote stripping ---------------------------------------------------------
UKRAINIAN_QUOTE_EMAIL = """Дякую, але проблема залишилась.

нд, 27 вер. 2026 р. о 23:37 Softorino Support Team <support@softorino.app> пише:

> Hi Andrew,
>
> Please reinstall iTunes from Apple's website.
"""

SPANISH_QUOTE_EMAIL = """Sigue sin funcionar.

El dom, 27 sept 2026 a las 23:37, Softorino Support Team <support@softorino.app> escribió:
Hi Andrew, please reinstall iTunes.
"""

ENGLISH_QUOTE_EMAIL = """Still broken.

On Sun, Sep 27, 2026 at 11:37 PM Softorino Support Team <support@softorino.app> wrote:
Hi Andrew, please reinstall iTunes.
"""

# A closing line of the customer's own must survive: no timestamp, no colon.
CUSTOMER_ADDRESS_EMAIL = """My app still crashes.

You can also reach me at <andrew.other@example.com>"""


# (label, email text, subject, expected_sensitive)
SENSITIVE_CASES = [
    ("threat with straight apostrophe", "I'll sue you", "", True),
    ("threat with curly apostrophe", "I’ll sue you", "", True),
    # "sue you" matches with or without the apostrophe. This pair is the one
    # that actually depended on it: the only route to True here is the
    # "i(?:'ll| will) kill" pattern.
    ("'i'll kill' with straight apostrophe", "I'll kill him", "", True),
    ("'i'll kill' with curly apostrophe", "I’ll kill him", "", True),
    ("threat with curly apostrophe in subject", "see subject", "I’ll sue you", True),
    (
        "technical email with a curly apostrophe is not sensitive",
        "WALTR PRO won’t start and I can’t work out why.",
        "Won’t launch",
        False,
    ),
]


def check_sensitive():
    failures = []
    for label, text, subject, expected in SENSITIVE_CASES:
        actual = bot.detect_sensitive_content(text, subject)
        if actual == expected:
            print(f"  ok    {label}")
            continue
        print(
            f"  FAIL  {label}\n"
            f"        expected sensitive={expected}, got {actual}\n"
            f"        text: {text[:90]!r} subject: {subject[:60]!r}"
        )
        failures.append(label)
    return failures


# --- escalation category routing -------------------------------------------
# The reply wording lives in tone_of_voice.md, one template per category. The
# code only reports the category, so a wrong mapping here means a billing
# escalation answered with a generic template.
# (label, reason, expected category)
CATEGORY_CASES = [
    ("refund refusal routes to billing", "Refund Request (Customer Refuses Help)", "billing"),
    ("unauthorized charge routes to billing", "Unauthorized Charge", "billing"),
    ("fraud report routes to general", "Fraud/Scam Report", "general"),
    ("claude fallback routes to general", "Claude fallback: No KB answer found", "general"),
    ("unknown reason falls back to general", "something nobody mapped", "general"),
    ("empty reason falls back to general", "", "general"),
    # Claude's marker reason is free text and may name the category itself.
    ("marker naming technical is honoured", "Known bug, technical team needed", "technical"),
    ("marker naming account is honoured", "Needs account record access", "account"),
]


def check_categories():
    failures = []
    for label, reason, expected in CATEGORY_CASES:
        actual = bot.category_for_escalation_reason(reason)
        if actual == expected:
            print(f"  ok    {label}")
            continue
        print(
            f"  FAIL  {label}\n"
            f"        reason {reason!r}: expected {expected!r}, got {actual!r}"
        )
        failures.append(label)
    return failures


# --- escalation dedup -------------------------------------------------------
ESCALATED_LABEL_ID = "Label_77"

# The phrase the old body-grep implementation looked for. A thread carrying it
# in the text but no label must NOT count as escalated -- otherwise the removed
# behaviour is still deciding things.
OLD_PHRASE_BODY = "We have forwarded your case to our support team for further review."


class FakeThreads:
    """Minimal stand-in for service.users().threads()."""

    def __init__(self, thread):
        self._thread = thread

    def get(self, **kwargs):
        thread = self._thread
        return types.SimpleNamespace(execute=lambda: thread)


class FakeUsers:
    def __init__(self, thread):
        self._threads = FakeThreads(thread)

    def threads(self):
        return self._threads


class FakeService:
    def __init__(self, thread):
        self._users = FakeUsers(thread)

    def users(self):
        return self._users


def _thread_with(messages):
    return {"messages": messages}


def _text_message(msg_id, body, label_ids):
    """A thread message shaped the way the Gmail API returns it."""
    import base64

    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": msg_id,
        "labelIds": label_ids,
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": "support@softorino.app"}],
            "body": {"data": encoded},
        },
    }


DEDUP_CASES = [
    (
        "thread with AI_ESCALATED is already escalated",
        _thread_with([
            _text_message("m1", "Hi there", ["INBOX"]),
            _text_message("m2", "Some reply", ["SENT", ESCALATED_LABEL_ID]),
        ]),
        True,
    ),
    (
        "thread without AI_ESCALATED is not escalated",
        _thread_with([
            _text_message("m1", "Hi there", ["INBOX"]),
            _text_message("m2", "Some reply", ["SENT"]),
        ]),
        False,
    ),
    (
        "old phrase in the body without the label does not count",
        _thread_with([
            _text_message("m1", OLD_PHRASE_BODY, ["SENT"]),
        ]),
        False,
    ),
    (
        "empty thread is not escalated",
        _thread_with([]),
        False,
    ),
]


def _quiet(call):
    """Run a bot function with its [ESCALATION-DEDUP] logging swallowed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return call()


def check_dedup():
    failures = []
    for label, thread, expected in DEDUP_CASES:
        service = FakeService(thread)
        actual = _quiet(lambda: bot.thread_already_escalated(service, "t1", ESCALATED_LABEL_ID))
        if actual == expected:
            print(f"  ok    {label}")
            continue
        print(f"  FAIL  {label}\n        expected {expected}, got {actual}")
        failures.append(label)

    # Degenerate inputs must fail open (escalate), never silently skip.
    for label, thread_id, label_id in [
        ("missing thread id escalates", None, ESCALATED_LABEL_ID),
        ("missing label id escalates", "t1", None),
    ]:
        service = FakeService(_thread_with([]))
        if _quiet(lambda: bot.thread_already_escalated(service, thread_id, label_id)) is False:
            print(f"  ok    {label}")
        else:
            print(f"  FAIL  {label}")
            failures.append(label)
    return failures


def check_escalations():
    failures = []
    for label, text, expected in ESCALATION_CASES:
        actual = bot.detect_escalation_triggers(text)["should_escalate"]
        if actual == expected:
            print(f"  ok    {label}")
            continue
        reason = bot.detect_escalation_triggers(text).get("reason", "-")
        print(
            f"  FAIL  {label}\n"
            f"        expected should_escalate={expected}, got {actual} (reason: {reason})\n"
            f"        text: {text[:90]!r}"
        )
        failures.append(label)
    return failures


def check_quote_stripping():
    failures = []

    def case(label, body, must_keep, must_drop):
        latest, _older = bot.split_latest_message(body)
        problems = [f"missing {s!r}" for s in must_keep if s not in latest]
        problems += [f"leaked {s!r}" for s in must_drop if s in latest]
        if not problems:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}\n        {'; '.join(problems)}\n        latest: {latest!r}")
        failures.append(label)

    # The company name must not survive into the "new message" text -- that is
    # what used to trip the escalation check.
    case(
        "ukrainian attribution line is stripped",
        UKRAINIAN_QUOTE_EMAIL,
        must_keep=["Дякую"],
        must_drop=["Softorino Support Team", "support@softorino.app", "пише:"],
    )
    case(
        "spanish attribution line is stripped",
        SPANISH_QUOTE_EMAIL,
        must_keep=["Sigue sin funcionar"],
        must_drop=["Softorino Support Team", "escribió:"],
    )
    case(
        "english attribution line is still stripped",
        ENGLISH_QUOTE_EMAIL,
        must_keep=["Still broken"],
        must_drop=["Softorino Support Team", "wrote:"],
    )
    case(
        "customer's own closing address is kept",
        CUSTOMER_ADDRESS_EMAIL,
        must_keep=["My app still crashes", "andrew.other@example.com"],
        must_drop=[],
    )

    # End to end: the stripped quote must not re-trigger the escalation bug.
    latest, _ = bot.split_latest_message(UKRAINIAN_QUOTE_EMAIL)
    if bot.detect_escalation_triggers(latest)["should_escalate"]:
        print("  FAIL  ukrainian quoted reply does not escalate")
        failures.append("ukrainian quoted reply does not escalate")
    else:
        print("  ok    ukrainian quoted reply does not escalate")

    return failures


# --- end to end --------------------------------------------------------------
# The checks above test one function at a time. This group runs
# process_single_message() itself, which is where the expensive mistakes live:
# whether the customer is answered at all, whether ops is notified, and which
# label the message ends up with. A unit test cannot see any of that.

_UNSET = object()

LABEL_IDS = {
    "processing": "L1",
    "replied": "L2",
    "escalated": "L3",
    "failed": "L4",
    "skipped_human": "L5",
    "skipped_service": "L6",
    "skipped_stale": "L7",
    "check_failed": "L8",
}
ESCALATED_LABEL = LABEL_IDS["escalated"]
OWN_MAILBOX = "support@softorino.app"


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmailService:
    """Enough of the Gmail API to run process_single_message() end to end.

    Records every drafts().create, messages().send and messages().modify so the
    checks can tell a customer reply from an ops notification.
    """

    def __init__(self, thread=None, agent_search_rows=None):
        self.actions = []
        self._thread = thread or {"messages": []}
        # What "from:us to:customer" finds. Each row: {"id", "subject", "stamped"}.
        self.agent_search_rows = list(agent_search_rows or [])
        self.search_queries = []

    # -- api surface --
    def users(self):
        return self

    def drafts(self):
        return self

    def messages(self):
        return self

    def threads(self):
        return self

    def create(self, userId=None, body=None):
        self.actions.append(("draft", body["message"]))
        return _Call({"id": "draft-1"})

    def send(self, userId=None, body=None):
        self.actions.append(("send", body))
        return _Call({"id": "sent-1"})

    def modify(self, userId=None, id=None, body=None):
        self.actions.append(("modify", body))
        return _Call({})

    def list(self, userId=None, q=None, maxResults=None):
        self.search_queries.append(q)
        return _Call({"messages": [{"id": row["id"]} for row in self.agent_search_rows]})

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        # format="metadata" is the agent search; format="full" is the thread read.
        if format != "metadata":
            return _Call(self._thread)
        row = next(r for r in self.agent_search_rows if r["id"] == id)
        headers = [{"name": "Subject", "value": row.get("subject", "")}]
        if row.get("stamped"):
            headers.append({"name": bot.BOT_HEADER_NAME, "value": bot.BOT_HEADER_VALUE})
        return _Call({"id": id, "payload": {"headers": headers}})

    def getProfile(self, userId=None):
        return _Call({"emailAddress": OWN_MAILBOX})

    # -- assertions helpers --
    def customer_reply(self):
        """The reply to the customer: the draft, in DRY_RUN."""
        for kind, body in self.actions:
            if kind == "draft":
                return base64.urlsafe_b64decode(body["raw"]).decode("utf-8")
        return None

    def ops_notification(self):
        """escalate_email() sends to ops through messages().send()."""
        for kind, body in self.actions:
            if kind == "send":
                return base64.urlsafe_b64decode(body["raw"]).decode("utf-8")
        return None

    def added_labels(self):
        for entry in self.actions:
            if entry[0] == "modify":
                return entry[1].get("addLabelIds", [])
        return []

    def removed_labels(self):
        for entry in self.actions:
            if entry[0] == "modify":
                return entry[1].get("removeLabelIds", [])
        return []

    def ops_sends(self):
        """Ops notifications: sent as brand-new messages, no threadId."""
        return [body for kind, body in self.actions if kind == "send" and "threadId" not in body]

    def customer_sends(self):
        """Live customer replies: always sent into the customer's thread."""
        return [body for kind, body in self.actions if kind == "send" and "threadId" in body]


def _epoch_ms_hours_ago(hours):
    return str(int((time.time() - hours * 3600) * 1000))


def _incoming(
    body,
    subject="Refund",
    sender="Andrew Q <andrew@example.com>",
    age_hours=0.5,
    internal_date=_UNSET,
    extra_headers=None,
):
    raw = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    message = {
        "id": "msg-1",
        "threadId": "t1",
        "internalDate": _epoch_ms_hours_ago(age_hours),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": "<abc@mail>"},
                *(extra_headers or []),
            ],
            "body": {"data": raw},
        },
    }
    if internal_date is not _UNSET:
        if internal_date is None:
            message.pop("internalDate")
        else:
            message["internalDate"] = internal_date
    return message


def _outgoing(msg_id, stamped):
    """A message in the thread sent from our own mailbox, bot-stamped or not."""
    headers = [{"name": "From", "value": f"Softorino Support <{OWN_MAILBOX}>"}]
    if stamped:
        headers.append({"name": bot.BOT_HEADER_NAME, "value": bot.BOT_HEADER_VALUE})
    return {
        "id": msg_id,
        "labelIds": ["SENT"],
        "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": ""}},
    }


def _escalated_thread():
    return {"messages": [{"id": "old", "labelIds": ["SENT", ESCALATED_LABEL]}]}


def _run_message(
    body,
    subject="Refund",
    thread=None,
    sender="Andrew Q <andrew@example.com>",
    age_hours=0.5,
    internal_date=_UNSET,
    agent_search_rows=None,
    cache=None,
    dry_run=True,
    extra_headers=None,
    service=None,
):
    service = service or FakeGmailService(thread, agent_search_rows)
    CLAUDE_CALLS.clear()
    incoming = _incoming(body, subject, sender, age_hours, internal_date, extra_headers)
    result = _quiet(
        lambda: bot.process_single_message(
            service,
            incoming,
            dry_run,
            LABEL_IDS,
            own_email=OWN_MAILBOX,
            agent_search_cache=cache,
        )
    )
    return service, result


def _category_line():
    """The escalation-category line out of the last prompt sent to Claude."""
    content = CLAUDE_CALLS[0]["messages"][0]["content"]
    for line in content.splitlines():
        if line.startswith("Escalation category"):
            return line
    return ""


def check_end_to_end():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    # The bot must not reach GitHub for the knowledge base during tests.
    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"

    try:
        # 1. Rule escalation: the silence bug. Reply AND notification AND label.
        service, result = _run_message("Just refund me, I don't want troubleshooting")
        expect(
            "rule escalation replies to the customer",
            result.get("processed") is True and service.customer_reply() is not None,
            f"processed={result.get('processed')}, reply={service.customer_reply()!r}",
        )
        expect(
            "rule escalation does not page ops in DRY_RUN",
            service.ops_notification() is None,
            f"ops={service.ops_notification()!r}",
        )
        expect(
            "the suppressed notification is described instead",
            (result.get("escalation") or {}).get("suppressed_by_dry_run") is True
            and (result.get("escalation") or {}).get("would_notify") == ["ops@softorino.app"],
            f"escalation={result.get('escalation')}",
        )
        live, _ = _run_message("Just refund me, I don't want troubleshooting", dry_run=False)
        expect(
            "rule escalation notifies ops for real in live mode",
            live.ops_notification() is not None,
        )
        expect(
            "rule escalation does not label in DRY_RUN",
            service.added_labels() == [],
            f"added={service.added_labels()}",
        )
        live, _ = _run_message("Just refund me, I don't want troubleshooting", dry_run=False)
        expect(
            "rule escalation labels the message AI_ESCALATED for real",
            live.added_labels() == [ESCALATED_LABEL],
            f"added={live.added_labels()}",
        )
        expect(
            "rule escalation passes the billing category to Claude",
            result.get("escalation_category") == "billing" and "billing" in _category_line(),
            f"category={result.get('escalation_category')!r}, line={_category_line()!r}",
        )

        # 2. Already-escalated thread: still reply, do not page ops again.
        service, result = _run_message(
            "Just refund me, I don't want troubleshooting", thread=_escalated_thread()
        )
        expect(
            "repeat escalation still replies to the customer",
            service.customer_reply() is not None,
        )
        expect(
            "repeat escalation does not notify ops twice",
            service.ops_notification() is None
            and result["escalation"].get("skipped_duplicate") is True,
            f"ops={service.ops_notification()!r}, escalation={result.get('escalation')}",
        )

        # 3. Sensitive content: fixed template, Claude never called.
        service, result = _run_message("I'll sue you, I am calling my lawyer")
        reply = service.customer_reply() or ""
        expect(
            "sensitive content sends the fixed template",
            bot.SENSITIVE_ESCALATION_REPLY.strip() in reply,
            f"reply={reply!r}",
        )
        expect(
            "sensitive content never reaches Claude",
            not CLAUDE_CALLS,
            f"{len(CLAUDE_CALLS)} call(s) made",
        )
        expect(
            "sensitive content does not page ops in DRY_RUN",
            service.ops_notification() is None,
            f"ops={service.ops_notification()!r}",
        )
        live, _ = _run_message("I'll sue you, I am calling my lawyer", dry_run=False)
        expect(
            "sensitive content notifies ops for real in live mode",
            live.ops_notification() is not None,
        )

        # 4. The ordinary path must still behave.
        service, result = _run_message(
            "WALTR PRO will not start on Windows 11", subject="Crash"
        )
        expect(
            "normal reply is delivered",
            result.get("processed") is True and service.customer_reply() is not None,
        )
        expect(
            "normal reply does not label in DRY_RUN",
            service.added_labels() == [],
            f"added={service.added_labels()}",
        )
        live, _ = _run_message(
            "WALTR PRO will not start on Windows 11", subject="Crash", dry_run=False
        )
        expect(
            "normal reply is labelled AI_REPLIED for real",
            live.added_labels() == [LABEL_IDS["replied"]],
            f"added={live.added_labels()}",
        )
        expect(
            "normal reply carries no escalation category",
            "(none)" in _category_line(),
            f"line={_category_line()!r}",
        )
        expect(
            "normal reply does not notify ops",
            service.ops_notification() is None,
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


# --- sender allow-list ------------------------------------------------------
# ALLOWED_SENDERS is the only thing standing between a test run and 600+ real
# customers. The two cases that matter most are the ones where the variable is
# missing or blank: the bot has to go quiet, because the other reading of an
# unset variable is "no filter" and that mails everyone.

TEST_SENDER = "andrewsupport78@gmail.com"


@contextlib.contextmanager
def allowed_senders_env(value):
    """Set ALLOWED_SENDERS to a value, or remove it when value is None."""
    previous = os.environ.get("ALLOWED_SENDERS")
    if value is None:
        os.environ.pop("ALLOWED_SENDERS", None)
    else:
        os.environ["ALLOWED_SENDERS"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ALLOWED_SENDERS", None)
        else:
            os.environ["ALLOWED_SENDERS"] = previous


def check_allowed_senders():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    def query_for(value):
        with allowed_senders_env(value):
            return bot.build_unread_query(bot.allowed_senders())

    # One address.
    query = query_for(TEST_SENDER)
    expect(
        "single address is filtered on",
        f"from:({TEST_SENDER})" in query,
        f"query={query!r}",
    )

    # Several addresses.
    query = query_for(f"{TEST_SENDER},someone@example.com")
    expect(
        "both addresses appear in the query",
        TEST_SENDER in query and "someone@example.com" in query and " OR " in query,
        f"query={query!r}",
    )

    # Whitespace and casing.
    query = query_for(f"  {TEST_SENDER.upper()} , Someone@Example.COM  ")
    expect(
        "addresses are trimmed and lowercased",
        f"from:({TEST_SENDER} OR someone@example.com)" in query,
        f"query={query!r}",
    )

    # "all" lifts the filter.
    query = query_for("all")
    expect(
        '"all" adds no from: filter',
        "from:(" not in query,
        f"query={query!r}",
    )
    expect(
        '"all" still filters the rest of the query',
        "in:inbox" in query and "is:unread" in query,
        f"query={query!r}",
    )
    query = query_for("  ALL  ")
    expect('"ALL" with padding is also recognised', "from:(" not in query, f"query={query!r}")

    # -- the safety catch --
    with allowed_senders_env(None):
        expect("missing variable parses as None", bot.allowed_senders() is None)
    with allowed_senders_env(""):
        expect("empty variable parses as None", bot.allowed_senders() is None)
    with allowed_senders_env("   "):
        expect("whitespace-only variable parses as None", bot.allowed_senders() is None)
    with allowed_senders_env(" , , "):
        expect("separators without addresses parse as None", bot.allowed_senders() is None)

    # process_unread_emails() must bail out before it ever builds a service.
    class ExplodingService:
        def __getattr__(self, name):
            raise AssertionError(
                "process_unread_emails touched the mailbox with ALLOWED_SENDERS unset"
            )

    original_service = bot.gmail_service
    bot.gmail_service = lambda: ExplodingService()
    try:
        for label, value in [
            ("missing variable processes nothing", None),
            ("empty variable processes nothing", ""),
        ]:
            with allowed_senders_env(value):
                try:
                    result = _quiet(bot.process_unread_emails)
                except AssertionError as error:
                    expect(label, False, str(error))
                    continue
            expect(
                label,
                result.get("processed_count") == 0 and result.get("results") == [],
                f"result={result!r}",
            )
    finally:
        bot.gmail_service = original_service

    return failures


# --- thread grouping --------------------------------------------------------
# One customer writing five times between runs used to get five separate
# replies in ninety seconds. Only the newest message of a thread is answered
# now; the rest are labelled and left alone. Getting "newest" wrong means
# answering a stale request, so the ordering is asserted explicitly.


_RECENT_BASE_MS = int((time.time() - 3600) * 1000)


class FakeMailbox:
    """A Gmail stand-in complete enough to run process_unread_emails().

    Messages are declared as (id, thread_id, internal_date, subject). The list
    order is the order given, which is deliberately NOT date order in the tests
    so the internalDate sort has something to prove.
    """

    def __init__(self, messages):
        self.store = {}
        self.refs = []
        for msg_id, thread_id, internal_date, subject in messages:
            ref = {"id": msg_id}
            if thread_id is not None:
                ref["threadId"] = thread_id
            self.refs.append(ref)
            self.store[msg_id] = {
                "id": msg_id,
                "threadId": thread_id,
                # The fixtures give small numbers purely to order messages
                # within a thread. Offset them to "an hour ago" so the age gate
                # does not read every fixture as a 1970 message.
                "internalDate": str(_RECENT_BASE_MS + int(internal_date)),
                "labelIds": ["INBOX", "UNREAD"],
                "subject": subject,
            }
        self.created_drafts = []
        self.unread_total = 137
        self.unread_total_raises = False
        self.label_gets = []
        self.agent_search_rows = []
        self.thread_messages = []
        self.sender = "Angry Customer <angry@example.com>"
        self.sent = []
        self.modifies = []
        self.list_calls = []
        # Extra headers per message id, e.g. List-Unsubscribe.
        self.extra_headers = {}
        self.agent_search_raises = False
        self.ops_send_raises = False

    # -- state helpers used by the checks --
    def labels_of(self, msg_id):
        return list(self.store[msg_id]["labelIds"])

    def answered_message_ids(self):
        """Which message each draft replied to, via In-Reply-To."""
        answered = []
        for raw in self.created_drafts:
            text = base64.urlsafe_b64decode(raw["raw"]).decode("utf-8")
            for line in text.splitlines():
                if line.lower().startswith("in-reply-to:"):
                    answered.append(line.split("<", 1)[1].split("@", 1)[0])
        return answered

    # -- api surface --
    def users(self):
        return self

    def getProfile(self, userId=None):
        return _Call({"emailAddress": OWN_MAILBOX})

    def messages(self):
        return _MailboxMessages(self)

    def drafts(self):
        return _MailboxDrafts(self)

    def labels(self):
        return _MailboxLabels(self)

    def threads(self):
        return _MailboxThreads(self)


class _MailboxMessages:
    def __init__(self, mailbox):
        self.mb = mailbox

    def list(self, userId=None, q=None, maxResults=None):
        self.mb.list_calls.append({"q": q, "maxResults": maxResults})
        # "from:us to:customer ..." is the agent lookup, not the unread queue.
        if (q or "").startswith(f"from:{OWN_MAILBOX}"):
            if self.mb.agent_search_raises:
                raise RuntimeError("Gmail search timed out")
            return _Call({"messages": [{"id": r["id"]} for r in self.mb.agent_search_rows]})
        return _Call({"messages": self.mb.refs[:maxResults]})

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        if format == "metadata":
            row = next(r for r in self.mb.agent_search_rows if r["id"] == id)
            headers = [{"name": "Subject", "value": row.get("subject", "")}]
            if row.get("stamped"):
                headers.append({"name": bot.BOT_HEADER_NAME, "value": bot.BOT_HEADER_VALUE})
            return _Call({"id": id, "payload": {"headers": headers}})
        stored = self.mb.store[id]
        common = {
            "id": stored["id"],
            "threadId": stored["threadId"],
            "internalDate": stored["internalDate"],
            "labelIds": list(stored["labelIds"]),
        }
        if format == "minimal":
            return _Call(common)
        body = base64.urlsafe_b64encode(
            f"{stored['subject']} body text".encode("utf-8")
        ).decode("ascii")
        common["payload"] = {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": self.mb.sender},
                {"name": "Subject", "value": stored["subject"]},
                {"name": "Message-ID", "value": f"<{stored['id']}@mail>"},
                *self.mb.extra_headers.get(stored["id"], []),
            ],
            "body": {"data": body},
        }
        return _Call(common)

    def modify(self, userId=None, id=None, body=None):
        self.mb.modifies.append((id, body))
        labels = self.mb.store[id]["labelIds"]
        for label in body.get("removeLabelIds", []):
            if label in labels:
                labels.remove(label)
        for label in body.get("addLabelIds", []):
            if label not in labels:
                labels.append(label)
        return _Call({})

    def send(self, userId=None, body=None):
        if self.mb.ops_send_raises and "threadId" not in body:
            raise RuntimeError("SMTP said no")
        self.mb.sent.append(body)
        return _Call({"id": "sent-1"})


class _MailboxDrafts:
    def __init__(self, mailbox):
        self.mb = mailbox

    def create(self, userId=None, body=None):
        self.mb.created_drafts.append(body["message"])
        return _Call({"id": f"draft-{len(self.mb.created_drafts)}"})


class _MailboxLabels:
    def __init__(self, mailbox):
        self.mb = mailbox

    def get(self, userId=None, id=None):
        self.mb.label_gets.append(id)
        if self.mb.unread_total_raises:
            raise RuntimeError("Gmail is having a moment")
        return _Call({"id": id, "messagesUnread": self.mb.unread_total})

    def list(self, userId=None):
        return _Call({"labels": [{"name": name, "id": f"id-{name}"} for name in bot.AI_LABEL_NAMES.values()]})


class _MailboxThreads:
    def __init__(self, mailbox):
        self.mb = mailbox

    def get(self, userId=None, id=None, format=None):
        return _Call({"messages": self.mb.thread_messages})


@contextlib.contextmanager
def _run_environment(mailbox):
    """ALLOWED_SENDERS set, no network, no inter-thread sleep."""
    original_service = bot.gmail_service
    original_kb = bot.build_knowledge_base
    original_delay = bot.DELAY_BETWEEN_EMAILS_SECONDS
    bot.gmail_service = lambda: mailbox
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    bot.DELAY_BETWEEN_EMAILS_SECONDS = 0
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"
    try:
        with allowed_senders_env("all"):
            yield
    finally:
        bot.gmail_service = original_service
        bot.build_knowledge_base = original_kb
        bot.DELAY_BETWEEN_EMAILS_SECONDS = original_delay


@contextlib.contextmanager
def dry_run_env(value):
    previous = os.environ.get("DRY_RUN")
    os.environ["DRY_RUN"] = "true" if value else "false"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DRY_RUN", None)
        else:
            os.environ["DRY_RUN"] = previous


def _run(mailbox, dry_run=True):
    CLAUDE_CALLS.clear()
    with _run_environment(mailbox), dry_run_env(dry_run):
        return _quiet(bot.process_unread_emails)


# --- not talking over people ------------------------------------------------
# On production the bot answered on top of agents Chloe and Alex, and replied to
# its own autoresponder with its internal reasoning. Groove sends an agent's
# reply outside the original Gmail conversation, so thread grouping cannot see
# the agent at all -- the signal is the X-Softorino-Bot header on outgoing mail.
AUTORESPONDER_SUBJECT = "Your Softorino support request has been received"


def check_human_and_service_gates():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"

    def silent(service, result, label, expected_reason, expected_label):
        expect(
            f"{label}: nothing is sent to the customer",
            service.customer_reply() is None,
            f"reply={service.customer_reply()!r}",
        )
        expect(f"{label}: Claude is not called", not CLAUDE_CALLS, f"{len(CLAUDE_CALLS)} call(s)")
        expect(
            f"{label}: labelled {expected_label}",
            service.added_labels() == [LABEL_IDS[expected_label]],
            f"added={service.added_labels()}",
        )
        expect(
            f"{label}: reported as {expected_reason}",
            result.get("skipped_reason") == expected_reason and result.get("processed") is False,
            f"result={result!r}",
        )

    try:
        # 1. An agent replied in the thread -- outgoing message with no stamp.
        service, result = _run_message(
            "Any update on this?",
            thread={"messages": [_outgoing("agent-1", stamped=False)]},
        )
        silent(service, result, "human in thread", "human", "skipped_human")

        # 2. Only the bot has replied -- every outgoing message is stamped.
        service, result = _run_message(
            "Any update on this?",
            thread={"messages": [_outgoing("bot-1", stamped=True)]},
        )
        expect(
            "bot-only thread is answered as usual",
            result.get("processed") is True and service.customer_reply() is not None,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        # 2b. Mixed: one stamped, one not. One human message is enough.
        service, result = _run_message(
            "Any update on this?",
            thread={
                "messages": [_outgoing("bot-1", stamped=True), _outgoing("agent-1", stamped=False)]
            },
        )
        expect(
            "one unstamped message among stamped ones still counts as human",
            result.get("skipped_reason") == "human" and service.customer_reply() is None,
            f"result={result!r}",
        )

        # 3. A brand new enquiry: no outgoing messages at all.
        service, result = _run_message("My app will not start", thread={"messages": []})
        expect(
            "new enquiry with no outgoing mail is answered",
            result.get("processed") is True and service.customer_reply() is not None,
        )

        # 4. Mail from our own mailbox, caught before every other check.
        service, result = _run_message(
            "Thank you for reaching out.",
            subject=AUTORESPONDER_SUBJECT,
            sender=f"Softorino Support <{OWN_MAILBOX}>",
            thread={"messages": []},
        )
        silent(service, result, "own mailbox", "self", "skipped_service")

        # The self check must win even when the thread looks perfectly normal
        # and the subject is an ordinary one.
        service, result = _run_message(
            "loop bait",
            subject="Re: Crash on launch",
            sender=OWN_MAILBOX,
            thread={"messages": [_outgoing("bot-1", stamped=True)]},
        )
        expect(
            "own mailbox is caught before the other gates",
            result.get("skipped_reason") == "self",
            f"result={result!r}",
        )

        # 5. Quidget agent notifications.
        service, result = _run_message(
            "A visitor started a chat",
            subject="[AI Chat] New conversation from a visitor",
            thread={"messages": []},
        )
        silent(service, result, "[AI Chat] notification", "service", "skipped_service")

        # 6. Our own autoresponder, arriving from somewhere other than our mailbox.
        service, result = _run_message(
            "We have received your request.",
            subject=f"Re: {AUTORESPONDER_SUBJECT}",
            thread={"messages": []},
        )
        silent(service, result, "autoresponder subject", "service", "skipped_service")

        # Subject matching is case insensitive and tolerates padding.
        expect(
            "service subjects match regardless of case and padding",
            bot.is_service_subject("  [ai chat] something  ")
            and bot.is_service_subject(AUTORESPONDER_SUBJECT.upper())
            and not bot.is_service_subject("My app crashes"),
        )

        # 7. An unreadable thread must not be answered into.
        class BrokenThreads(FakeGmailService):
            def get(self, userId=None, id=None, format=None):
                raise RuntimeError("Gmail is having a moment")

        broken = BrokenThreads({"messages": []})
        CLAUDE_CALLS.clear()
        # The check now returns the match it found rather than a bare True, so
        # a run can be audited. Both failure modes still count as human-handled.
        unreadable = _quiet(lambda: bot.thread_has_human_reply(broken, "t1", OWN_MAILBOX))
        expect(
            "an unreadable thread is treated as human-handled",
            bool(unreadable) and "unreadable" in unreadable["subject"],
            f"got {unreadable!r}",
        )
        unknown_own = _quiet(lambda: bot.thread_has_human_reply(FakeGmailService(), "t1", ""))
        expect(
            "an unknown own address is treated as human-handled",
            bool(unknown_own),
            f"got {unknown_own!r}",
        )
        expect(
            "a thread with no agent reply returns None",
            _quiet(lambda: bot.thread_has_human_reply(FakeGmailService(), "t1", OWN_MAILBOX))
            is None,
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


def check_outgoing_stamp():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    try:
        service, _result = _run_message("My app will not start", thread={"messages": []})
        reply = service.customer_reply() or ""
        expect(
            "the customer reply carries the bot header",
            f"{bot.BOT_HEADER_NAME}: {bot.BOT_HEADER_VALUE}" in reply,
            f"headers={reply.split(chr(10) * 2)[0]!r}",
        )
        # The ops notification only exists in live mode now.
        service, _result = _run_message(
            "Just refund me, I don't want troubleshooting",
            thread={"messages": []},
            dry_run=False,
        )
        ops = service.ops_notification() or ""
        expect(
            "the ops notification carries the bot header too",
            f"{bot.BOT_HEADER_NAME}: {bot.BOT_HEADER_VALUE}" in ops,
            f"ops={ops[:200]!r}",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


# --- message age -------------------------------------------------------------
# Groove never clears UNREAD in Gmail, so the queue fills with tickets agents
# closed days ago. Answering one of those is how the bot replied to Willie
# Johnson about forwarding his case to billing, two days after an agent had
# already refunded him.


# --- agent lookup by customer address ---------------------------------------
# Groove rewrites the threading headers, so most agent replies never land in the
# customer's Gmail thread and the thread check cannot see them. The lookup below
# asks a different question: has a person written to this customer at all?
#
# The trap the mailbox diagnostic exposed: the autoresponder ("Your Softorino
# support request has been received") also goes out unstamped, to every single
# customer. Counting it as an agent would mark every ticket human-handled and
# the bot would answer nobody.

AUTORESPONDER = {
    "id": "auto-1",
    "subject": "Your Softorino support request has been received \u2764\ufe0f",
    "stamped": False,
}
AGENT_MAIL = {"id": "agent-1", "subject": "Re: unable to activate folder colorizer", "stamped": False}
BOT_MAIL = {"id": "bot-1", "subject": "Re: Crash on launch", "stamped": True}
QUIDGET_MAIL = {"id": "quidget-1", "subject": "[AI Chat] New conversation", "stamped": False}


# --- DRY_RUN ----------------------------------------------------------------
# In DRY_RUN no reply reaches the customer. Applying AI_REPLIED and clearing
# UNREAD anyway would drop the message out of the queue for good: no later run
# would list it, and the customer would never be answered by anyone.
#
# Gate skips are the exception. Those decisions do not depend on the mode, and
# leaving them unread would have them re-listed on every run.


def _ops_sends(mailbox):
    """Ops notifications only.

    escalate_email() sends a brand-new message with no threadId; a live
    customer reply is sent with one. Both land in mailbox.sent.
    """
    return [body for body in mailbox.sent if "threadId" not in body]


# --- subject normalisation and the queue size -------------------------------
# "Re: [AI Chat] License Recovery - Lost Key Issue" walked straight through the
# service-mail gate on a live run: the prefix test ran against the raw subject,
# and the moment anyone replies the subject no longer starts with "[AI Chat]".

SERVICE_SUBJECT_CASES = [
    # (subject, expected)
    ("[AI Chat] License Recovery", True),
    ("Re: [AI Chat] License Recovery - Lost Key Issue", True),
    ("RE: [AI Chat] License Recovery", True),
    ("re: [ai chat] license recovery", True),
    ("Fwd: [AI Chat] License Recovery", True),
    ("FW: [AI Chat] License Recovery", True),
    ("Fw: [AI Chat] License Recovery", True),
    ("Re[2]: [AI Chat] License Recovery", True),
    ("RE: RE: [AI Chat] License Recovery", True),
    ("Fw: Re: [AI Chat] License Recovery", True),
    ("RE:[AI Chat] License Recovery", True),
    ("  re : [AI Chat] License Recovery  ", True),
    ("Your Softorino support request has been received", True),
    ("Re: Your Softorino support request has been received", True),
    ("RE: RE: Your Softorino support request has been received \u2764\ufe0f", True),
    # Subjects that merely begin with those letters must survive untouched.
    ("Refund request for my order", False),
    ("Reply needed: my app crashes", False),
    ("Regarding: activation on a second Mac", False),
    ("Review of my licence", False),
    ("Forward my licence to a new email", False),
    ("FWIW the app still crashes", False),
    ("My app will not start", False),
    ("", False),
    ("Re:", False),
]


def check_subject_normalisation():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    for subject, expected in SERVICE_SUBJECT_CASES:
        actual = bot.is_service_subject(subject)
        expect(
            f"{'service' if expected else 'not service'}: {subject!r}",
            actual == expected,
            f"expected {expected}, got {actual}, stripped to "
            f"{bot.strip_reply_prefixes(subject)!r}",
        )

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"
    try:
        # The live case, end to end.
        service, result = _run_message(
            "A visitor started a chat",
            subject="Re: [AI Chat] License Recovery - Lost Key Issue",
            thread={"messages": []},
        )
        expect(
            "a replied-to [AI Chat] notification is skipped as service mail",
            result.get("skipped_reason") == "service" and service.customer_reply() is None,
            f"result={result!r}",
        )
        # The agent classification asks the same function, so it is fixed too.
        service, result = _run_message(
            "My app will not start",
            thread={"messages": []},
            agent_search_rows=[
                {
                    "id": "auto-2",
                    "subject": "Re: Your Softorino support request has been received",
                    "stamped": False,
                }
            ],
        )
        expect(
            "a replied-to autoresponder is still not an agent",
            result.get("processed") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


def check_unread_total():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"
    try:
        mailbox = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
        mailbox.unread_total = 842
        result = _run(mailbox)
        expect(
            "the run reports the whole unread queue, not just what it scanned",
            result.get("unread_total") == 842 and result.get("processed_count") == 1,
            f"result={ {k: v for k, v in result.items() if k != 'results'} }",
        )
        expect(
            "it reads the UNREAD label rather than paging messages",
            mailbox.label_gets == ["UNREAD"],
            f"label_gets={mailbox.label_gets}",
        )

        empty = FakeMailbox([])
        empty.unread_total = 0
        result = _run(empty)
        expect(
            "an empty queue reports zero rather than omitting the field",
            result.get("unread_total") == 0,
            f"result={result!r}",
        )

        broken = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
        broken.unread_total_raises = True
        result = _run(broken)
        expect(
            "a failed label lookup reports null and does not break the run",
            "unread_total" in result
            and result["unread_total"] is None
            and result.get("processed_count") == 1,
            f"result={ {k: v for k, v in result.items() if k != 'results'} }",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


def check_dry_run():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"

    try:
        # -- 1. a processed message must come out of a dry run unchanged --
        mailbox = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
        before = mailbox.labels_of("m1")
        result = _run(mailbox, dry_run=True)
        expect(
            "a dry run still produces a draft",
            len(mailbox.created_drafts) == 1 and result.get("processed_count") == 1,
            f"drafts={len(mailbox.created_drafts)}",
        )
        expect(
            "a dry run leaves the message UNREAD",
            "UNREAD" in mailbox.labels_of("m1"),
            f"labels={mailbox.labels_of('m1')}",
        )
        expect(
            "a dry run applies no label at all, not even AI_PROCESSING",
            mailbox.labels_of("m1") == before,
            f"before={before}, after={mailbox.labels_of('m1')}",
        )
        expect(
            "a dry run makes no modify call for that message",
            not [entry for entry in mailbox.modifies if entry[0] == "m1"],
            f"modifies={mailbox.modifies}",
        )

        # The point of all of it: a later real run still sees the message.
        live_mailbox = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
        _run(live_mailbox, dry_run=False)
        expect(
            "the same message in a live run is labelled and marked read",
            "id-AI_REPLIED" in live_mailbox.labels_of("m1")
            and "UNREAD" not in live_mailbox.labels_of("m1"),
            f"labels={live_mailbox.labels_of('m1')}",
        )

        # -- 2. gate skips still settle the message in DRY_RUN --
        mailbox = FakeMailbox([("s1", "ts", 1000, "[AI Chat] New conversation")])
        _run(mailbox, dry_run=True)
        expect(
            "a service-mail skip still labels in DRY_RUN",
            "id-AI_SKIPPED_SERVICE" in mailbox.labels_of("s1")
            and "UNREAD" not in mailbox.labels_of("s1"),
            f"labels={mailbox.labels_of('s1')}",
        )

        mailbox = FakeMailbox([("o1", "told", 1000, "Old ticket")])
        mailbox.store["o1"]["internalDate"] = _epoch_ms_hours_ago(30)
        _run(mailbox, dry_run=True)
        expect(
            "a stale skip still labels in DRY_RUN, and keeps UNREAD",
            "id-AI_SKIPPED_STALE" in mailbox.labels_of("o1")
            and "UNREAD" in mailbox.labels_of("o1"),
            f"labels={mailbox.labels_of('o1')}",
        )

        mailbox = FakeMailbox([("h1", "th", 1000, "Any update?")])
        mailbox.agent_search_rows = [AGENT_MAIL]
        _run(mailbox, dry_run=True)
        expect(
            "a human-gate skip still labels in DRY_RUN",
            "id-AI_SKIPPED_HUMAN" in mailbox.labels_of("h1")
            and "UNREAD" not in mailbox.labels_of("h1"),
            f"labels={mailbox.labels_of('h1')}",
        )

        # -- 3. the generated text comes back in the response --
        service, result = _run_message(
            "My app will not start", thread={"messages": []}, dry_run=True
        )
        expect(
            "DRY_RUN returns the generated reply",
            result.get("draft_reply") == CLAUDE_STUB_REPLY,
            f"draft_reply={result.get('draft_reply')!r}",
        )
        expect(
            "the reply is returned whole, not truncated",
            result.get("draft_reply", "").endswith("Softorino Support Team")
            and len(result.get("draft_reply", "")) == len(CLAUDE_STUB_REPLY),
        )
        expect(
            "a normal reply reports escalated false",
            result.get("escalated") is False,
            f"escalated={result.get('escalated')!r}",
        )

        service, result = _run_message("My app will not start", dry_run=False)
        expect(
            "live mode does not return the reply",
            "draft_reply" not in result,
            f"keys={sorted(result)}",
        )

        # -- 4. escalations report their category alongside the text --
        service, result = _run_message(
            "Just refund me, I don't want troubleshooting",
            thread={"messages": []},
            dry_run=True,
        )
        expect(
            "an escalation returns the reply, escalated and the category",
            result.get("draft_reply") == CLAUDE_STUB_REPLY
            and result.get("escalated") is True
            and result.get("escalation_category") == "billing",
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        service, result = _run_message(
            "I'll sue you, I am calling my lawyer", thread={"messages": []}, dry_run=True
        )
        expect(
            "sensitive content returns the fixed template it would send",
            result.get("draft_reply") == bot.SENSITIVE_ESCALATION_REPLY
            and result.get("escalated") is True,
            f"draft_reply={result.get('draft_reply')!r}",
        )

        # -- 4b. the ops notification is described, not sent --
        # Dry runs reprocess the same messages every time, so a real page here
        # would arrive once per press of Run workflow.
        mailbox = FakeMailbox([("e1", "te", 1000, "Just refund me, I don't want troubleshooting")])
        result = _run(mailbox, dry_run=True)
        expect(
            "no ops mail is sent in DRY_RUN",
            not _ops_sends(mailbox),
            f"ops sends={len(_ops_sends(mailbox))}",
        )
        suppressed = result.get("suppressed_ops_notifications") or []
        expect(
            "the run records what would have been sent",
            len(suppressed) == 1,
            f"suppressed={suppressed}",
        )
        if suppressed:
            entry = suppressed[0]
            expect(
                "the record names the message, reason, category and recipients",
                entry["message_id"] == "e1"
                and entry["reason"] == "Refund Request (Customer Refuses Help)"
                and entry["category"] == "billing"
                and entry["would_notify"] == ["ops@softorino.app"]
                and entry["customer"],
                f"entry={entry}",
            )

        live_mailbox = FakeMailbox([
            ("e1", "te", 1000, "Just refund me, I don't want troubleshooting")
        ])
        live_result = _run(live_mailbox, dry_run=False)
        expect(
            "a live run sends the ops mail and records nothing",
            len(_ops_sends(live_mailbox)) == 1
            and live_result.get("suppressed_ops_notifications") == [],
            f"ops sends={len(_ops_sends(live_mailbox))}, "
            f"suppressed={live_result.get('suppressed_ops_notifications')}",
        )
        expect(
            "a run with no escalation records nothing either",
            (_run(FakeMailbox([("n1", "tn", 1000, "Crash on launch")]), dry_run=True)
             .get("suppressed_ops_notifications")) == [],
        )

        # -- 5. the run-level results carry it too, which is where it is read --
        mailbox = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
        result = _run(mailbox, dry_run=True)
        entry = result["results"][0]
        expect(
            "the run results carry the draft text per message",
            entry.get("draft_reply") == CLAUDE_STUB_REPLY,
            f"entry keys={sorted(entry)}",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


def check_agent_lookup():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"

    def run(rows, **kwargs):
        return _run_message(
            "My app will not start",
            thread={"messages": []},
            agent_search_rows=rows,
            **kwargs,
        )

    try:
        # -- the autoresponder trap --
        service, result = run([AUTORESPONDER])
        expect(
            "only the autoresponder found: still answered",
            result.get("processed") is True and service.customer_reply() is not None,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )
        service, result = run([AUTORESPONDER, AGENT_MAIL])
        expect(
            "autoresponder plus a real agent reply: treated as human",
            result.get("skipped_reason") == "human",
            f"result={result!r}",
        )
        expect(
            "the agent message is named, not the autoresponder",
            result.get("matched_message_id") == "agent-1",
            f"matched={result.get('matched_message_id')!r}",
        )
        service, result = run([BOT_MAIL, AUTORESPONDER])
        expect(
            "bot reply plus autoresponder: still answered",
            result.get("processed") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )
        service, result = run([QUIDGET_MAIL])
        expect(
            "an unstamped [AI Chat] message is not an agent",
            result.get("processed") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        # -- the plain cases from the brief --
        service, result = run([AGENT_MAIL])
        expect(
            "an unstamped agent reply means human",
            result.get("skipped_reason") == "human" and service.customer_reply() is None,
            f"result={result!r}",
        )
        expect("a human-gate hit sends nothing", service.customer_reply() is None)
        expect("a human-gate hit never calls Claude", not CLAUDE_CALLS, f"{len(CLAUDE_CALLS)}")
        expect(
            "a human-gate hit is labelled AI_SKIPPED_HUMAN",
            service.added_labels() == [LABEL_IDS["skipped_human"]],
            f"added={service.added_labels()}",
        )
        expect(
            "the match is reported for audit",
            result.get("matched_by") == "address_search"
            and result.get("matched_subject") == AGENT_MAIL["subject"]
            and result.get("customer") == "andrew@example.com",
            f"result={result!r}",
        )

        service, result = run([BOT_MAIL])
        expect(
            "only stamped bot replies: processed",
            result.get("processed") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )
        service, result = run([])
        expect("nothing found: processed", result.get("processed") is True)

        # -- the query itself --
        expect(
            "the lookup asks from:us to:customer over 14 days",
            service.search_queries
            == [f"from:{OWN_MAILBOX} to:andrew@example.com newer_than:14d"],
            f"queries={service.search_queries}",
        )

        # -- a failed lookup must not become a reply --
        class BrokenSearch(FakeGmailService):
            def list(self, userId=None, q=None, maxResults=None):
                raise RuntimeError("Gmail is having a moment")

        broken = BrokenSearch({"messages": []})
        CLAUDE_CALLS.clear()
        result = _quiet(
            lambda: bot.process_single_message(
                broken,
                _incoming("My app will not start"),
                True,
                LABEL_IDS,
                own_email=OWN_MAILBOX,
            )
        )
        expect(
            "a failed lookup reports human_check_failed",
            result.get("skipped_reason") == "human_check_failed",
            f"result={result!r}",
        )
        expect("a failed lookup sends nothing", broken.customer_reply() is None)
        expect("a failed lookup never calls Claude", not CLAUDE_CALLS)
        expect(
            "a failed lookup is not counted as a real human hit",
            result.get("skipped_reason") != "human",
        )

        # -- caching: two messages from one customer, one lookup --
        cache = {}
        first, _ = run([AGENT_MAIL], cache=cache)
        second, _ = run([AGENT_MAIL], cache=cache)
        expect(
            "the same customer is looked up once per run",
            len(first.search_queries) == 1 and second.search_queries == [],
            f"first={first.search_queries}, second={second.search_queries}",
        )

        # -- gate order: the thread check still wins when it matches --
        service, result = _run_message(
            "My app will not start",
            thread={"messages": [_outgoing("in-thread", stamped=False)]},
            agent_search_rows=[AGENT_MAIL],
        )
        expect(
            "the thread check is credited when it matches first",
            result.get("matched_by") == "thread",
            f"result={result!r}",
        )

        # -- the thread check must ignore the autoresponder too --
        service, result = _run_message(
            "My app will not start",
            thread={
                "messages": [
                    {
                        "id": "auto-in-thread",
                        "labelIds": ["SENT"],
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "From", "value": f"Softorino <{OWN_MAILBOX}>"},
                                {"name": "Subject", "value": AUTORESPONDER["subject"]},
                            ],
                            "body": {"data": ""},
                        },
                    }
                ]
            },
            agent_search_rows=[],
        )
        expect(
            "an autoresponder inside the thread is not a human either",
            result.get("processed") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        # -- run level: counters and the audit list --
        mailbox = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
        mailbox.agent_search_rows = [AGENT_MAIL]
        run_result = _run(mailbox)
        expect(
            "the run counts the human gate",
            run_result.get("skipped_human_handled") == 1 and not mailbox.created_drafts,
            f"result={ {k: v for k, v in run_result.items() if k != 'results'} }",
        )
        hits = run_result.get("human_gate_hits") or []
        expect(
            "the run lists the id and subject that triggered it",
            len(hits) == 1
            and hits[0]["matched_message_id"] == "agent-1"
            and hits[0]["matched_subject"] == AGENT_MAIL["subject"]
            and hits[0]["message_id"] == "m1",
            f"hits={hits}",
        )

        mailbox = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
        mailbox.agent_search_rows = [AUTORESPONDER]
        run_result = _run(mailbox)
        expect(
            "a run where only the autoresponder exists still answers",
            run_result.get("skipped_human_handled") == 0
            and len(mailbox.created_drafts) == 1,
            f"result={ {k: v for k, v in run_result.items() if k != 'results'} }, "
            f"drafts={len(mailbox.created_drafts)}",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


def check_message_age():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"

    try:
        expect(
            f"the limit is {bot.MAX_MESSAGE_AGE_HOURS}h",
            bot.MAX_MESSAGE_AGE_HOURS == 12,
            f"MAX_MESSAGE_AGE_HOURS={bot.MAX_MESSAGE_AGE_HOURS}",
        )

        # 2 hours old: a live conversation, answer it.
        service, result = _run_message(
            "My app will not start", thread={"messages": []}, age_hours=2
        )
        expect(
            "a 2 hour old message is processed",
            result.get("processed") is True and service.customer_reply() is not None,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        # 13 hours: past the limit.
        service, result = _run_message(
            "My app will not start", thread={"messages": []}, age_hours=13
        )
        expect(
            "a 13 hour old message is stale",
            result.get("skipped_reason") == "stale",
            f"result={result!r}",
        )
        expect("a stale message gets no reply", service.customer_reply() is None)
        expect("a stale message never reaches Claude", not CLAUDE_CALLS, f"{len(CLAUDE_CALLS)}")
        expect(
            "a stale message is labelled AI_SKIPPED_STALE",
            service.added_labels() == [LABEL_IDS["skipped_stale"]],
            f"added={service.added_labels()}",
        )

        # Exactly on the boundary: skip, so seconds of drift cannot flip it.
        service, result = _run_message(
            "My app will not start",
            thread={"messages": []},
            age_hours=bot.MAX_MESSAGE_AGE_HOURS,
        )
        expect(
            "a message exactly at the limit is stale",
            result.get("skipped_reason") == "stale",
            f"result={result!r}",
        )

        # Just inside the boundary still gets answered.
        service, result = _run_message(
            "My app will not start",
            thread={"messages": []},
            age_hours=bot.MAX_MESSAGE_AGE_HOURS - 0.5,
        )
        expect(
            "a message just under the limit is processed",
            result.get("processed") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        # internalDate missing, or garbage: fail closed.
        for label, override in [
            ("missing internalDate is stale", None),
            ("unparseable internalDate is stale", "not-a-number"),
            ("empty internalDate is stale", ""),
            ("zero internalDate is stale", "0"),
        ]:
            service, result = _run_message(
                "My app will not start", thread={"messages": []}, internal_date=override
            )
            expect(label, result.get("skipped_reason") == "stale", f"result={result!r}")
            expect(f"{label}: no reply sent", service.customer_reply() is None)

        # Gate order: service subject is checked before age, age before the
        # human-in-thread lookup.
        service, result = _run_message(
            "A visitor started a chat",
            subject="[AI Chat] New conversation",
            thread={"messages": []},
            age_hours=48,
        )
        expect(
            "a stale service mail is still reported as service",
            result.get("skipped_reason") == "service",
            f"result={result!r}",
        )
        service, result = _run_message(
            "Any update?",
            thread={"messages": [_outgoing("agent-1", stamped=False)]},
            age_hours=48,
        )
        expect(
            "age is checked before the human-in-thread lookup",
            result.get("skipped_reason") == "stale",
            f"result={result!r}",
        )

        # Run level: the label lands, UNREAD is cleared, the counter reports it,
        # and a fresh message beside it is still answered.
        mailbox = FakeMailbox([("old1", "told", 1000, "Old ticket")])
        mailbox.store["old1"]["internalDate"] = _epoch_ms_hours_ago(30)
        result = _run(mailbox)
        expect(
            "the run counts stale mail separately",
            result.get("skipped_stale") == 1,
            f"result={ {k: v for k, v in result.items() if k != 'results'} }",
        )
        expect(
            "a stale message gains AI_SKIPPED_STALE",
            "id-AI_SKIPPED_STALE" in mailbox.labels_of("old1"),
            f"labels={mailbox.labels_of('old1')}",
        )
        expect(
            "a stale message keeps UNREAD -- the date-bounded query drops it anyway",
            "UNREAD" in mailbox.labels_of("old1"),
            f"labels={mailbox.labels_of('old1')}",
        )
        # The other gates still settle the message, so it is not re-listed.
        service_mailbox = FakeMailbox([("s1", "ts", 1000, "[AI Chat] New conversation")])
        _run(service_mailbox, dry_run=False)
        expect(
            "a service skip still clears UNREAD",
            "UNREAD" not in service_mailbox.labels_of("s1"),
            f"labels={service_mailbox.labels_of('s1')}",
        )

        # -- the queue itself is bounded by date, so the age gate is a backstop --
        query = bot.build_unread_query([])
        expect(
            "the queue query is bounded by date",
            f"newer_than:{bot.process_window_days()}d" in query,
            f"query={query!r}",
        )
        expect(
            "exactly one newer_than term, so two windows cannot disagree",
            query.count("newer_than:") == 1,
            f"query={query!r}",
        )
        original_limit = bot.MAX_MESSAGE_AGE_HOURS
        try:
            for hours, expected_days in [(1, 1), (12, 1), (24, 1), (25, 2), (48, 2), (72, 3)]:
                bot.MAX_MESSAGE_AGE_HOURS = hours
                expect(
                    f"{hours}h maps to newer_than:{expected_days}d",
                    bot.process_window_days() == expected_days,
                    f"got {bot.process_window_days()}",
                )
                expect(
                    f"the {hours}h window is never narrower than the age gate",
                    bot.process_window_days() * 24 >= hours,
                )
        finally:
            bot.MAX_MESSAGE_AGE_HOURS = original_limit
        expect("a stale message is not replied to", not mailbox.created_drafts)

        mailbox = FakeMailbox([
            ("old1", "told", 1000, "Old ticket"),
            ("new1", "tnew", 1000, "Fresh ticket"),
        ])
        mailbox.store["old1"]["internalDate"] = _epoch_ms_hours_ago(30)
        result = _run(mailbox)
        expect(
            "a fresh message beside a stale one is still answered",
            result.get("skipped_stale") == 1 and len(mailbox.created_drafts) == 1,
            f"stale={result.get('skipped_stale')}, drafts={len(mailbox.created_drafts)}",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


def check_thread_grouping():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    SUBJECT = "Re: your software doesn't work!!!!"

    # 1. Three messages, one thread. List order is oldest-first on purpose, so
    #    answering the first one would be wrong.
    mailbox = FakeMailbox([
        ("m1", "t1", 1000, SUBJECT),
        ("m2", "t1", 3000, SUBJECT),  # the newest
        ("m3", "t1", 2000, SUBJECT),
    ])
    result = _run(mailbox)
    expect(
        "one thread produces exactly one reply",
        len(mailbox.created_drafts) == 1,
        f"{len(mailbox.created_drafts)} draft(s)",
    )
    expect(
        "the newest message is the one answered",
        mailbox.answered_message_ids() == ["m2"],
        f"answered {mailbox.answered_message_ids()}",
    )
    expect(
        "Claude is called once, not once per message",
        len(CLAUDE_CALLS) == 1,
        f"{len(CLAUDE_CALLS)} call(s)",
    )
    expect(
        "the two older messages are counted as folded in",
        result.get("skipped_older_in_thread") == 2,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )
    expect(
        "older messages are left untouched in DRY_RUN",
        all("UNREAD" in mailbox.labels_of(m) for m in ("m1", "m3")),
        f"m1={mailbox.labels_of('m1')}, m3={mailbox.labels_of('m3')}",
    )
    live_mailbox = FakeMailbox([
        ("m1", "t1", 1000, SUBJECT),
        ("m2", "t1", 3000, SUBJECT),
        ("m3", "t1", 2000, SUBJECT),
    ])
    _run(live_mailbox, dry_run=False)
    expect(
        "older messages get the answered message's label and lose UNREAD for real",
        all(
            "id-AI_REPLIED" in live_mailbox.labels_of(m)
            and "UNREAD" not in live_mailbox.labels_of(m)
            for m in ("m1", "m3")
        ),
        f"m1={live_mailbox.labels_of('m1')}, m3={live_mailbox.labels_of('m3')}",
    )
    expect(
        "one result entry, not three",
        result.get("processed_count") == 1,
        f"processed_count={result.get('processed_count')}",
    )

    # 2. Three separate threads behave exactly as before.
    mailbox = FakeMailbox([
        ("a1", "ta", 1000, "Crash on launch"),
        ("b1", "tb", 1000, "Activation question"),
        ("c1", "tc", 1000, "Transfer question"),
    ])
    result = _run(mailbox)
    expect("three threads produce three replies", len(mailbox.created_drafts) == 3, f"{len(mailbox.created_drafts)}")
    expect("three threads skip nothing", result.get("skipped_older_in_thread") == 0)
    expect("three threads give three results", result.get("processed_count") == 3)

    # 3. Mixed: one two-message thread plus two singles.
    mailbox = FakeMailbox([
        ("x1", "tx", 1000, "Crash"),
        ("x2", "tx", 2000, "Crash again"),
        ("y1", "ty", 1000, "Licence"),
        ("z1", "tz", 1000, "Transfer"),
    ])
    result = _run(mailbox)
    expect("mixed case produces three replies", len(mailbox.created_drafts) == 3, f"{len(mailbox.created_drafts)}")
    expect("mixed case folds in one older message", result.get("skipped_older_in_thread") == 1)
    expect(
        "mixed case answers the newest of the grouped thread",
        "x2" in mailbox.answered_message_ids() and "x1" not in mailbox.answered_message_ids(),
        f"answered {mailbox.answered_message_ids()}",
    )

    # 4. A ref with no threadId must not merge into anyone else's conversation.
    mailbox = FakeMailbox([
        ("n1", None, 1000, "No thread id"),
        ("n2", None, 2000, "Also no thread id"),
        ("p1", "tp", 1000, "Normal"),
    ])
    result = _run(mailbox)
    expect(
        "messages without threadId stay separate",
        len(mailbox.created_drafts) == 3 and result.get("skipped_older_in_thread") == 0,
        f"{len(mailbox.created_drafts)} draft(s), skipped={result.get('skipped_older_in_thread')}",
    )

    # 5. The run must list more messages than the thread limit, or one talkative
    #    customer starves the rest of the run.
    expect(
        "the list call scans past the thread limit",
        mailbox.list_calls[0]["maxResults"] > bot.MAX_THREADS_PER_RUN,
        f"maxResults={mailbox.list_calls[0]['maxResults']}, limit={bot.MAX_THREADS_PER_RUN}",
    )

    # 6. Thread count, not message count, is what the limit caps.
    many = []
    for index in range(bot.MAX_THREADS_PER_RUN + 3):
        many.append((f"g{index}", f"tg{index}", 1000, "Question"))
    mailbox = FakeMailbox(many)
    result = _run(mailbox)
    expect(
        "no more than MAX_THREADS_PER_RUN threads per run",
        result.get("processed_count") == bot.MAX_THREADS_PER_RUN,
        f"processed_count={result.get('processed_count')}",
    )

    # 7. The run result has to show why things were skipped, or the logs say
    #    nothing happened without saying why.
    mailbox = FakeMailbox([
        ("s1", "ts1", 1000, "[AI Chat] New conversation"),
        ("s2", "ts2", 1000, "Your Softorino support request has been received"),
        ("ok1", "tok", 1000, "Crash on launch"),
    ])
    result = _run(mailbox)
    expect(
        "the run counts service mail separately",
        result.get("skipped_service_mail") == 2,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )
    expect(
        "the run still answers the real email beside them",
        len(mailbox.created_drafts) == 1,
        f"{len(mailbox.created_drafts)} draft(s)",
    )

    mailbox = FakeMailbox([("h1", "th", 1000, "Any update?")])
    mailbox.thread_messages = [_outgoing("agent-1", stamped=False)]
    result = _run(mailbox)
    expect(
        "the run counts human-handled threads separately",
        result.get("skipped_human_handled") == 1 and not mailbox.created_drafts,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }, "
        f"drafts={len(mailbox.created_drafts)}",
    )

    mailbox = FakeMailbox([("o1", "to", 1000, "Re: anything")])
    mailbox.sender = f"Softorino Support <{OWN_MAILBOX}>"
    result = _run(mailbox)
    expect(
        "the run counts our own mail separately",
        result.get("skipped_own_mail") == 1 and not mailbox.created_drafts,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )

    return failures


def _expecter(failures):
    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    return expect


@contextlib.contextmanager
def escalation_env(first, second=None):
    """Set ESCALATION_EMAIL_1/_2, or remove them when None."""
    names = ("ESCALATION_EMAIL_1", "ESCALATION_EMAIL_2")
    previous = {name: os.environ.get(name) for name in names}
    for name, value in zip(names, (first, second)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _decoded(body):
    return base64.urlsafe_b64decode(body["raw"]).decode("utf-8")


# --- sensitive content goes ahead of the age and agent gates ----------------
# Behind them, a threat from a customer an agent was handling, or one that had
# sat for 12 hours, never reached the team. The notification has to go out
# every time; the fixed reply only where the bot would answer anyway, or it is
# the September bug again: a canned reply on top of an agent.
THREAT = "I'll sue you, I am calling my lawyer"


def check_sensitive_gate():
    failures = []
    expect = _expecter(failures)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"

    def notified_not_answered(label, service, result, withheld):
        expect(
            f"{label}: ops is notified",
            len(service.ops_sends()) == 1,
            f"ops sends={len(service.ops_sends())}",
        )
        expect(
            f"{label}: the customer gets nothing",
            not service.customer_sends() and service.customer_reply() is None,
            f"customer sends={len(service.customer_sends())}",
        )
        expect(f"{label}: Claude is not called", not CLAUDE_CALLS, f"{len(CLAUDE_CALLS)} call(s)")
        expect(
            f"{label}: labelled AI_ESCALATED",
            service.added_labels() == [ESCALATED_LABEL],
            f"added={service.added_labels()}",
        )
        expect(
            f"{label}: the result says why the reply was withheld",
            result.get("escalated") is True
            and result.get("customer_replied") is False
            and result.get("reply_withheld") == withheld,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

    try:
        # 1. An agent is in the thread.
        service, result = _run_message(
            THREAT, thread={"messages": [_outgoing("agent-1", stamped=False)]}, dry_run=False
        )
        notified_not_answered("sensitive + agent in thread", service, result, "human")

        # 2. An agent wrote to this customer from another thread.
        service, result = _run_message(
            THREAT, thread={"messages": []}, agent_search_rows=[AGENT_MAIL], dry_run=False
        )
        notified_not_answered("sensitive + agent by address", service, result, "human")
        expect(
            "sensitive + agent by address: the match is named for audit",
            result.get("matched_message_id") == "agent-1",
            f"result={result!r}",
        )

        # 3. Older than 12 hours: used to be dropped before anyone looked.
        service, result = _run_message(
            THREAT, thread={"messages": []}, age_hours=13, dry_run=False
        )
        notified_not_answered("sensitive + older than 12h", service, result, "stale")
        service, result = _run_message(
            THREAT, thread={"messages": []}, age_hours=200, dry_run=False
        )
        expect(
            "sensitive + 200h old: ops is still notified",
            len(service.ops_sends()) == 1 and not service.customer_sends(),
            f"ops={len(service.ops_sends())}, customer={len(service.customer_sends())}",
        )

        # 4. The agent lookup failed: notify, but do not guess at answering.
        class BrokenSearch(FakeGmailService):
            def list(self, userId=None, q=None, maxResults=None):
                raise RuntimeError("Gmail is having a moment")

        service, result = _run_message(
            THREAT, service=BrokenSearch({"messages": []}), dry_run=False
        )
        notified_not_answered(
            "sensitive + failed lookup", service, result, "human_check_failed"
        )

        # 5. A clean thread: notified AND answered with the fixed template.
        service, result = _run_message(THREAT, thread={"messages": []}, dry_run=False)
        expect(
            "sensitive + clean thread: ops is notified",
            len(service.ops_sends()) == 1,
            f"ops sends={len(service.ops_sends())}",
        )
        customer = [_decoded(body) for body in service.customer_sends()]
        expect(
            "sensitive + clean thread: the customer gets the fixed template",
            len(customer) == 1 and bot.SENSITIVE_ESCALATION_REPLY.splitlines()[2] in customer[0],
            f"customer sends={customer!r}",
        )
        expect(
            "sensitive + clean thread: reported as replied",
            result.get("customer_replied") is True and result.get("escalated") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )
        expect("sensitive + clean thread: Claude is not called", not CLAUDE_CALLS)
        expect(
            "the notification is a [SENSITIVE] one",
            _decoded(service.ops_sends()[0]).count("Subject: [SENSITIVE] ") == 1,
            f"ops={_decoded(service.ops_sends()[0])[:300]!r}",
        )

        # 6. DRY_RUN with an agent in the thread: no draft, no page, recorded.
        service, result = _run_message(
            THREAT, thread={"messages": [_outgoing("agent-1", stamped=False)]}, dry_run=True
        )
        expect(
            "sensitive + agent in DRY_RUN: no draft and no page",
            service.customer_reply() is None and not service.ops_sends(),
            f"actions={[kind for kind, _ in service.actions]}",
        )
        expect(
            "sensitive + agent in DRY_RUN: no draft_reply is returned",
            "draft_reply" not in result
            and (result.get("escalation") or {}).get("suppressed_by_dry_run") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        # 7. The gates in front of it still win.
        service, result = _run_message(
            THREAT, subject="[AI Chat] New conversation", thread={"messages": []}, dry_run=False
        )
        expect(
            "a threat inside service mail is still service mail",
            result.get("skipped_reason") == "service" and not service.ops_sends(),
            f"result={result!r}",
        )
        service, result = _run_message(
            THREAT, sender=f"Softorino Support <{OWN_MAILBOX}>", dry_run=False
        )
        expect(
            "a threat from our own mailbox is still skipped as self",
            result.get("skipped_reason") == "self" and not service.ops_sends(),
            f"result={result!r}",
        )
        service, result = _run_message(
            "I am in court all day and will reply tomorrow.",
            subject="Out of Office: Refund",
            dry_run=False,
        )
        expect(
            "an out-of-office mentioning court pages nobody and gets no reply",
            not service.ops_sends() and not service.customer_sends(),
            f"actions={[kind for kind, _ in service.actions]}",
        )

        # 8. Run level: a stale threat, live.
        mailbox = FakeMailbox([("x1", "tx", 1000, THREAT)])
        mailbox.store["x1"]["internalDate"] = _epoch_ms_hours_ago(13)
        result = _run(mailbox, dry_run=False)
        expect(
            "run level: a stale threat pages ops and answers nobody",
            len(_ops_sends(mailbox)) == 1
            and len(mailbox.sent) == 1
            and not mailbox.created_drafts,
            f"ops={len(_ops_sends(mailbox))}, sent={len(mailbox.sent)}",
        )
        expect(
            "run level: it is escalated, not counted as stale",
            "id-AI_ESCALATED" in mailbox.labels_of("x1")
            and "UNREAD" not in mailbox.labels_of("x1")
            and result.get("skipped_stale") == 0,
            f"labels={mailbox.labels_of('x1')}, stale={result.get('skipped_stale')}",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


# --- DRY_RUN on the sensitive path ------------------------------------------
# DRY_RUN=true promises that nothing leaves the building and nothing in the
# mailbox changes. The sensitive path has three side effects -- the ops page, the
# customer reply and the labels -- and each one has its own dry_run switch, so
# each one has to be proven separately. A draft is the one thing a dry run is
# allowed to create: it is the reply, held back from sending.
def check_sensitive_dry_run():
    failures = []
    expect = _expecter(failures)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    OPS = ["ops@softorino.app", "ops2@softorino.app"]

    def kinds(service):
        return [kind for kind, _ in service.actions]

    def untouched(label, service, drafts):
        expect(
            f"{label}: no message is sent -- not to the customer, not to either ESCALATION_EMAIL",
            "send" not in kinds(service),
            f"actions={kinds(service)}",
        )
        expect(
            f"{label}: no label is added or removed, UNREAD stays",
            "modify" not in kinds(service),
            f"actions={kinds(service)}",
        )
        expect(
            f"{label}: {drafts} draft(s) created",
            kinds(service).count("draft") == drafts,
            f"actions={kinds(service)}",
        )
        expect(f"{label}: Claude is not called", not CLAUDE_CALLS, f"{len(CLAUDE_CALLS)} call(s)")

    try:
        with escalation_env(*OPS):
            # 1. Clean thread: the reply is drafted, nothing else happens.
            service, result = _run_message(THREAT, thread={"messages": []}, dry_run=True)
            untouched("DRY_RUN sensitive, clean thread", service, drafts=1)
            expect(
                "DRY_RUN sensitive, clean thread: the draft is the fixed template",
                bot.SENSITIVE_ESCALATION_REPLY.splitlines()[2] in (service.customer_reply() or ""),
                f"reply={service.customer_reply()!r}",
            )
            expect(
                "DRY_RUN sensitive, clean thread: the page is described for both addresses",
                (result.get("escalation") or {}).get("suppressed_by_dry_run") is True
                and (result.get("escalation") or {}).get("would_notify") == OPS,
                f"escalation={result.get('escalation')}",
            )

            # 2-4. The three reasons the reply is withheld: no draft either.
            service, result = _run_message(
                THREAT, thread={"messages": [_outgoing("agent-1", stamped=False)]}, dry_run=True
            )
            untouched("DRY_RUN sensitive, agent in thread", service, drafts=0)
            service, result = _run_message(
                THREAT, thread={"messages": []}, agent_search_rows=[AGENT_MAIL], dry_run=True
            )
            untouched("DRY_RUN sensitive, agent by address", service, drafts=0)
            service, result = _run_message(
                THREAT, thread={"messages": []}, age_hours=30, dry_run=True
            )
            untouched("DRY_RUN sensitive, older than 12h", service, drafts=0)

            # Control: the same scenario live does all three, so the checks above
            # are not passing because nothing ever happens in this harness.
            service, result = _run_message(THREAT, thread={"messages": []}, dry_run=False)
            expect(
                "control, live: one page to each address, one reply, one label change",
                len(service.ops_sends()) == 1
                and _decoded(service.ops_sends()[0]).count("ops@softorino.app") >= 1
                and "ops2@softorino.app" in _decoded(service.ops_sends()[0])
                and len(service.customer_sends()) == 1
                and "modify" in kinds(service),
                f"actions={kinds(service)}",
            )

            # Run level, through process_unread_emails() with DRY_RUN=true.
            mailbox = FakeMailbox([("x1", "tx", 1000, THREAT)])
            before = mailbox.labels_of("x1")
            result = _run(mailbox, dry_run=True)
            expect(
                "DRY_RUN sensitive, run level: nothing is sent at all",
                mailbox.sent == [],
                f"sent={len(mailbox.sent)}",
            )
            expect(
                "DRY_RUN sensitive, run level: the mailbox is unchanged",
                mailbox.modifies == [] and mailbox.labels_of("x1") == before,
                f"modifies={mailbox.modifies}, labels={mailbox.labels_of('x1')}",
            )
            expect(
                "DRY_RUN sensitive, run level: the suppressed page is recorded",
                len(result.get("suppressed_ops_notifications") or []) == 1
                and result["suppressed_ops_notifications"][0]["would_notify"] == OPS,
                f"suppressed={result.get('suppressed_ops_notifications')}",
            )

            mailbox = FakeMailbox([("x2", "tx2", 1000, THREAT)])
            mailbox.agent_search_rows = [AGENT_MAIL]
            before = mailbox.labels_of("x2")
            _run(mailbox, dry_run=True)
            expect(
                "DRY_RUN sensitive + agent, run level: no send, no draft, no label change",
                mailbox.sent == []
                and mailbox.created_drafts == []
                and mailbox.modifies == []
                and mailbox.labels_of("x2") == before,
                f"sent={len(mailbox.sent)}, drafts={len(mailbox.created_drafts)}, "
                f"modifies={mailbox.modifies}",
            )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


# --- the bot's own ops notifications are not agents --------------------------
# escalate_email() writes "[ESCALATION] <subject> — <customer>" from our own
# mailbox to ESCALATION_EMAIL. Read as an agent message, every address on that
# list was silenced for good the first time it was paged.
def check_ops_notification_subjects():
    failures = []
    expect = _expecter(failures)

    def agent(subject, stamped=False):
        headers = [{"name": "Subject", "value": subject}]
        if stamped:
            headers.append({"name": bot.BOT_HEADER_NAME, "value": bot.BOT_HEADER_VALUE})
        return bot.is_agent_message(headers)

    for subject in [
        "[ESCALATION] Re: Crash on launch — customer@example.com",
        "Re: [ESCALATION] Re: Crash on launch — customer@example.com",
        "RE: Fwd: [escalation] Refund — customer@example.com",
        "[HIGH PRIORITY] This is a scam — customer@example.com",
        "[SENSITIVE] I'll sue you — customer@example.com",
    ]:
        expect(f"not an agent: {subject!r}", agent(subject) is False)
    expect("an ordinary unstamped reply is still an agent", agent("Re: Crash on launch") is True)
    expect(
        "the prefix has to lead: a mention later on does not count",
        agent("Re: why did I get an [ESCALATION] notice?") is True,
    )

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"
    try:
        ops_page = {
            "id": "ops-1",
            "subject": "[ESCALATION] Re: Refund — ops@softorino.app",
            "stamped": False,
        }
        service, result = _run_message(
            "My app will not start",
            sender="Ops <ops@softorino.app>",
            thread={"messages": []},
            agent_search_rows=[ops_page],
        )
        expect(
            "an ESCALATION_EMAIL address that was paged is still answered",
            result.get("processed") is True and service.customer_reply() is not None,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )
        service, result = _run_message(
            "WALTR PRO will not start", subject="[HIGH PRIORITY] app broken", thread={"messages": []}
        )
        expect(
            "a customer subject starting [HIGH PRIORITY] is not dropped as service mail",
            result.get("processed") is True,
            f"result={ {k: v for k, v in result.items() if k != 'escalation'} }",
        )

        # The send path: subject prefix and bot stamp, as Gmail will see them.
        service, _ = _run_message(
            "Just refund me, I don't want troubleshooting", thread={"messages": []}, dry_run=False
        )
        ops = _decoded(service.ops_sends()[0]) if service.ops_sends() else ""
        expect(
            "the ops notification subject starts with [ESCALATION]",
            "Subject: [ESCALATION] Refund" in ops,
            f"ops={ops[:300]!r}",
        )
        expect(
            "the ops notification is stamped X-Softorino-Bot",
            f"{bot.BOT_HEADER_NAME}: {bot.BOT_HEADER_VALUE}" in ops,
            f"ops={ops[:300]!r}",
        )
        expect(
            "every priority prefix is recognised by is_agent_message()",
            all(
                bot.is_ops_notification_subject(f"{prefix} x")
                for prefix in bot.OPS_NOTIFICATION_PREFIXES.values()
            ),
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


# --- a failed agent lookup is retried, not buried ---------------------------
def check_lookup_failure_retry():
    failures = []
    expect = _expecter(failures)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"

    class BrokenSearch(FakeGmailService):
        def list(self, userId=None, q=None, maxResults=None):
            raise RuntimeError("Gmail is having a moment")

    try:
        service, result = _run_message(
            "My app will not start", service=BrokenSearch({"messages": []}), dry_run=False
        )
        expect(
            "a failed lookup is labelled AI_CHECK_FAILED",
            service.added_labels() == [LABEL_IDS["check_failed"]],
            f"added={service.added_labels()}",
        )
        expect(
            "a failed lookup keeps UNREAD",
            "UNREAD" not in service.removed_labels(),
            f"removed={service.removed_labels()}",
        )
        expect(
            "a failed lookup is reported with the check_failed label key",
            result.get("label_key") == "check_failed"
            and result.get("skipped_reason") == "human_check_failed"
            and "remove_unread" not in result,
            f"result={result!r}",
        )

        # Run level: the next run picks it up again and answers it.
        mailbox = FakeMailbox([("f1", "tf", 1000, "Crash on launch")])
        mailbox.agent_search_raises = True
        result = _run(mailbox, dry_run=False)
        expect(
            "run level: AI_CHECK_FAILED lands and UNREAD stays",
            "id-AI_CHECK_FAILED" in mailbox.labels_of("f1")
            and "UNREAD" in mailbox.labels_of("f1")
            and result.get("human_check_failed") == 1
            and not mailbox.sent,
            f"labels={mailbox.labels_of('f1')}, result={ {k: v for k, v in result.items() if k != 'results'} }",
        )
        mailbox.agent_search_raises = False
        result = _run(mailbox, dry_run=False)
        expect(
            "run level: the retry answers it and drops AI_CHECK_FAILED",
            "id-AI_REPLIED" in mailbox.labels_of("f1")
            and "id-AI_CHECK_FAILED" not in mailbox.labels_of("f1")
            and "UNREAD" not in mailbox.labels_of("f1")
            and len(mailbox.sent) == 1,
            f"labels={mailbox.labels_of('f1')}, sent={len(mailbox.sent)}",
        )

        # A failure that never clears ends at the age gate, not in a loop.
        mailbox = FakeMailbox([("f2", "tf2", 1000, "Crash on launch")])
        mailbox.agent_search_raises = True
        _run(mailbox, dry_run=False)
        mailbox.store["f2"]["internalDate"] = _epoch_ms_hours_ago(13)
        result = _run(mailbox, dry_run=False)
        expect(
            "a persistent failure is settled as stale after 12 hours",
            result.get("skipped_stale") == 1
            and "id-AI_SKIPPED_STALE" in mailbox.labels_of("f2")
            and "id-AI_CHECK_FAILED" not in mailbox.labels_of("f2"),
            f"labels={mailbox.labels_of('f2')}",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


# --- a notification nobody receives is reported -----------------------------
def check_ops_notifications_skipped():
    failures = []
    expect = _expecter(failures)
    REFUND = "Just refund me, I don't want troubleshooting"

    def run(mailbox, dry_run, first, second=None):
        CLAUDE_CALLS.clear()
        # Inside _run_environment, which sets ESCALATION_EMAIL_1 itself.
        with _run_environment(mailbox), dry_run_env(dry_run), escalation_env(first, second):
            return _quiet(bot.process_unread_emails)

    mailbox = FakeMailbox([("e1", "te", 1000, REFUND), ("n1", "tn", 1000, "Crash on launch")])
    result = run(mailbox, dry_run=False, first=None)
    entries = result.get("ops_notifications_skipped_messages") or []
    expect(
        "no ESCALATION_EMAIL: ops_notifications_skipped counts it",
        result.get("ops_notifications_skipped") == 1,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )
    expect(
        "no ESCALATION_EMAIL: the message is listed with the reason",
        len(entries) == 1
        and entries[0]["message_id"] == "e1"
        and entries[0]["why"] == "no_recipients"
        and entries[0]["reason"] == "Refund Request (Customer Refuses Help)",
        f"entries={entries}",
    )
    expect(
        "no ESCALATION_EMAIL: the customer is still answered",
        len(mailbox.sent) == 2 and not _ops_sends(mailbox),
        f"sent={len(mailbox.sent)}, ops={len(_ops_sends(mailbox))}",
    )

    result = run(FakeMailbox([("e1", "te", 1000, REFUND)]), dry_run=True, first=None)
    expect(
        "no ESCALATION_EMAIL in DRY_RUN is counted too",
        result.get("ops_notifications_skipped") == 1
        and result.get("suppressed_ops_notifications") == [],
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )

    result = run(FakeMailbox([("e1", "te", 1000, REFUND)]), dry_run=False, first=None,
                 second="ops2@softorino.app")
    expect(
        "ESCALATION_EMAIL_2 alone is enough",
        result.get("ops_notifications_skipped") == 0,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )

    mailbox = FakeMailbox([("e1", "te", 1000, REFUND)])
    result = run(mailbox, dry_run=False, first="ops@softorino.app")
    expect(
        "configured and sent: nothing is counted",
        result.get("ops_notifications_skipped") == 0
        and result.get("ops_notifications_skipped_messages") == []
        and len(_ops_sends(mailbox)) == 1,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )

    mailbox = FakeMailbox([("e1", "te", 1000, REFUND)])
    mailbox.ops_send_raises = True
    result = run(mailbox, dry_run=False, first="ops@softorino.app")
    entries = result.get("ops_notifications_skipped_messages") or []
    expect(
        "a failed send is counted as well",
        result.get("ops_notifications_skipped") == 1
        and entries
        and entries[0]["why"].startswith("send_failed"),
        f"entries={entries}",
    )

    mailbox = FakeMailbox([("x1", "tx", 1000, THREAT)])
    result = run(mailbox, dry_run=False, first=None)
    expect(
        "a sensitive escalation with no ESCALATION_EMAIL is counted",
        result.get("ops_notifications_skipped") == 1,
        f"result={ {k: v for k, v in result.items() if k != 'results'} }",
    )
    return failures


# --- knowledge base routing reads the subject -------------------------------
def check_kb_routing_by_subject():
    failures = []
    expect = _expecter(failures)

    def files(body, subject=""):
        return bot.relevant_kb_files(body, subject)

    expect(
        "product only in the subject: beamer.md is picked",
        "beamer.md" in files("It keeps crashing on start.", "Problems with Beamer"),
        f"files={files('It keeps crashing on start.', 'Problems with Beamer')}",
    )
    expect(
        "lowercase product in the subject: waltr_pro.md is picked",
        "waltr_pro.md" in files("doesn't see my phone", "waltr pro not working"),
    )
    expect(
        "Re:/Fwd: in front of the subject does not hide it",
        "beamer.md" in files("still broken", "RE: Fwd: Re: Beamer"),
    )
    expect(
        "a product named only in the body still routes",
        "beamer.md" in files("Beamer will not cast", "Help"),
    )
    expect(
        "no product anywhere: the catalogue fallback",
        "Softorino_Products.md" in files("hello?", "Question"),
        f"files={files('hello?', 'Question')}",
    )
    expect(
        "subject and body do not run together into a fake word",
        "beamer.md" not in files("mer is the word", "Bea"),
    )

    # End to end: process_single_message hands the subject through.
    original_fetch = bot.fetch_kb_file
    bot.fetch_kb_file = lambda filename: f"KB {filename}"
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    try:
        _service, result = _run_message(
            "It keeps crashing on start.", subject="Problems with Beamer", thread={"messages": []}
        )
        expect(
            "end to end: a subject-only product reaches the KB files used",
            "beamer.md" in (result.get("knowledge_base_files") or []),
            f"files={result.get('knowledge_base_files')}",
        )
        prompt = CLAUDE_CALLS[0]["messages"][0]["content"] if CLAUDE_CALLS else ""
        system = CLAUDE_CALLS[0].get("system", "") if CLAUDE_CALLS else ""
        expect(
            "end to end: the Beamer KB text is in what Claude sees",
            "KB beamer.md" in f"{system}{prompt}",
        )
    finally:
        bot.fetch_kb_file = original_fetch

    return failures


# --- mailing lists and marketing --------------------------------------------
UNSUBSCRIBE = [{"name": "List-Unsubscribe", "value": "<mailto:unsub@promo.example>"}]


def check_bulk_mail():
    failures = []
    expect = _expecter(failures)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content, subject="": ("KB TEXT", ["tone_of_voice.md"])
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ["ESCALATION_EMAIL_1"] = "ops@softorino.app"
    try:
        service, result = _run_message(
            "Grow your SEO 10x this week!",
            subject="Quick question",
            thread={"messages": []},
            extra_headers=UNSUBSCRIBE,
            dry_run=False,
        )
        expect(
            "List-Unsubscribe: skipped as bulk",
            result.get("skipped_reason") == "bulk" and result.get("processed") is False,
            f"result={result!r}",
        )
        expect("List-Unsubscribe: Claude is not called", not CLAUDE_CALLS)
        expect(
            "List-Unsubscribe: nothing is sent",
            not service.customer_sends() and not service.ops_sends(),
        )
        expect(
            "List-Unsubscribe: labelled AI_SKIPPED_SERVICE and read",
            service.added_labels() == [LABEL_IDS["skipped_service"]]
            and "UNREAD" in service.removed_labels(),
            f"added={service.added_labels()}, removed={service.removed_labels()}",
        )
        service, result = _run_message(
            "Your lawyer will love this offer",
            thread={"messages": []},
            extra_headers=UNSUBSCRIBE,
            dry_run=False,
        )
        expect(
            "a bulk mail with a sensitive word does not page ops",
            result.get("skipped_reason") == "bulk" and not service.ops_sends(),
            f"result={result!r}",
        )
        expect(
            "the header name is matched case-insensitively",
            bot.is_bulk_mail([{"name": "list-unsubscribe", "value": "<https://x>"}])
            and not bot.is_bulk_mail([{"name": "Subject", "value": "unsubscribe me"}]),
        )

        mailbox = FakeMailbox([
            ("b1", "tb", 1000, "Black Friday deals"),
            ("ok1", "tok", 1000, "Crash on launch"),
        ])
        mailbox.extra_headers["b1"] = UNSUBSCRIBE
        result = _run(mailbox)
        expect(
            "run level: skipped_bulk counts it and the real mail is answered",
            result.get("skipped_bulk") == 1
            and result.get("skipped_service_mail") == 0
            and len(mailbox.created_drafts) == 1
            and len(CLAUDE_CALLS) == 1,
            f"result={ {k: v for k, v in result.items() if k != 'results'} }",
        )
    finally:
        bot.build_knowledge_base = original_kb

    return failures


# --- removed and renamed ----------------------------------------------------
def check_removed_and_renamed():
    failures = []
    expect = _expecter(failures)

    expect(
        "MAX_THREADS_PER_RUN replaces MAX_EMAILS_PER_RUN",
        getattr(bot, "MAX_THREADS_PER_RUN", None) == 10 and not hasattr(bot, "MAX_EMAILS_PER_RUN"),
    )
    expect(
        "the scan budget still derives from the thread limit",
        bot.MAX_MESSAGES_SCANNED_PER_RUN == bot.MAX_THREADS_PER_RUN * 5,
    )
    expect(
        "diagnostic mode is gone",
        not any(
            hasattr(bot, name)
            for name in ("run_mailbox_diagnostic", "diagnostic_addresses", "DIAGNOSTIC_ADDRESSES_ENV")
        ),
    )
    expect(
        "the one-off diagnostic script is gone",
        not (REPO_ROOT / "scripts" / "diagnose_agent_replies.py").exists(),
    )

    # A leftover variable in Vercel must not stop the bot answering mail.
    mailbox = FakeMailbox([("m1", "t1", 1000, "Crash on launch")])
    previous = os.environ.get("DIAGNOSTIC_ADDRESSES")
    os.environ["DIAGNOSTIC_ADDRESSES"] = "someone@example.com"
    try:
        result = _run(mailbox)
    finally:
        if previous is None:
            os.environ.pop("DIAGNOSTIC_ADDRESSES", None)
        else:
            os.environ["DIAGNOSTIC_ADDRESSES"] = previous
    expect(
        "a stale DIAGNOSTIC_ADDRESSES no longer short-circuits the run",
        result.get("processed_count") == 1 and len(mailbox.created_drafts) == 1,
        f"result keys={sorted(result)}",
    )
    return failures


class _CountingStdout:
    """Passes output through and counts the ok/FAIL lines, so the total at the
    bottom cannot drift from what actually ran."""

    def __init__(self, stream):
        self.stream = stream
        self.checks = 0

    def write(self, text):
        self.checks += sum(
            1 for line in text.splitlines() if line.startswith(("  ok ", "  FAIL "))
        )
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()


def main():
    counter = _CountingStdout(sys.stdout)
    sys.stdout = counter
    try:
        return _run_all(counter)
    finally:
        sys.stdout = counter.stream


def _run_all(counter):
    print("Escalation triggers:")
    failures = check_escalations()
    print("\nEscalation categories:")
    failures += check_categories()

    print("\nEscalation dedup (label based):")
    failures += check_dedup()

    print("\nSensitive content:")
    failures += check_sensitive()

    print("\nSender allow-list (ALLOWED_SENDERS):")
    failures += check_allowed_senders()

    print("\nEnd to end (process_single_message):")
    failures += check_end_to_end()

    print("\nOutgoing mail is stamped:")
    failures += check_outgoing_stamp()

    print("\nHuman agents and service mail:")
    failures += check_human_and_service_gates()

    print("\nSubject normalisation:")
    failures += check_subject_normalisation()

    print("\nUnread queue size:")
    failures += check_unread_total()

    print("\nDRY_RUN behaviour:")
    failures += check_dry_run()

    print("\nAgent lookup by address:")
    failures += check_agent_lookup()

    print("\nMessage age:")
    failures += check_message_age()

    print("\nThread grouping:")
    failures += check_thread_grouping()

    print("\nQuote stripping:")
    failures += check_quote_stripping()

    print("\nSensitive content ahead of the age and agent gates:")
    failures += check_sensitive_gate()

    print("\nSensitive content in DRY_RUN:")
    failures += check_sensitive_dry_run()

    print("\nOps notifications are not agents:")
    failures += check_ops_notification_subjects()

    print("\nFailed agent lookup (AI_CHECK_FAILED):")
    failures += check_lookup_failure_retry()

    print("\nOps notifications that never left:")
    failures += check_ops_notifications_skipped()

    print("\nKnowledge base routing by subject:")
    failures += check_kb_routing_by_subject()

    print("\nMailing lists (List-Unsubscribe):")
    failures += check_bulk_mail()

    print("\nRemoved and renamed:")
    failures += check_removed_and_renamed()

    total = counter.checks
    if failures:
        print(f"\n{len(failures)} of {total} checks failed.")
        return 1
    print(f"\nOK -- {total} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
