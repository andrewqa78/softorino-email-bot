#!/usr/bin/env python3
"""One-off diagnostic: are Groove-sent agent replies in the Gmail mailbox at all?

The bot answered on top of agent Amy in two tickets. Its "a human replied in
this thread" gate looks at the Gmail thread, and the agent replies were not in
it. Before anything is built on top of Gmail, one question has to be settled:

    Do the agents' replies exist in support@softorino.app in ANY form?

If Groove sends through its own SMTP and the mail never lands in the mailbox,
then no Gmail-side check can work, whatever shape it takes, and the fix has to
go through the Groove API instead.

This script is read-only. It lists and reads message metadata, nothing else.

Usage::

    export GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... GMAIL_REFRESH_TOKEN=...
    python scripts/diagnose_agent_replies.py
    python scripts/diagnose_agent_replies.py other@customer.com --days 30

The credentials are the same three the bot uses in Vercel. Nothing is printed
from them.
"""

import argparse
import os
import sys

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
except ImportError:
    sys.exit(
        "google-api-python-client is not installed.\n"
        "  python3 -m venv .venv && source .venv/bin/activate\n"
        "  python -m pip install -r requirements.txt"
    )

# Both customers from the two broken tickets.
DEFAULT_CUSTOMERS = [
    "johnsonwjsn@gmail.com",   # Willie Johnson, OrderID UxGCq8ouRE-OpAbq8ScHxQ
    "bisaillonfamily@gmail.com",  # Mike Bisaillon, folder colorizer activation
]
BOT_HEADER_NAME = "X-Softorino-Bot"
WANTED_HEADERS = ["Date", "From", "To", "Subject", "Message-ID", BOT_HEADER_NAME]


def gmail_service():
    required = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        sys.exit(
            f"Missing environment variables: {', '.join(missing)}.\n"
            "Take the same values the bot uses in Vercel and export them here."
        )
    credentials = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def header_value(headers, name):
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def fetch(service, query, days):
    """List a query and pull metadata for every hit."""
    listing = (
        service.users()
        .messages()
        .list(userId="me", q=f"{query} newer_than:{days}d", maxResults=100)
        .execute()
    )
    rows = []
    for ref in listing.get("messages", []):
        message = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=WANTED_HEADERS,
            )
            .execute()
        )
        headers = message.get("payload", {}).get("headers", [])
        rows.append(
            {
                "id": message.get("id", ""),
                "threadId": message.get("threadId", ""),
                "date": header_value(headers, "Date"),
                "from": header_value(headers, "From"),
                "to": header_value(headers, "To"),
                "subject": header_value(headers, "Subject"),
                "message_id": header_value(headers, "Message-ID"),
                "bot_header": header_value(headers, BOT_HEADER_NAME),
            }
        )
    return rows


def shorten(text, width):
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(rows):
    if not rows:
        print("  (nothing found)")
        return
    columns = [
        ("threadId", "threadId", 18),
        ("date", "Date", 26),
        ("from", "From", 30),
        ("to", "To", 28),
        ("subject", "Subject", 34),
        ("bot_header", BOT_HEADER_NAME, 14),
    ]
    header = "  " + " | ".join(title.ljust(width) for _, title, width in columns)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        print(
            "  "
            + " | ".join(shorten(row[key], width).ljust(width) for key, _, width in columns)
        )


def analyse(customer, rows, own_email):
    """Answer the three questions for one customer."""
    inbound = [r for r in rows if customer.lower() in (r["from"] or "").lower()]
    outbound = [r for r in rows if customer.lower() not in (r["from"] or "").lower()]
    unstamped = [r for r in outbound if not r["bot_header"]]
    stamped = [r for r in outbound if r["bot_header"]]

    customer_threads = {r["threadId"] for r in inbound}
    unstamped_threads = {r["threadId"] for r in unstamped}

    print()
    print(f"  Q1  Outgoing mail to this customer that is NOT from the bot: {len(unstamped)}")
    if unstamped:
        senders = sorted({shorten(r["from"], 60) for r in unstamped})
        for sender in senders:
            print(f"        from: {sender}")
    else:
        print("        none -- no agent reply reached this mailbox")

    print(f"  Q2  Customer threads: {len(customer_threads)}; "
          f"threads holding those outgoing messages: {len(unstamped_threads)}")
    if unstamped and customer_threads:
        shared = unstamped_threads & customer_threads
        if shared:
            print(f"        SAME thread as the customer for {len(shared)} of them: {sorted(shared)}")
        outside = unstamped_threads - customer_threads
        if outside:
            print(f"        DIFFERENT thread for {len(outside)} of them: {sorted(outside)}")

    print(f"  Q3  {BOT_HEADER_NAME} present on {len(stamped)} outgoing, "
          f"absent on {len(unstamped)}")
    print(f"        (absent is the agent signature; the bot always stamps its own mail)")

    return {
        "customer": customer,
        "total": len(rows),
        "inbound": len(inbound),
        "outbound": len(outbound),
        "unstamped": len(unstamped),
        "stamped": len(stamped),
        "same_thread": len(unstamped_threads & customer_threads) if customer_threads else 0,
        "other_thread": len(unstamped_threads - customer_threads) if customer_threads else 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "customers",
        nargs="*",
        default=DEFAULT_CUSTOMERS,
        help="customer addresses to inspect (default: the two broken tickets)",
    )
    parser.add_argument("--days", type=int, default=14, help="how far back to look (default 14)")
    args = parser.parse_args()
    customers = args.customers or DEFAULT_CUSTOMERS

    service = gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    own_email = (profile.get("emailAddress") or "").strip()
    print(f"Mailbox: {own_email}")
    print(f"Window : last {args.days} days")

    summaries = []
    for customer in customers:
        print()
        print("=" * 100)
        print(f"{customer}  --  everything addressed TO them")
        print("=" * 100)
        rows = fetch(service, f"to:{customer}", args.days)
        print_table(rows)
        summaries.append(analyse(customer, rows, own_email))

        # Anything they sent us, to line the threads up.
        print()
        print(f"{customer}  --  everything FROM them")
        print("-" * 100)
        print_table(fetch(service, f"from:{customer}", args.days))

    print()
    print("=" * 100)
    print("VERDICT")
    print("=" * 100)
    total_unstamped = sum(s["unstamped"] for s in summaries)
    for summary in summaries:
        print(
            f"  {summary['customer']}: {summary['total']} message(s), "
            f"{summary['unstamped']} outgoing without {BOT_HEADER_NAME} "
            f"({summary['same_thread']} in the customer's thread, "
            f"{summary['other_thread']} in another)"
        )
    print()
    if total_unstamped:
        print("  Agent replies DO reach this mailbox. A Gmail-side check can work:")
        print("  go with step 2 (search by customer address), not the Groove API.")
    else:
        print("  NO agent replies reached this mailbox in the window.")
        print("  A Gmail-side check cannot see them no matter how it is written:")
        print("  the fix has to go through the Groove API (section 6).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
