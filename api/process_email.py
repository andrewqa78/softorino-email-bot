"""Vercel entry point for the Softorino support email bot."""

import base64
import json
import os
import re
import time
from email.message import EmailMessage
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler
from html.parser import HTMLParser
from urllib.request import Request as UrlRequest, urlopen

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]
KB_BASE_URL = (
    "https://raw.githubusercontent.com/andrewqa78/Softorino_Support_AI/main/"
    "knowledge_base/"
)
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_EMAIL_CHARS = 30000
TEST_SENDER_EMAIL = "andrewsupport78@gmail.com"

MAX_EMAILS_PER_RUN = 5
PROCESS_WINDOW_DAYS = 7
DELAY_BETWEEN_EMAILS_SECONDS = 1
EXCLUDED_SENDER_TERMS = ["noreply", "no-reply", "mailer-daemon"]
EXCLUDED_SUBJECT_TERMS = [
    "unsubscribe", "newsletter", "notification", "invoice",
    "receipt", "order confirmation", "auto-reply", "out of office",
]


def build_unread_query():
    terms = [
        "in:inbox", "is:unread", "-in:spam", "-in:trash",
        f"newer_than:{PROCESS_WINDOW_DAYS}d",
        f"from:{TEST_SENDER_EMAIL}",
    ]
    terms += [f"-from:{term}" for term in EXCLUDED_SENDER_TERMS]
    terms += [f'-subject:"{term}"' for term in EXCLUDED_SUBJECT_TERMS]
    return " ".join(terms)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        return " ".join(" ".join(self.parts).split())


def gmail_service():
    required = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    credentials = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=GMAIL_SCOPES,
    )
    credentials.refresh(Request())
    if not credentials.valid:
        raise RuntimeError("Gmail OAuth credentials are not valid.")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def header_value(headers, name):
    name = name.lower()
    for header in headers:
        if header.get("name", "").lower() == name:
            return header.get("value", "")
    return ""


def decode_part_body(part):
    body = part.get("body", {}).get("data")
    if not body:
        return ""
    decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    return decoded.decode("utf-8", errors="replace")


def message_text(payload):
    plain_parts = []
    html_parts = []

    def collect(part):
        mime_type = part.get("mimeType", "")
        if part.get("filename"):
            return
        if mime_type == "text/plain":
            plain_parts.append(decode_part_body(part))
        elif mime_type == "text/html":
            html_parts.append(decode_part_body(part))
        for child in part.get("parts", []):
            collect(child)

    collect(payload)
    text = "\n".join(part for part in plain_parts if part.strip()).strip()
    if not text and html_parts:
        extractor = TextExtractor()
        extractor.feed("\n".join(html_parts))
        text = extractor.text()
    return text.strip()


QUOTE_BOUNDARY_PATTERNS = [
    re.compile(r"^\s*On .{0,120}?wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*From:\s.+$", re.MULTILINE),
    re.compile(r"^\s*>", re.MULTILINE),
]


def split_latest_message(text):
    """Split an email body into (latest reply, quoted thread history).

    Reply clients append the full quoted thread below the new text, so
    without this the rule-based checks (escalation, sensitive content)
    would react to an older message in the thread instead of what the
    customer just wrote.
    """
    cut_at = len(text)
    for pattern in QUOTE_BOUNDARY_PATTERNS:
        match = pattern.search(text)
        if match:
            cut_at = min(cut_at, match.start())
    latest = text[:cut_at].strip()
    older = text[cut_at:].strip()
    return (latest or text.strip()), older


ESCALATION_MARKER_PATTERN = re.compile(r"\n?\[\[ESCALATE:\s*(.*?)\]\]\s*$", re.IGNORECASE | re.DOTALL)


def extract_escalation_marker(reply):
    """Strip Claude's trailing [[ESCALATE: reason]] marker, if present.

    Claude's KB-approved reply templates (e.g. "I've forwarded this to our
    billing team") never contain the hardcoded fallback sentence, so without
    this marker the bot had no way to know a KB-templated reply also needs an
    ops notification. Returns (customer_facing_reply, reason_or_none).
    """
    match = ESCALATION_MARKER_PATTERN.search(reply)
    if not match:
        return reply.strip(), None
    clean_reply = reply[: match.start()].rstrip()
    reason = match.group(1).strip() or "Claude flagged this reply for escalation"
    return clean_reply, reason


ESCALATION_NOTICE_PHRASE = "forwarded your case to our support team"


