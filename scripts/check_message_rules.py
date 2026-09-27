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

LABEL_IDS = {"processing": "L1", "replied": "L2", "escalated": "L3", "failed": "L4"}
ESCALATED_LABEL = LABEL_IDS["escalated"]


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


def _incoming(body, subject="Refund"):
    raw = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": "msg-1",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Andrew Q <andrew@example.com>"},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": "<abc@mail>"},
            ],
            "body": {"data": raw},
        },
    }


def _escalated_thread():
    return {"messages": [{"id": "old", "labelIds": ["SENT", ESCALATED_LABEL]}]}


def _run_message(body, subject="Refund", thread=None):
    service = FakeGmailService(thread)
    CLAUDE_CALLS.clear()
    result = _quiet(
        lambda: bot.process_single_message(service, _incoming(body, subject), True, LABEL_IDS)
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
        + 5  # quote stripping
    )
    if failures:
        print(f"\n{len(failures)} of {total} checks failed.")
        return 1
    print(f"\nOK -- {total} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
