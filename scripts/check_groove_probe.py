#!/usr/bin/env python3
"""Tests for api/groove_probe.py, on mocked Groove responses.

The probe's response lands in public Actions logs, so the failures that matter
are leaks and writes, not wrong numbers:

* any fake name, email, subject, message text or id planted in the mocked Groove
  data must be absent from the output
* the Groove token must be absent from every output, success or error
* the function must only ever send GET to Groove
* a missing token, a 401, a 429 or a timeout must give a short error, not a
  traceback

Stdlib only, no network. Usage::

    python scripts/check_groove_probe.py
"""

import ast
import contextlib
import io
import json
import os
import socket
import sys
import threading
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "api" / "groove_probe.py"
sys.path.insert(0, str(REPO_ROOT / "api"))
sys.dont_write_bytecode = True

import groove_probe as probe  # noqa: E402

TOKEN = "gt_SECRETTOKEN_9f8e7d6c"
CRON = "cron_SECRET_1a2b3c"
AUTH = f"Bearer {CRON}"
ENV = {"CRON_SECRET": CRON, "GROOVE_API_TOKEN": TOKEN}

# Everything below must never appear in any output.
PII = [
    "Zorblat Quimby", "zorblat.quimby@example-leak.test", "Quimby",
    "Refund for order 998877 please", "My card ending 4242 was charged twice",
    "agent.wilhelmina@softorino-leak.test", "Wilhelmina", "cust-id-5550123",
    "Billing dispute for Quimby", "tag_for_quimby_only", "Fenwick Group",
    "Quimby login issue",
]


def _href(kind, who):
    return f"https://api.groovehq.com/v1/{kind}/{who}"


def make_ticket(number, tags, assigned=True, title=None, closed_by=None):
    ticket = {
        "number": number,
        "title": title or "Refund for order 998877 please",
        "summary": "My card ending 4242 was charged twice",
        "state": "opened",
        "priority": None,
        "starred": False,
        "assigned_group": "Fenwick Group",
        "closed_by": closed_by,
        "tags": tags,
        "message_count": 3,
        "customer_name": "Zorblat Quimby",
        "created_at": "2026-11-10T09:15:00Z",
        "updated_at": "2026-11-11T10:20:00Z",
        "links": {
            "assignee": {"href": _href("agents", "agent.wilhelmina@softorino-leak.test")} if assigned else {"href": None},
            "customer": {"href": _href("customers", "zorblat.quimby@example-leak.test")},
            "state": {"href": _href("states", "cust-id-5550123")},
            "messages": {"href": f"https://api.groovehq.com/v1/tickets/{number}/messages"},
        },
    }
    return ticket


def make_message(author_kind, note=False, at="2026-11-11T10:00:00Z"):
    who = "agent.wilhelmina@softorino-leak.test" if author_kind == "agents" else "zorblat.quimby@example-leak.test"
    other = "zorblat.quimby@example-leak.test" if author_kind == "agents" else "agent.wilhelmina@softorino-leak.test"
    return {
        "created_at": at,
        "updated_at": at,
        "note": note,
        "body": "<p>My card ending 4242 was charged twice</p>",
        "plain_text_body": "My card ending 4242 was charged twice",
        "sender_name": "Zorblat Quimby",
        "href": f"https://api.groovehq.com/v1/messages/cust-id-5550123",
        "links": {
            "attachments": {"href": "https://api.groovehq.com/v1/messages/x/attachments"},
            "author": {"href": _href(author_kind, who)},
            "recipient": {"href": _href("customers" if author_kind == "agents" else "agents", other)},
            "ticket": {"href": "https://api.groovehq.com/v1/tickets/1"},
        },
    }


def mock_dataset():
    tickets = [
        make_ticket(1001, ["SVAI", "tag_for_quimby_only"], title="[AI Chat] Billing dispute for Quimby"),
        make_ticket(1002, ["SVAI", "QCRM"]),
        make_ticket(1003, ["QCRM"], assigned=False),
        make_ticket(1004, [], assigned=False),
        make_ticket(1005, [{"name": "SVAI"}], closed_by="agent.wilhelmina@softorino-leak.test"),
        make_ticket(1006, [], title="Quimby login issue"),
    ]
    messages = {
        1001: [make_message("customers"), make_message("agents")],
        1002: [make_message("customers"), make_message("agents", note=True)],
        1003: [make_message("customers")],
        1004: [make_message("customers"), make_message("agents"), make_message("customers")],
        1005: [make_message("agents")],
        1006: [make_message("customers")],  # not sampled: only 5 tickets get messages
    }
    return tickets, messages