def ensure_escalation_notice(reply):
    """Guarantee the canonical escalation phrase appears in the customer reply.

    Claude's own KB-approved phrasing varies ("I've forwarded this to our
    billing team", etc.), which is too unreliable to search for later. Adding
    this exact, fixed sentence whenever we escalate lets thread_already_escalated()
    detect "we already escalated this thread" deterministically.
    """
    if ESCALATION_NOTICE_PHRASE in reply.lower():
        return reply
    return f"{reply}\n\nWe have forwarded your case to our support team for further review."


def thread_already_escalated(service, thread_id):
    """Check the thread for a prior reply that already notified the customer
    of an escalation, so we don't notify ops again for the same open case."""
    if not thread_id:
        return False
    own_email = (os.getenv("GMAIL_USER_EMAIL") or "").strip().lower()
    if not own_email:
        return False
    try:
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    except Exception as error:
        print(f"[ESCALATION] Could not read thread {thread_id} for dedup check: {error}")
        return False
    for thread_message in thread.get("messages", []):
        headers = thread_message.get("payload", {}).get("headers", [])
        sender = header_value(headers, "From").lower()
        if own_email not in sender:
            continue
        body = message_text(thread_message.get("payload", {})).lower()
        if ESCALATION_NOTICE_PHRASE in body:
            return True
    return False


def fetch_kb_file(filename):
    request = UrlRequest(KB_BASE_URL + filename, headers={"User-Agent": "Softorino-Email-Bot"})
    with urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


def relevant_kb_files(email_content):
    content = email_content.lower()
    files = ["active_product_issues.md", "global_rules.md"]
    routing = {
        "waltr_pro.md": ("waltr", "waltr pro"),
        "syc_pro.md": ("syc", "youtube converter"),
        "alttunes.md": ("alttunes",),
        "iringg.md": ("iringg",),
        "activation_and_license.md": ("activation", "license", "subscription", "dashboard"),
        "Softorino_Billing_and_Payments.md": ("refund", "charge", "billing", "payment", "cancel"),
        "other_products.md": ("beamer", "folder colorizer", "picfindr", "cleanappsnow"),
    }
    for filename, keywords in routing.items():
        if any(re.search(rf"\b{re.escape(keyword)}\b", content) for keyword in keywords):
            files.append(filename)
    if len(files) == 2:
        files.append("Softorino_Products.md")
    return files


def build_knowledge_base(email_content):
    files = relevant_kb_files(email_content)
    sections = []
    for filename in files:
        sections.append(f"\n--- {filename} ---\n{fetch_kb_file(filename)}")
    return "".join(sections), files


def is_auto_reply(subject, sender):
    """Check if email is an auto-reply or bounce-back."""
    auto_reply_keywords = ["out of office", "auto-reply", "automatic reply", "delivery failed", "undeliverable", "mail delivery"]
    auto_reply_senders = ["mailer-daemon@", "postmaster@", "noreply@", "no-reply@"]
    subject_lower = subject.lower()
    if any(keyword in subject_lower for keyword in auto_reply_keywords):
        return True
    if any(sender.lower().startswith(prefix) for prefix in auto_reply_senders):
        return True
    return False


def detect_escalation_triggers(email_content):
    """Detect HARD escalation triggers only: fraud and explicit help refusal."""
    content = email_content.lower()

    # Fraud/scam detection (HIGH PRIORITY) - always escalate
    fraud_keywords = ["scam", "fraud", "you scammed", "you lied", "stolen"]
    for keyword in fraud_keywords:
        if keyword in content:
            return {"should_escalate": True, "reason": "Fraud/Scam Report", "priority": "HIGH PRIORITY"}

    # Explicit refusal to accept help - customer wants ONLY refund/cancellation, not troubleshooting
    hard_refusal_patterns = [
        r"just refund",
        r"only refund",
        r"don't want help",
        r"don't want support",
        r"no troubleshooting",
        r"no support",
        r"skip the help",
        r"i just want.*refund",
        r"please cancel.*no.*help",
    ]
    for pattern in hard_refusal_patterns:
        if re.search(pattern, content):
            return {"should_escalate": True, "reason": "Refund Request (Customer Refuses Help)", "priority": "NORMAL"}

    # Unauthorized charges (technical fraud)
    if re.search(r"(unauthorized|didn't authorize|didn't make this|i didn't buy)", content):
        return {"should_escalate": True, "reason": "Unauthorized Charge", "priority": "NORMAL"}

    # Soft refund mentions → do NOT escalate, let Claude handle
    # "I want a refund" + "can you help?" = offer help first
    return {"should_escalate": False}


