"""Read-only probe of the Groove REST API v1.

Reports the *shape* of what Groove returns -- field names, types, enum values,
counts -- so the bot's Groove logic can be written against real data instead of
guesses. It must never output anything that identifies a person: no names,
emails, subjects, message text or ids. The repo is public and so are the Actions
logs that show this function's response.

Standalone: stdlib only, not connected to process_email.py.

Sources (Groove REST API v1, no longer in active development):
  https://doc.groovehq.com/              base URL, auth
  https://doc.groovehq.com/tickets       GET /v1/tickets
  https://doc.groovehq.com/messages      GET /v1/tickets/:number/messages
"""

import hmac
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

GROOVE_BASE_URL = "https://api.groovehq.com"
TICKETS_PER_PAGE = 25
MESSAGE_TICKETS_SAMPLED = 5
# Vercel Hobby kills the function at 10s. The list call and the message calls
# (run in parallel) each get 4s, so the worst case is 8s plus overhead.
REQUEST_TIMEOUT_SECONDS = 4
# 1 list request + at most 5 message requests.
MAX_GROOVE_REQUESTS = 6

AI_CHAT_SUBJECT_PREFIX = "[ai chat]"
WATCHED_TAGS = ("SVAI", "QCRM")
# A tag name is only printed when it sits on at least this many tickets, so a
# tag that happens to be a person's name cannot single someone out.
TAG_MIN_TICKETS = 2

# Scalar fields whose values are printed. Everything else is reported by name
# and type only. An allowlist, because a denylist leaks the first field nobody
# thought of.
TICKET_ENUM_FIELDS = ("state", "priority", "type", "starred")
MESSAGE_ENUM_FIELDS = ("note", "type", "kind", "direction", "source", "channel", "state", "status")
# Fields whose value is a person or a free-form string: only "set / not set".
TICKET_PRESENCE_FIELDS = ("closed_by", "assigned_group", "resolution_time", "links.assignee.href")

SAFE_KEY = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
SAFE_ENUM_VALUE = re.compile(r"^[A-Za-z0-9_ -]{1,24}$")
SAFE_TAG = re.compile(r"^[A-Za-z0-9_ -]{1,30}$")
EMAIL_PARAM = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


