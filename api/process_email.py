"""Vercel entry point for the Softorino support email bot."""

import os
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    """Process one support email request.

    Implementation will be added in the next setup step.
    """

    def do_GET(self):
        mailbox = os.getenv("GMAIL_USER_EMAIL", "not configured")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            f"Softorino email bot is ready. Mailbox: {mailbox}".encode("utf-8")
        )

    def do_POST(self):
        self.send_response(501)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Email processing is not implemented yet.")