def detect_sensitive_content(email_content, subject):
    """Detect explicit threats, legal language, or profanity only.

    Word-boundary matching on purpose: plain substring checks previously
    flagged normal words like "issue" (contains "sue") and "courtesy"
    (contains "court") as sensitive.
    """
    combined = (email_content + " " + subject).lower()
    sensitive_patterns = [
        r"\blawyer\b", r"\battorney\b", r"\bsue you\b", r"\blawsuit\b",
        r"\blegal action\b", r"\bpress charges\b", r"\bcourt\b",
        r"\bpolice\b", r"\bfbi\b", r"\bblackmail\b",
        r"\bdeath threat\b", r"\bkill you\b", r"\bi(?:'ll| will) kill\b", r"\bhurt you\b",
        r"\bfuck\w*\b", r"\bshit\w*\b", r"\basshole\w*\b", r"\bbastard\w*\b",
        r"\bbitch\w*\b", r"\bcunt\w*\b",
    ]
    return any(re.search(pattern, combined) for pattern in sensitive_patterns)



def generate_reply(latest_message, subject, knowledge_base, thread_context="", max_retries=2):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing environment variable: ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = f"""You are a Softorino customer support agent named Sofi.
Reply in the same language as the customer's email using ONLY the knowledge base provided below.
Be friendly and concise. Never mention that you are an AI.
Never promise ETAs or refunds. Never offer remote sessions.
Sign off exactly as: Best regards, Softorino Support Team

IMPORTANT - Plain text only:
Gmail does not render Markdown, so never use Markdown formatting.
Never use ** or * for bold or italic emphasis.
Never use Markdown bullet points (-, *, +).
If you need a list, use numbered lines like "1. 2. 3." instead.
Write everything as plain text.

IMPORTANT - Natural formatting:
Write in natural paragraphs of 2-4 sentences each, the way a person writes an email.
Do not put every sentence on its own line.
Keep the whole reply to 3-4 paragraphs maximum.
Keep the tone warm but professional, not robotic.

IMPORTANT - Escalation decisions use the latest message only:
Base your reply and any escalation/fallback decision ONLY on the customer's
latest message below. Earlier thread history is provided for context only —
e.g. to avoid repeating troubleshooting steps already suggested. Never treat
an older message in the thread (such as a past refund request) as the
customer's current request if the latest message asks something else.

IMPORTANT - Refund/Cancellation Requests:
When customer mentions refund or cancellation, first check if they're open to help:
- If they ask "can you help?", "is there a solution?", "what can I do?" → offer troubleshooting
- If they show willingness to fix the issue → guide them with solutions from KB
- Only use fallback (escalation) if customer explicitly refuses help or issue is clearly unfixable

Example good response:
"I understand you'd like a refund. Let's first try [solution from KB]. 
If that doesn't work, our billing team can help with next steps."

Only use the fallback response below if:
1. Customer explicitly refuses help (says "no troubleshooting", "just refund me", etc.)
2. The issue is clearly a billing/refund matter that KB can't resolve

If the knowledge base does not contain a reliable answer, reply exactly:
Thank you for reaching out. Our support team will review your case and get back to you shortly. We appreciate your patience. Best regards, Softorino Support Team

IMPORTANT - Escalation marker (use sparingly):
Only add this marker when you explicitly cannot help the customer any
further yourself and a human agent must take over now -- e.g. a billing/
refund/cancellation action trigger that requires internal system access,
a known bug with no workaround, or troubleshooting that is genuinely
exhausted. Do NOT add it as a precaution, and do NOT add it again in a
thread where you already added it and are still actively guiding the
customer through next steps -- only when this specific reply is the point
where you hand off to a human.
When it applies, write your normal customer-facing reply using the
approved KB templates, then add ONE extra final line by itself, in
exactly this format:
[[ESCALATE: short reason]]
This line is stripped before the customer sees the email -- it only signals
our support team to also get notified. Do not add it for cases you fully
resolve yourself (activation steps, explanations, standard troubleshooting).

Knowledge base:
{knowledge_base}
"""
    user_content = f"Customer email subject: {subject}\n\n"
    if thread_context:
        user_content += (
            "Earlier thread history (context only — do not base the "
            f"escalation decision on this):\n{thread_context}\n\n"
        )
    user_content += (
        "Customer's latest message (base your reply and escalation decision "
        f"on this):\n{latest_message}"
    )

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1200,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": user_content,
                    }
                ],
            )
            reply = "".join(block.text for block in response.content if block.type == "text").strip()
            if not reply:
                raise RuntimeError("Claude returned an empty reply.")
            return reply
        except Exception as e:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Claude API failed after {max_retries} attempts: {e}")
            time.sleep(3)