class FakeGroove:
    """Stands in for groove_get and records every request."""

    def __init__(self, tickets=None, messages=None, fail=None):
        default_tickets, default_messages = mock_dataset()
        self.tickets = default_tickets if tickets is None else tickets
        self.messages = default_messages if messages is None else messages
        self.fail = fail
        self.paths = []
        self.lock = threading.Lock()

    def __call__(self, path_and_query, token, timeout=None):
        with self.lock:
            self.paths.append(path_and_query)
        assert token == TOKEN
        if self.fail:
            raise self.fail
        if path_and_query.startswith("/v1/tickets?"):
            return {"tickets": self.tickets, "meta": {"pagination": {"current_page": 1}}}
        number = int(path_and_query.split("/")[3])
        return {"messages": self.messages.get(number, []), "meta": {"pagination": {"current_page": 1}}}


@contextlib.contextmanager
def patched(**attrs):
    originals = {name: getattr(probe, name) for name in attrs}
    for name, value in attrs.items():
        setattr(probe, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(probe, name, value)


def run(query=None, env=None, authorization=AUTH):
    """Call the endpoint core, capturing anything it prints."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status, payload = probe.handle(ENV if env is None else env, authorization, query or {})
    return status, payload, json.dumps(payload) + out.getvalue() + err.getvalue()


def leaked(text, needles):
    return [n for n in needles if n in text]


def check_no_personal_data(expect):
    fake = FakeGroove()
    with patched(groove_get=fake):
        status, payload, text = run()
    expect("overview returns 200", status == 200, str(status))
    found = leaked(text, PII)
    expect("overview output contains none of the planted names, emails, subjects, texts or ids",
           not found, f"leaked: {found}")

    fake = FakeGroove()
    with patched(groove_get=fake):
        status, payload, text = run({"customer_email": ["zorblat.quimby@example-leak.test"]})
    expect("customer mode returns 200", status == 200, str(status))
    found = leaked(text, PII)
    expect("customer mode output contains none of the planted values, nor the address",
           not found, f"leaked: {found}")
    first = payload["tickets"][0]["messages"][0]
    expect("customer mode keeps author kind and time",
           first["author"] in ("customer", "agent") and first["created_at"] == "2026-11-11T10:00:00Z", str(first))
    expect("customer mode drops everything else per message",
           set(first) == {"author", "note", "created_at"}, str(sorted(first)))

    # A tag that sits on one ticket could be a person's name; it is hidden.
    with patched(groove_get=FakeGroove()):
        _, payload, _ = run()
    tags = payload["enum_values"]["ticket_tags_on_2plus_tickets"]
    expect("a tag on a single ticket is not printed", "tag_for_quimby_only" not in tags, str(tags))
    expect("tags on 2+ tickets are printed with counts", tags.get("SVAI") == 3 and tags.get("QCRM") == 2, str(tags))


def check_numbers_and_answers(expect):
    with patched(groove_get=FakeGroove()):
        _, payload, _ = run()
    counts = payload["counts"]
    expect("counts tickets returned", counts["tickets_returned"] == 6, str(counts))
    expect("counts assigned vs unassigned", (counts["assigned"], counts["unassigned"]) == (4, 2), str(counts))
    expect("counts [AI Chat] subjects", counts["subject_starts_with_ai_chat"] == 1, str(counts))
    expect("counts SVAI / QCRM tags", counts["tagged"] == {"SVAI": 3, "QCRM": 2}, str(counts["tagged"]))
    expect("only 5 tickets get their messages fetched", counts["tickets_with_messages_fetched"] == 5, str(counts))
    expect("sampled tickets without an agent reply (note does not count)",
           counts["sampled_tickets_without_agent_reply"] == 2, str(counts))
    answers = payload["answers"]
    expect("author kind is derived from the author link",
           answers["agent_vs_customer_message"]["observed"] == {"agent": 4, "customer": 5},
           str(answers["agent_vs_customer_message"]))
    expect("note field values are reported", answers["note_vs_reply"]["observed"] == {"false": 8, "true": 1},
           str(answers["note_vs_reply"]))
    expect("direction pairs are reported",
           payload["enum_values"]["message_direction"] == {"agent->customer": 4, "customer->agent": 5},
           str(payload["enum_values"]["message_direction"]))
    expect("date format is a digit shape, not a date",
           payload["date_format"]["message"] == ["9999-99-99T99:99:99Z"], str(payload["date_format"]))
    expect("ticket and message field names come with types",
           payload["ticket_fields"].get("title") == "str" and payload["message_fields"].get("note") == "bool",
           "")
    expect("closed_by is reported as set / not set only",
           payload["presence"]["closed_by"] == {"set": 1, "empty_or_missing": 5}, str(payload["presence"]["closed_by"]))
    expect("ticket state values are reported", payload["enum_values"]["ticket"]["state"] == {"opened": 6}, "")


def check_token_never_printed(expect):
    cases = {
        "success": FakeGroove(),
        "401": FakeGroove(fail=probe.GrooveError("http", 401)),
        "429": FakeGroove(fail=probe.GrooveError("http", 429)),
        "timeout": FakeGroove(fail=probe.GrooveError("timeout")),
        "unexpected exception carrying the token": FakeGroove(fail=RuntimeError(f"boom {TOKEN} {CRON}")),
    }
    for label, fake in cases.items():
        with patched(groove_get=fake):
            _, _, text = run()
        expect(f"token and cron secret absent from output ({label})", TOKEN not in text and CRON not in text, text[:200])

    # Same through the real groove_get, with urllib errors that carry the token.
    def raising(error):
        def fake_urlopen(request, timeout=None):
            raise error
        return fake_urlopen

    for label, error in {
        "HTTPError whose url holds the token": HTTPError(f"https://x/?access_token={TOKEN}", 401, TOKEN, {}, None),
        "URLError mentioning the token": URLError(f"failed for {TOKEN}"),
    }.items():
        with patched(urlopen=raising(error)):
            status, payload, text = run()
        expect(f"token absent through the real request path ({label})",
               TOKEN not in text and status in (502, 504), f"{status} {text[:200]}")


def check_get_only(expect):
    tree = ast.parse(SOURCE.read_text())
    write_verbs = {"POST", "PUT", "PATCH", "DELETE"}
    verbs = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.value.upper() in write_verbs]
    expect("source contains no POST / PUT / PATCH / DELETE string", not verbs, str(verbs))

    methods, bodies = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "method":
                    methods.append(ast.literal_eval(kw.value) if isinstance(kw.value, ast.Constant) else "<dynamic>")
                if kw.arg in ("data", "json", "body"):
                    bodies.append(kw.arg)
    expect("every method= keyword in the source is GET", methods and all(m == "GET" for m in methods), str(methods))
    expect("no request body (data= / json= / body=) is ever passed", not bodies, str(bodies))
    expect("source never calls a write-style HTTP helper",
           not any(isinstance(n, ast.Attribute) and n.attr in ("post", "put", "patch", "delete", "request")
                   for n in ast.walk(tree)), "")

    # Behaviour: what actually reaches urlopen.
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        number_part = request.full_url.split("/v1/tickets")[1]
        tickets, messages = mock_dataset()
        body = ({"tickets": tickets} if number_part.startswith("?")
                else {"messages": messages[int(number_part.split("/")[1])]})
        return io.BytesIO(json.dumps(body).encode())

    with patched(urlopen=fake_urlopen):
        status, _, _ = run()
    expect("real request path works against the fake transport", status == 200, str(status))
    expect("every request to Groove is a GET with no body",
           seen and all(r.get_method() == "GET" and r.data is None for r in seen), str([r.get_method() for r in seen]))
    expect("every request goes to the documented base URL over https",
           all(r.full_url.startswith("https://api.groovehq.com/v1/") for r in seen), "")
    expect("token travels in the Authorization header, not in the URL",
           all(r.get_header("Authorization") == f"Bearer {TOKEN}" and TOKEN not in r.full_url for r in seen), "")
    expect("never more than 6 Groove requests per call", len(seen) <= 6, str(len(seen)))
    expect("list request is one page of at most 25 opened tickets",
           "per_page=25" in seen[0].full_url and "state=opened" in seen[0].full_url, seen[0].full_url)

    fake = FakeGroove(tickets=[make_ticket(n, []) for n in range(2000, 2040)],
                      messages={})
    with patched(groove_get=fake):
        run()
    expect("40 tickets in the mock still cost at most 6 requests", len(fake.paths) <= 6, str(len(fake.paths)))
    with patched(groove_get=(fake := FakeGroove())):
        run({"customer_email": ["a@b.test"]})
    expect("customer mode also stays within 6 requests", len(fake.paths) <= 6, str(len(fake.paths)))
    expect("customer mode filters the list by customer", "customer=a%40b.test" in fake.paths[0], fake.paths[0])


def check_errors(expect):
    status, payload, _ = run(env={"CRON_SECRET": CRON})
    expect("missing GROOVE_API_TOKEN gives 500", status == 500, str(status))
    expect("... with a short message and no details", set(payload) == {"error"}, str(payload))

    status, _, _ = run(env={"GROOVE_API_TOKEN": TOKEN})
    expect("missing CRON_SECRET is rejected (fail closed)", status == 401, str(status))
    status, _, _ = run(authorization="Bearer wrong")
    expect("wrong Authorization is rejected", status == 401, str(status))
    status, _, _ = run(authorization="")
    expect("no Authorization is rejected", status == 401, str(status))

    for label, error, want_status, want_error in [
        ("Groove 401", probe.GrooveError("http", 401), 502, "groove_rejected_token"),
        ("Groove 429", probe.GrooveError("http", 429), 502, "groove_rate_limited"),
        ("Groove 500", probe.GrooveError("http", 500), 502, "groove_http_error"),
        ("timeout", probe.GrooveError("timeout"), 504, "groove_timeout"),
        ("network failure", probe.GrooveError("network"), 502, "groove_network"),
    ]:
        with patched(groove_get=FakeGroove(fail=error)):
            status, payload, text = run()
        expect(f"{label}: clean {want_status} / {want_error}",
               (status, payload.get("error")) == (want_status, want_error), f"{status} {payload}")
        expect(f"{label}: no traceback in the output", "Traceback" not in text and "File \"" not in text, text[:200])

    with patched(groove_get=FakeGroove(fail=ZeroDivisionError("kaboom"))):
        status, payload, text = run()
    expect("unexpected exception: generic 500 without its text", status == 500 and "kaboom" not in text, text)

    # Real transport: HTTP errors and timeouts map to the same kinds.
    def raising(error):
        def fake(request, timeout=None):
            raise error
        return fake

    for label, error, want in [
        ("HTTPError 429", HTTPError("u", 429, "x", {}, None), "groove_rate_limited"),
        ("socket timeout", socket.timeout("timed out"), "groove_timeout"),
        ("URLError wrapping a timeout", URLError(socket.timeout("timed out")), "groove_timeout"),
    ]:
        with patched(urlopen=raising(error)):
            _, payload, _ = run()
        expect(f"real transport: {label} -> {want}", payload.get("error") == want, str(payload))

    status, payload, text = run({"customer_email": ["not an address <x>"]})
    expect("a malformed customer_email is a 400 and is not echoed", status == 400 and "not an address" not in text, text)

    # One ticket's messages failing must not sink the probe.
    class Flaky(FakeGroove):
        def __call__(self, path, token, timeout=None):
            if path.startswith("/v1/tickets/1002/"):
                raise probe.GrooveError("http", 500)
            return super().__call__(path, token, timeout)

    with patched(groove_get=Flaky()):
        status, payload, _ = run()
    expect("one failed message fetch is counted, not fatal",
           status == 200 and payload["counts"]["message_fetches_failed"] == 1, str(payload.get("counts")))

    with patched(groove_get=FakeGroove(tickets=[], messages={})):
        status, payload, _ = run()
    expect("an empty queue is a valid answer", status == 200 and payload["counts"]["tickets_returned"] == 0, str(status))


def check_http_handler(expect):
    server = HTTPServer(("127.0.0.1", 0), probe.handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    old = {k: os.environ.get(k) for k in ("CRON_SECRET", "GROOVE_API_TOKEN")}
    try:
        os.environ["CRON_SECRET"] = CRON
        os.environ.pop("GROOVE_API_TOKEN", None)

        def call(method, path, auth=None):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(method, path, headers={"Authorization": auth} if auth else {})
            response = conn.getresponse()
            body = response.read().decode()
            conn.close()
            return response.status, body

        expect("HTTP: no Authorization -> 401", call("GET", "/")[0] == 401, "")
        status, body = call("GET", "/", AUTH)
        expect("HTTP: no GROOVE_API_TOKEN -> 500 with a plain error", status == 500 and json.loads(body) == {"error": "GROOVE_API_TOKEN is not configured"}, body)
        status, body = call("POST", "/", AUTH)
        expect("HTTP: POST is accepted the same way (the workflow uses curl)", status == 500, str(status))
    finally:
        server.shutdown()
        server.server_close()
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main():
    failures = []
    checks = [0]

    def expect(label, condition, detail=""):
        checks[0] += 1
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        failures.append(label)

    groups = [
        ("No personal data in the output:", check_no_personal_data),
        ("Counts and answers:", check_numbers_and_answers),
        ("Token never printed:", check_token_never_printed),
        ("GET only, request budget:", check_get_only),
        ("Errors and auth:", check_errors),
        ("HTTP handler:", check_http_handler),
    ]
    for title, group in groups:
        print(f"\n{title}")
        group(expect)

    if failures:
        print(f"\n{len(failures)} of {checks[0]} checks failed.")
        return 1
    print(f"\nOK -- {checks[0]} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
