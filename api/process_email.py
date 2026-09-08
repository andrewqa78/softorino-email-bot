"""Vercel entry point for the Softorino support email bot."""

import base64
import json
import os
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]
TEST_DRAFT_TEXT = "Test draft from Softorino Bot"


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


def process_first_unread_email():
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
        .get(userId="me", id=messages[0]["id"], format="metadata")
        .execute()
    )
    headers = message.get("payload", {}).get("headers", [])
    sender = header_value(headers, "Reply-To") or header_value(headers, "From")
    subject = header_value(headers, "Subject") or "(no subject)"
    message_id = header_value(headers, "Message-ID")
    references = header_value(headers, "References")
    if not sender:
        raise RuntimeError("Unread email does not contain a sender address.")

    draft_message = EmailMessage()
    draft_message["To"] = sender
    draft_message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if message_id:
        draft_message["In-Reply-To"] = message_id
        draft_message["References"] = f"{references} {message_id}".strip()
    draft_message.set_content(TEST_DRAFT_TEXT)
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