def escalate_email(service, sender, subject, email_content, reason, priority="NORMAL"):
    recipients = [
        email
        for email in (os.getenv("ESCALATION_EMAIL_1"), os.getenv("ESCALATION_EMAIL_2"))
        if email
    ]
    if not recipients:
        print(
            f"[ESCALATION] SKIPPED — no ESCALATION_EMAIL_1/ESCALATION_EMAIL_2 configured. "
            f"reason={reason!r} priority={priority}"
        )
        return {"escalated": False, "recipients": [], "reason": reason, "priority": priority}

    print("Sending escalation to: ", recipients)
    print(
        f"[ESCALATION] Attempting notification to {recipients} — "
        f"reason={reason!r} priority={priority}"
    )

    subject_prefix = f"[{priority}] " if priority != "NORMAL" else "[ESCALATION] "
    escalation_body = f"""[ESCALATION NOTIFICATION]
Priority: {priority}

Customer Email: {sender}
Subject: {subject}
Reason: {reason}

--- Original Message ---
{email_content[:2000]}

--- End of Original Message ---

This ticket requires manual attention from the support team.
"""

    escalation_msg = EmailMessage()
    escalation_msg["To"] = ", ".join(recipients)
    escalation_msg["From"] = os.getenv("GMAIL_USER_EMAIL", "support@softorino.app")
    escalation_msg["Subject"] = f"{subject_prefix}{subject} — {sender}"
    escalation_msg.set_content(escalation_body)

    encoded_escalation = base64.urlsafe_b64encode(escalation_msg.as_bytes()).decode()
    try:
        # No threadId/In-Reply-To/References set above: this is always sent
        # as a brand-new message/thread to the ops team, never as a reply in
        # the customer's thread.
        service.users().messages().send(
            userId="me",
            body={"raw": encoded_escalation},
        ).execute()
    except Exception as error:
        print(f"[ESCALATION] FAILED to send notification to {recipients}: {error}")
        return {
            "escalated": False,
            "recipients": recipients,
            "reason": reason,
            "priority": priority,
            "error": str(error),
        }

    print(f"[ESCALATION] SENT notification to {recipients}")
    return {
        "escalated": True,
        "recipients": recipients,
        "reason": reason,
        "priority": priority,
    }


def deliver_reply(service, draft_message, thread_id, dry_run):
    """Create a Gmail draft (DRY_RUN) or send the reply immediately (live)."""
    encoded_message = base64.urlsafe_b64encode(draft_message.as_bytes()).decode()
    body = {"threadId": thread_id, "raw": encoded_message}
    if dry_run:
        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": body})
            .execute()
        )
        return {"mode": "draft", "id": draft["id"]}
    sent = service.users().messages().send(userId="me", body=body).execute()
    return {"mode": "sent", "id": sent["id"]}


def process_unread_emails():
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    service = gmail_service()
    result = (
        service.users()
        .messages()
        .list(
            userId="me",
            q=build_unread_query(),
            maxResults=MAX_EMAILS_PER_RUN,
        )
        .execute()
    )
    message_refs = result.get("messages", [])
    if not message_refs:
        return {"processed_count": 0, "results": [], "message": "No unread inbox email found."}

    results = []
    for index, message_ref in enumerate(message_refs):
        if index > 0:
            time.sleep(DELAY_BETWEEN_EMAILS_SECONDS)
        message = (
            service.users()
            .messages()
            .get(userId="me", id=message_ref["id"], format="full")
            .execute()
        )
        try:
            results.append(process_single_message(service, message, dry_run))
        except Exception as error:
            results.append({"processed": False, "error": str(error)})

    return {"processed_count": len(results), "results": results}


