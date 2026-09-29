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

    def __init__(self, thread=None):
        self.actions = []
        self._thread = thread or {"messages": []}

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

    def get(self, userId=None, id=None, format=None):
        return _Call(self._thread)

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


def _epoch_ms_hours_ago(hours):
    return str(int((time.time() - hours * 3600) * 1000))


def _incoming(
    body,
    subject="Refund",
    sender="Andrew Q <andrew@example.com>",
    age_hours=0.5,
    internal_date=_UNSET,
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
):
    service = FakeGmailService(thread)
    CLAUDE_CALLS.clear()
    incoming = _incoming(body, subject, sender, age_hours, internal_date)
    result = _quiet(
        lambda: bot.process_single_message(
            service, incoming, True, LABEL_IDS, own_email=OWN_MAILBOX
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
    bot.build_knowledge_base = lambda content: ("KB TEXT", ["tone_of_voice.md"])
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
            "rule escalation notifies ops",
            service.ops_notification() is not None,
        )
        expect(
            "rule escalation labels the message AI_ESCALATED",
            service.added_labels() == [ESCALATED_LABEL],
            f"added={service.added_labels()}",
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
            "sensitive content notifies ops",
            service.ops_notification() is not None,
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
            "normal reply is labelled AI_REPLIED",
            service.added_labels() == [LABEL_IDS["replied"]],
            f"added={service.added_labels()}",
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
        self.thread_messages = []
        self.sender = "Angry Customer <angry@example.com>"
        self.sent = []
        self.modifies = []
        self.list_calls = []

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
        return _Call({"messages": self.mb.refs[:maxResults]})

    def get(self, userId=None, id=None, format=None):
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
    bot.build_knowledge_base = lambda content: ("KB TEXT", ["tone_of_voice.md"])
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


def _run(mailbox):
    CLAUDE_CALLS.clear()
    with _run_environment(mailbox):
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
    bot.build_knowledge_base = lambda content: ("KB TEXT", ["tone_of_voice.md"])
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
        expect(
            "an unreadable thread is treated as human-handled",
            _quiet(lambda: bot.thread_has_human_reply(broken, "t1", OWN_MAILBOX)) is True,
        )
        expect(
            "an unknown own address is treated as human-handled",
            _quiet(lambda: bot.thread_has_human_reply(FakeGmailService(), "t1", "")) is True,
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
    bot.build_knowledge_base = lambda content: ("KB TEXT", ["tone_of_voice.md"])
    try:
        service, _result = _run_message("My app will not start", thread={"messages": []})
        reply = service.customer_reply() or ""
        expect(
            "the customer reply carries the bot header",
            f"{bot.BOT_HEADER_NAME}: {bot.BOT_HEADER_VALUE}" in reply,
            f"headers={reply.split(chr(10) * 2)[0]!r}",
        )
        service, _result = _run_message(
            "Just refund me, I don't want troubleshooting", thread={"messages": []}
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


def check_message_age():
    failures = []

    def expect(label, condition, detail=""):
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    original_kb = bot.build_knowledge_base
    bot.build_knowledge_base = lambda content: ("KB TEXT", ["tone_of_voice.md"])
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
            "a stale message loses UNREAD and gains AI_SKIPPED_STALE",
            "UNREAD" not in mailbox.labels_of("old1")
            and "id-AI_SKIPPED_STALE" in mailbox.labels_of("old1"),
            f"labels={mailbox.labels_of('old1')}",
        )
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
        "older messages get the answered message's label and lose UNREAD",
        all(
            "id-AI_REPLIED" in mailbox.labels_of(m) and "UNREAD" not in mailbox.labels_of(m)
            for m in ("m1", "m3")
        ),
        f"m1={mailbox.labels_of('m1')}, m3={mailbox.labels_of('m3')}",
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
        mailbox.list_calls[0]["maxResults"] > bot.MAX_EMAILS_PER_RUN,
        f"maxResults={mailbox.list_calls[0]['maxResults']}, limit={bot.MAX_EMAILS_PER_RUN}",
    )

    # 6. Thread count, not message count, is what the limit caps.
    many = []
    for index in range(bot.MAX_EMAILS_PER_RUN + 3):
        many.append((f"g{index}", f"tg{index}", 1000, "Question"))
    mailbox = FakeMailbox(many)
    result = _run(mailbox)
    expect(
        "no more than MAX_EMAILS_PER_RUN threads per run",
        result.get("processed_count") == bot.MAX_EMAILS_PER_RUN,
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


def main():
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

    print("\nMessage age:")
    failures += check_message_age()

    print("\nThread grouping:")
    failures += check_thread_grouping()

    print("\nQuote stripping:")
    failures += check_quote_stripping()

    total = (
        len(ESCALATION_CASES)
        + len(CATEGORY_CASES)
        + len(DEDUP_CASES)
        + 2  # degenerate dedup inputs
        + len(SENSITIVE_CASES)
        + 12  # sender allow-list
        + 13  # end to end
        + 14  # thread grouping
        + 2   # outgoing stamp
        + 31  # human / service gates
        + 21  # message age
        + 5  # quote stripping
    )
    if failures:
        print(f"\n{len(failures)} of {total} checks failed.")
        return 1
    print(f"\nOK -- {total} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