class GrooveError(Exception):
    """Carries only a short kind and an HTTP status -- never the Groove body,
    the URL or the token, so nothing secret can reach a response or a log."""

    def __init__(self, kind, status=None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


def groove_get(path_and_query, token, timeout=REQUEST_TIMEOUT_SECONDS):
    """The only function that talks to Groove, and it only does GET."""
    request = Request(
        GROOVE_BASE_URL + path_and_query,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise GrooveError("http", error.code) from None
    except TimeoutError:
        raise GrooveError("timeout") from None
    except (ValueError, UnicodeDecodeError):
        raise GrooveError("bad_json") from None
    except Exception as error:
        # URLError wraps socket timeouts too.
        if "timed out" in str(getattr(error, "reason", error)):
            raise GrooveError("timeout") from None
        raise GrooveError("network") from None


# ---------- shape helpers ----------

def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "other"


def _safe_key(key):
    return key if isinstance(key, str) and SAFE_KEY.match(key) else "<odd_key>"


def _flatten(obj, prefix="", depth=0):
    """Yield (dotted_path, value) for scalars and lists, descending into dicts."""
    for key, value in obj.items():
        path = f"{prefix}{_safe_key(key)}"
        if isinstance(value, dict) and depth < 3:
            yield path, value
            yield from _flatten(value, path + ".", depth + 1)
        else:
            yield path, value


def field_types(items):
    types = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for path, value in _flatten(item):
            types.setdefault(path, set()).add(_type_name(value))
    return {path: "|".join(sorted(found)) for path, found in sorted(types.items())}


def _lookup(item, dotted):
    current = item
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def enum_values(items, fields):
    result = {}
    for field in fields:
        counter = Counter()
        for item in items:
            if not isinstance(item, dict) or field not in item:
                continue
            value = item[field]
            if value is None or isinstance(value, bool):
                counter[str(value).lower()] += 1
            elif isinstance(value, str) and SAFE_ENUM_VALUE.match(value):
                counter[value] += 1
            else:
                counter["<other>"] += 1
        if counter:
            result[field] = dict(sorted(counter.items()))
    return result


def presence(items, fields):
    result = {}
    for field in fields:
        seen = [(_lookup(i, field)) for i in items if isinstance(i, dict)]
        result[field] = {
            "set": sum(1 for v in seen if v not in (None, "", [], {})),
            "empty_or_missing": sum(1 for v in seen if v in (None, "", [], {})),
        }
    return result


def date_shapes(items, fields=("created_at", "updated_at")):
    shapes = Counter()
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in fields:
            value = item.get(field)
            if isinstance(value, str):
                shapes[re.sub(r"\d", "9", value)[:40]] += 1
    return [shape for shape, _ in shapes.most_common(3)]


def link_category(message_or_ticket, link):
    """Which kind of resource a links.<link>.href points at: the path segment
    ('agents' / 'customers' / ...) and nothing else. The rest of the path
    usually holds an email or an id, so it is never read out."""
    href = _lookup(message_or_ticket, f"links.{link}.href")
    if not isinstance(href, str):
        return "none"
    for segment in urlparse(href).path.split("/"):
        if segment in ("agents", "customers", "tickets", "messages"):
            return segment.rstrip("s")
    return "other"


def _tag_names(ticket):
    tags = ticket.get("tags")
    if not isinstance(tags, list):
        return []
    names = []
    for tag in tags:
        if isinstance(tag, str):
            names.append(tag)
        elif isinstance(tag, dict) and isinstance(tag.get("name"), str):
            names.append(tag["name"])
    return names


# ---------- the probe ----------

def _fetch_messages(ticket, token):
    number = ticket.get("number") if isinstance(ticket, dict) else None
    if isinstance(number, bool) or not isinstance(number, (int, str)) or not str(number).isdigit():
        raise GrooveError("no_ticket_number")
    payload = groove_get(f"/v1/tickets/{int(number)}/messages?per_page=50", token)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise GrooveError("bad_shape")
    return [m for m in messages if isinstance(m, dict)]


def _fetch_message_sets(tickets, token):
    """Messages for up to MESSAGE_TICKETS_SAMPLED tickets, in parallel. A failed
    ticket becomes None so one bad call does not sink the whole probe."""
    sample = tickets[:MESSAGE_TICKETS_SAMPLED]
    if not sample:
        return []

    def one(ticket):
        try:
            return _fetch_messages(ticket, token)
        except GrooveError:
            return None

    with ThreadPoolExecutor(max_workers=len(sample)) as pool:
        return list(pool.map(one, sample))


def _list_tickets(token, customer_email):
    if customer_email:
        query = urlencode({"customer": customer_email, "per_page": TICKETS_PER_PAGE})
    else:
        query = urlencode({"state": "opened", "per_page": TICKETS_PER_PAGE})
    payload = groove_get(f"/v1/tickets?{query}", token)
    tickets = payload.get("tickets") if isinstance(payload, dict) else None
    if not isinstance(tickets, list):
        raise GrooveError("bad_shape")
    return [t for t in tickets if isinstance(t, dict)]


def _author_kind(message):
    return link_category(message, "author")


def probe_customer(token, customer_email):
    """Author type and time of each message for one customer's tickets. The
    address itself is never echoed."""
    tickets = _list_tickets(token, customer_email)
    message_sets = _fetch_message_sets(tickets, token)
    out = []
    for messages in message_sets:
        if messages is None:
            out.append({"error": "messages_unavailable"})
            continue
        out.append({
            "messages": [
                {
                    "author": _author_kind(m),
                    "note": m.get("note") if isinstance(m.get("note"), bool) else None,
                    "created_at": m.get("created_at") if isinstance(m.get("created_at"), str)
                    and re.fullmatch(r"[0-9T:+.Z-]{10,40}", m["created_at"]) else None,
                }
                for m in messages
            ]
        })
    return {
        "mode": "customer",
        "tickets_found": len(tickets),
        "tickets_inspected": len(out),
        "tickets": out,
        "groove_requests": 1 + len(out),
    }


def probe_overview(token):
    tickets = _list_tickets(token, None)
    message_sets = _fetch_message_sets(tickets, token)
    sampled = [s for s in message_sets if s is not None]
    messages = [m for s in sampled for m in s]

    assigned = sum(1 for t in tickets if _lookup(t, "links.assignee.href"))
    tag_counts = Counter(name for t in tickets for name in set(_tag_names(t)))
    shown_tags = {
        name: count for name, count in sorted(tag_counts.items())
        if count >= TAG_MIN_TICKETS and SAFE_TAG.match(name)
    }

    author_kinds = Counter(_author_kind(m) for m in messages)
    directions = Counter(f"{_author_kind(m)}->{link_category(m, 'recipient')}" for m in messages)
    note_values = enum_values(messages, ("note",)).get("note", {})
    no_agent_reply = sum(
        1 for s in sampled
        if not any(_author_kind(m) == "agent" and m.get("note") is not True for m in s)
    )

    ticket_fields = field_types(tickets)
    message_fields = field_types(messages)
    state_values = enum_values(tickets, ("state",)).get("state")
    last_customer_like = [
        name for name in ticket_fields
        if re.search(r"(last|latest).*(customer|message|reply|response)|customer.*(_at|time)", name)
    ]

    return {
        "mode": "overview",
        "documented_base_url": GROOVE_BASE_URL,
        "ticket_fields": ticket_fields,
        "message_fields": message_fields,
        "enum_values": {
            "ticket": enum_values(tickets, TICKET_ENUM_FIELDS),
            "message": enum_values(messages, MESSAGE_ENUM_FIELDS),
            "message_author_kind": dict(sorted(author_kinds.items())),
            "message_direction": dict(sorted(directions.items())),
            "ticket_tags_on_2plus_tickets": shown_tags,
        },
        "presence": presence(tickets, TICKET_PRESENCE_FIELDS),
        "date_format": {
            "ticket": date_shapes(tickets),
            "message": date_shapes(messages),
        },
        "counts": {
            "tickets_returned": len(tickets),
            "assigned": assigned,
            "unassigned": len(tickets) - assigned,
            "subject_starts_with_ai_chat": sum(
                1 for t in tickets
                if isinstance(t.get("title"), str) and t["title"].strip().lower().startswith(AI_CHAT_SUBJECT_PREFIX)
            ),
            "tagged": {tag: tag_counts.get(tag, 0) for tag in WATCHED_TAGS},
            "tickets_with_messages_fetched": len(sampled),
            "message_fetches_failed": len(message_sets) - len(sampled),
            "sampled_tickets_without_agent_reply": no_agent_reply,
            "messages_seen": len(messages),
        },
        "answers": {
            "agent_vs_customer_message": {
                "no_author_type_field": not any(k in message_fields for k in ("author_type", "type", "kind")),
                "derive_from": "path segment of links.author.href (agents / customers)",
                "observed": dict(sorted(author_kinds.items())),
            },
            "note_vs_reply": {
                "field": "note",
                "present": "note" in message_fields,
                "observed": note_values,
            },
            "snoozed_and_closed": {
                "state_field_in_ticket": "state" in ticket_fields,
                "state_values_seen": state_values,
                "closed_by_set_count": presence(tickets, ("closed_by",))["closed_by"]["set"],
                "fields_mentioning_snooze": [n for n in ticket_fields if "snooz" in n],
                "documented_state_filter_values": ["unread", "opened", "pending", "closed", "spam"],
                "note": "this probe lists opened tickets only; closed/snoozed look is documented, not observed",
            },
            "last_customer_message_time_field": {
                "ticket_fields_that_look_like_it": last_customer_like,
                "derivable_from_messages": "created_at of the newest message whose author kind is customer",
            },
        },
        "groove_requests": 1 + len(message_sets),
    }


# ---------- request handling ----------

def handle(env, authorization, query):
    """Pure core of the endpoint: (status, payload). No printing, and no
    exception text ever goes into the payload."""
    secret = env.get("CRON_SECRET")
    # Fail closed: unlike the bot endpoint, this one returns data about a
    # helpdesk, so an unset secret must not mean "open".
    if not secret or not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        return 401, {"error": "Unauthorized"}

    token = env.get("GROOVE_API_TOKEN")
    if not token:
        return 500, {"error": "GROOVE_API_TOKEN is not configured"}

    customer_email = (query.get("customer_email") or [""])[0].strip()
    if customer_email and not EMAIL_PARAM.match(customer_email):
        return 400, {"error": "customer_email is not a valid address"}

    try:
        if customer_email:
            return 200, probe_customer(token, customer_email)
        return 200, probe_overview(token)
    except GrooveError as error:
        if error.kind == "timeout":
            return 504, {"error": "groove_timeout"}
        if error.kind == "http" and error.status in (401, 403):
            return 502, {"error": "groove_rejected_token", "groove_status": error.status}
        if error.kind == "http" and error.status == 429:
            return 502, {"error": "groove_rate_limited", "groove_status": 429}
        if error.kind == "http":
            return 502, {"error": "groove_http_error", "groove_status": error.status}
        return 502, {"error": f"groove_{error.kind}"}
    except Exception:
        return 500, {"error": "probe_failed"}


class handler(BaseHTTPRequestHandler):
    """GET or POST with Authorization: Bearer $CRON_SECRET. Optional
    ?customer_email=. Reads Groove with GET requests only."""

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        query = parse_qs(urlparse(self.path).query)
        status, payload = handle(os.environ, self.headers.get("Authorization", ""), query)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # The default logs the request line, which would carry customer_email.
        pass
