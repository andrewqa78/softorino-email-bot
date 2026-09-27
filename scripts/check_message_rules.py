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

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    sys.modules["anthropic"].Anthropic = object

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


def main():
    print("Escalation triggers:")
    failures = check_escalations()
    print("\nSensitive content:")
    failures += check_sensitive()

    print("\nQuote stripping:")
    failures += check_quote_stripping()

    total = len(ESCALATION_CASES) + len(SENSITIVE_CASES) + 5
    if failures:
        print(f"\n{len(failures)} of {total} checks failed.")
        return 1
    print(f"\nOK -- {total} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