def process_single_message(service, message, dry_run):
    headers = message.get("payload", {}).get("headers", [])
    sender = header_value(headers, "Reply-To") or header_value(headers, "From")
    subject = header_value(headers, "Subject") or "(no subject)"
    message_id = header_value(headers, "Message-ID")
    references = header_value(headers, "References")
    email_content = message_text(message.get("payload", {}))[:MAX_EMAIL_CHARS]
    if not sender:
        raise RuntimeError("Unread email does not contain a sender address.")
    if not email_content:
        raise RuntimeError("Unread email does not contain readable text.")

    # Reply clients append quoted thread history below the new text. Rule-based
    # checks below must react to what the customer just wrote, not to an older
    # message quoted further down (e.g. a past refund request in the thread).
    latest_message, thread_context = split_latest_message(email_content)

    # Check for auto-reply/bounce-back
    if is_auto_reply(subject, sender):
        service.users().messages().modify(
            userId="me",
            id=message["id"],
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return {"processed": False, "message": "Auto-reply or bounce-back detected. Skipped."}

    # Check for sensitive content (threats/legal language)
    if detect_sensitive_content(latest_message, subject):
        escalation_result = escalate_email(service, sender, subject, email_content, "SENSITIVE: Threats or legal language detected", priority="SENSITIVE")
        service.users().messages().modify(
            userId="me",
            id=message["id"],
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return {
            "processed": False,
            "message": "Sensitive content detected. Escalated without auto-reply.",
            "escalation": escalation_result,
        }

    # Check for escalation triggers
    escalation_check = detect_escalation_triggers(latest_message)
    if escalation_check["should_escalate"]:
        knowledge_base, kb_files = build_knowledge_base(email_content)
        escalation_result = escalate_email(service, sender, subject, email_content, escalation_check["reason"], priority=escalation_check["priority"])
        service.users().messages().modify(
            userId="me",
            id=message["id"],
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return {
            "processed": False,
            "message": "Escalation trigger detected. Escalated to ops team.",
            "escalation_reason": escalation_check["reason"],
            "knowledge_base_files": kb_files,
            "escalation": escalation_result,
        }

    # Generate reply
    knowledge_base, kb_files = build_knowledge_base(email_content)
    raw_reply = generate_reply(latest_message, subject, knowledge_base, thread_context)
    reply, marker_reason = extract_escalation_marker(raw_reply)

    # Escalate if Claude flagged this reply (KB-templated escalation, e.g.
    # billing action triggers or a known bug) or used the no-answer fallback.
    is_fallback_reply = "our support team will review your case" in reply.lower()
    if marker_reason or is_fallback_reply:
        escalation_reason = marker_reason or "Claude fallback: No KB answer found"
        thread_id = message.get("threadId")
        if thread_already_escalated(service, thread_id):
            print(f"[ESCALATION] Thread {thread_id} was already escalated earlier — skipping duplicate ops notification.")
            escalation_result = {
                "escalated": False,
                "recipients": [],
                "reason": escalation_reason,
                "priority": "NORMAL",
                "skipped_duplicate": True,
            }
        else:
            escalation_result = escalate_email(service, sender, subject, email_content, escalation_reason, priority="NORMAL")
            reply = ensure_escalation_notice(reply)
        # Still send/draft the customer-facing reply
        draft_message = EmailMessage()
        draft_message["To"] = parseaddr(sender)[1] or sender
        draft_message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if message_id:
            draft_message["In-Reply-To"] = message_id
            draft_message["References"] = f"{references} {message_id}".strip()
        draft_message.set_content(reply)
        delivery = deliver_reply(service, draft_message, message.get("threadId"), dry_run)
        service.users().messages().modify(
            userId="me",
            id=message["id"],
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return {
            "processed": True,
            "subject": subject,
            "delivery": delivery,
            "knowledge_base_files": kb_files,
            "escalated": True,
            "escalation_reason": escalation_reason,
            "escalation": escalation_result,
        }

    # Deliver reply (draft in DRY_RUN, sent live otherwise)
    draft_message = EmailMessage()
    draft_message["To"] = parseaddr(sender)[1] or sender
    draft_message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if message_id:
        draft_message["In-Reply-To"] = message_id
        draft_message["References"] = f"{references} {message_id}".strip()
    draft_message.set_content(reply)
    delivery = deliver_reply(service, draft_message, message.get("threadId"), dry_run)
    service.users().messages().modify(
        userId="me",
        id=message["id"],
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()

    return {
        "processed": True,
        "subject": subject,
        "delivery": delivery,
        "knowledge_base_files": kb_files,
    }


class handler(BaseHTTPRequestHandler):
    """Process one support email request.

    Implementation will be added in the next setup step.
    """

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Softorino email bot is ready.")

    def do_POST(self):
        try:
            response = process_unread_emails()
            self._write_json(200, response)
        except Exception as error:
            self._write_json(
                500,
                {"processed": False, "error": f"Gmail processing failed: {error}"},
            )

    def _write_json(self, status_code, payload):
        response = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
