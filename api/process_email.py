"""Vercel entry point for the Softorino support email bot."""

import base64
import json
import os
import re
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


def generate_reply(email_content, subject, knowledge_base):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing environment variable: ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = f"""You are a Softorino customer support agent named Sofi.
Reply in English using ONLY the knowledge base provided below.
Be friendly and concise. Never mention that you are an AI.
Never promise ETAs or refunds. Never offer remote sessions.
Sign off exactly as: Best regards, Softorino Support Team
If the knowledge base does not contain a reliable answer, reply exactly:
Thank you for reaching out. Our support team will review your case and get back to you shortly. We appreciate your patience. Best regards, Softorino Support Team

Knowledge base:
{knowledge_base}
"""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1200,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": f"Customer email subject: {subject}\n\nCustomer email:\n{email_content}",
            }
        ],
    )
    reply = "".join(block.text for block in response.content if block.type == "text").strip()
    if not reply:
        raise RuntimeError("Claude returned an empty reply.")
    return reply


def escalate_email(service, sender, subject, email_content, reason):
    escalation_email = os.getenv("ESCALATION_EMAIL_1")
    if not escalation_email:
        raise RuntimeError("Missing environment variable: ESCALATION_EMAIL_1")

    escalation_body = f"""[ESCALATION NOTIFICATION]

Customer Email: {sender}
Subject: {subject}
Reason: {reason}

--- Original Message ---
{email_content}

--- End of Original Message ---

This ticket requires manual attention from the support team.
"""

    escalation_msg = EmailMessage()
    escalation_msg["To"] = escalation_email
    escalation_msg["From"] = os.getenv("GMAIL_USER_EMAIL", "support@softorino.app")
    escalation_msg["Subject"] = f"[ESCALATION] {subject} — {sender}"
    escalation_msg.set_content(escalation_body)

    encoded_escalation = base64.urlsafe_b64encode(escalation_msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me",
        body={"raw": encoded_escalation},
    ).execute()

    return {
        "escalated": True,
        "recipient": escalation_email,
        "reason": reason,
    }


def process_first_unread_email():
    if os.getenv("DRY_RUN", "true").lower() != "true":
        raise RuntimeError("DRY_RUN must be true while draft-only testing is enabled.")
    service = gmail_service()
    result = (
        service.users()
        .messages()
        .list(
            userId="me",
            q="in:inbox is:unread -in:spam -in:trash",
            maxResults=1,
        )
        .execute()
    )
    messages = result.get("messages", [])
    if not messages:
        return {"processed": False, "message": "No unread inbox email found."}

    message = (
        service.users()
        .messages()
        .get(userId="me", id=messages[0]["id"], format="full")
        .execute()
    )
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

    knowledge_base, kb_files = build_knowledge_base(email_content)
    reply = generate_reply(email_content, subject, knowledge_base)

    draft_message = EmailMessage()
    draft_message["To"] = parseaddr(sender)[1] or sender
    draft_message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if message_id:
        draft_message["In-Reply-To"] = message_id
        draft_message["References"] = f"{references} {message_id}".strip()
    draft_message.set_content(reply)
    encoded_message = base64.urlsafe_b64encode(draft_message.as_bytes()).decode()

    draft = (
        service.users()
        .drafts()
        .create(
            userId="me",
            body={
                "message": {
                    "threadId": message.get("threadId"),
                    "raw": encoded_message,
                }
            },
        )
        .execute()
    )
    service.users().messages().modify(
        userId="me",
        id=message["id"],
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()

    return {
        "processed": True,
        "subject": subject,
        "draft_id": draft["id"],
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
            response = process_first_unread_email()
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
