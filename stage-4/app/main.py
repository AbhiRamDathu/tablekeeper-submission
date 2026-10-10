"""HTTP surface for Tablekeeper Stage 1.

`ThreadingHTTPServer`, not the single-threaded default: the spec requires 50 concurrent in-flight
requests with no 5xx, and a server that handles one at a time fails that before any application
logic is even reached. Every request opens its own SQLite connection in WAL mode, so readers do not
queue behind the writer.

Routing is a table of (method, compiled path) pairs rather than a framework. The whole surface is
small enough that a dependency would cost more than it saves, and the image ships with no pip
install at all, which is what makes "no outbound network at run time" trivially true.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

if __package__ in (None, ""):  # `python -m app.main` from the stage-1 directory
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "app"

from . import auth, store  # noqa: E402
from .intervals import format_minutes, overlaps, slot_end  # noqa: E402
from .tz import (  # noqa: E402
    InvalidLocalTime,
    NonExistentLocalTime,
    format_instant,
    minutes_of,
    parse_local,
    resolve,
)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# `[0-9]`, not `\d`: in Python's `re` `\d` matches every Unicode decimal digit, so `^\d+$` also
# accepts `٤` and `４`, and `int()` then parses them happily. `REQUIREMENTS.md:59` says an integer
# query parameter is *plain decimal digits*, and a fullwidth four is not one. `date` is defended
# twice over because `date.fromisoformat` rejects what the regex lets through; `party_size` had no
# second line of defence.
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_HHMM_RE = re.compile(r"^[0-9]{2}:[0-9]{2}$")
_POSITIVE_INT_RE = re.compile(r"^[0-9]+$")
_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: §11:186 bounds a batch at 1-8 move objects. Zero is out of range too, which is why an empty
#: `moves` array is a validation error rather than a successful no-op.
_MAX_MOVES = 8

#: §Seating changes: planning must support up to six tables, four declared pairs and six
#: considered bookings; anything larger may be refused with 422 `planning_limit`.
_MAX_PLAN_TABLES = 6
_MAX_PLAN_PAIRS = 4
_MAX_PLAN_BOOKINGS = 6

log = logging.getLogger(__name__)

# The only text a client ever sees for an unhandled exception, and it interpolates nothing. A
# `sqlite3` message names tables and columns ("UNIQUE constraint failed: tables.id"), so sending
# `str(exc)` to the wire publishes the schema to whoever happened to trigger the bug -- and the
# catch-all is reachable with no token at all, since an unauthenticated `POST /_test/reset` is
# enough. The detail goes to the server log instead; the correlation id is what lets an operator
# join the two.
GENERIC_500_MESSAGE = "an internal error occurred"

# Every body this service writes is UTF-8, and the spec asks for that to be visible rather than
# assumed: bare `application/json` used to go out on both response paths, which is legal HTTP but is
# not the `application/json; charset=utf-8` the runtime contract names. One constant, because
# `_respond` and the `send_error` override are the only two places that write the header and a
# header that is correct on one path and not the other is the kind of split nothing else catches.
JSON_CONTENT_TYPE = "application/json; charset=utf-8"

# `BaseHTTPRequestHandler.send_error` speaks its own dialect: an HTML page. The spec says every 4xx
# and 5xx body is `{"error":{"code":...,"message":...}}`, and the errors it raises are exactly the
# ones a client can provoke at will -- an unimplemented verb, a malformed request line, a URI past
# the line limit. Left alone, the only errors a caller can trigger deliberately are the only ones
# they cannot parse. Codes the framework can actually raise are 400/414/431/501/505; 401/403/404 are
# here because `send_error` is public and an application path should not silently lose the spec's
# vocabulary by calling it.
_FRAMEWORK_ERROR_CODES = {
    400: "malformed_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    414: "uri_too_long",
    431: "too_many_headers",
    501: "not_implemented",
    505: "http_version_not_supported",
}


def _framework_error_code(status: int) -> str:
    """The `error.code` for a framework-raised status, in the spec's vocabulary where it has one."""
    return _FRAMEWORK_ERROR_CODES.get(status, "request_failed")


def _new_correlation_id() -> str:
    """An opaque id for one 500, echoed to the client and written to the log beside the cause."""
    return secrets.token_hex(8)


#: The one sentence a client gets when the exception reaching it carries text this module did not
#: author. Keyed by status, so the fallback is still specific about what went wrong -- a caller can
#: retry a 503 and must not retry a 422 -- while interpolating nothing at all.
GENERIC_ERROR_MESSAGES = {
    400: "the request could not be understood",
    401: "the supplied credentials were not accepted",
    404: "no such resource",
    409: "that resource already exists",
    422: "the request failed validation",
    500: GENERIC_500_MESSAGE,
}

#: The exception types whose message text this service wrote on purpose, and is therefore safe to
#: forward. This is an **allowlist**, not a denylist of bad types, and the direction is the whole
#: point: a new exception type is withheld from clients until someone decides its text is
#: publishable, so adding a catch clause somewhere cannot silently start leaking internals.
#:
#: Each of these is raised with a literal or with a path we built (`auth.py` and `store.py` construct
#: every one of them from their own strings), which is the property that earns the entry. What is
#: deliberately absent: anything out of `sqlite3`, and anything out of the standard library.
_AUTHORED_MESSAGES = (
    auth.MalformedRequest,
    auth.ValidationFailure,
    auth.CredentialsTaken,
    auth.BadCredentials,
    store.InvalidFixture,
    store.InvalidState,
    InvalidLocalTime,
    NonExistentLocalTime,
)


def client_message(exc: str | BaseException, status: int) -> str:
    """The text a client may see for `exc`, which is not always `str(exc)`.

    Takes a literal as readily as an exception, because most call sites pass a string this module
    wrote -- a string is forwarded unchanged, and only an *exception* has to earn the right to have
    its text published. That keeps the whole surface in one place without making every fixed message
    an f-string.

    Eight sites raise a caught exception's own message to the wire. Each is currently safe, because
    every one of those exceptions is constructed from a string this service wrote -- `auth.py:100`
    raises `CredentialsTaken(email)` with the *address*, not with the `sqlite3.IntegrityError` that
    provoked it, so a failed signup answers `409` with the caller's own address and nothing else.
    Verified by forcing a real `IntegrityError` inside the signup transaction: the body was
    `{"code": "email_taken", "message": "x@y.co"}`, with no `UNIQUE`, no `users.email`, no traceback.

    So the disclosure these sites could cause is a *latent* one, not a live one, and that is exactly
    why it is worth closing centrally. The dangerous shape is not any single line; it is
    `except SomeError as exc: raise _invalid(str(exc))` appearing a ninth time after someone adds a
    `sqlite3` failure to a new path. Patching each site freezes today's safe arrangement and says
    nothing about tomorrow's.

    Hence the allowlist above. Authored text is forwarded, because a client that cannot be told
    *which* field it got wrong cannot fix it -- and `REQUIREMENTS.md:169` wants the failing path in
    the 422 for exactly this reason, so flattening all of these to one generic string would trade a
    real requirement for a fix to nothing. Anything not on the list gets a fixed sentence chosen by
    status, which discloses nothing and cannot leak a table name.
    """
    if isinstance(exc, str):
        return exc
    if isinstance(exc, _AUTHORED_MESSAGES):
        return str(exc)
    log.warning("withholding unrecognised %s text from a %s client response",
                type(exc).__name__, status)
    return GENERIC_ERROR_MESSAGES.get(status, GENERIC_500_MESSAGE)


class HttpError(Exception):
    """An error with the status and `error.code` the spec assigns it."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _malformed(message: str | BaseException) -> HttpError:
    return HttpError(400, "malformed_request", client_message(message, 400))


def _invalid(message: str | BaseException) -> HttpError:
    return HttpError(422, "validation_failed", client_message(message, 422))


# ---- handlers -------------------------------------------------------------

def health(request, match):
    return 200, {"status": "ok"}


def reset(request, match):
    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("fixture must be a JSON object")
    try:
        store.reset_database(body)
    except store.InvalidFixture as exc:
        # `store` raises this with the fixture path and the first failing rule already spelled out,
        # and `REQUIREMENTS.md:169` wants exactly that sentence as a 422. Letting it reach the
        # catch-all in `_dispatch` turned every invalid fixture into a 500, which `REQUIREMENTS.md:17`
        # forbids outright. The validation runs before the transaction opens, so nothing was written.
        raise _invalid(exc) from exc
    return 204, None


def export_snapshot(request, match):
    """§10:162-167 -- 200 with the whole service state as one importable object.

    **Unauthenticated, like reset.** §10:160 says so in as many words, and this handler is
    registered in `ROUTES` beside `/_test/reset` with no `require_user()` call -- which is the whole
    of the authentication story on this route. §10:163 warns that an export "may contain
    credentials and session tokens", so this is a private test artifact and nothing else; the
    envelope's `track` is what tells the holder that.
    """
    return 200, {"track": store.EXPORT_TRACK,
                 "format_version": store.EXPORT_FORMAT_VERSION,
                 "state": store.export_state()}


def import_snapshot(request, match):
    """§10:167-170 -- 204, having replaced everything with what the export carried.

    **The envelope is judged here and the state is judged in `store`.** §10:169-170 splits the
    sentence: "Invalid JSON follows §5; missing fields, wrong track/version or an invalid state give
    422 `validation_failed` without changing the destination." Three of those four are about three
    fields this handler owns, and the fourth is about a value whose shape only the storage layer
    knows. Nothing here writes, and `import_state` validates before it opens its transaction, so
    every one of these refusals is a refusal and not a rollback.

    **Unparseable JSON is 400, not 422.** §5:47 sends a body that does not parse to
    `malformed_request`, and §10:169 says exactly that for this endpoint rather than leaving it to
    §5's table; a body that is not an object is the same case, because §5:47 also puts "a field of
    the wrong JSON type" there.

    `format_version` is compared with `!=` *and* a `bool` guard, because `True == 1` in Python: a
    request carrying `"format_version": true` would otherwise be accepted as version 1.
    """
    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("the export object must be a JSON object")
    for field in ("track", "format_version", "state"):
        if field not in body:
            raise _invalid(f"{field} is required")
    if body["track"] != store.EXPORT_TRACK:
        raise _invalid(f"track must be {store.EXPORT_TRACK!r}")
    if body["format_version"] != store.EXPORT_FORMAT_VERSION \
            or isinstance(body["format_version"], bool):
        raise _invalid(f"format_version must be {store.EXPORT_FORMAT_VERSION}")
    try:
        store.import_state(body["state"])
    except store.InvalidState as exc:
        # Same shape as `reset`: `store` writes the sentence with the JSON path of the offending
        # field, and REQUIREMENTS.md:169 wants the failing path in the 422.
        raise _invalid(exc) from exc
    return 204, None


def post_signup(request, match):
    user_id = "u_" + secrets.token_hex(6)
    try:
        created = auth.signup(request.json_body(), user_id)
    except auth.MalformedRequest as exc:
        raise _malformed(exc) from exc
    except auth.ValidationFailure as exc:
        raise _invalid(exc) from exc
    except auth.CredentialsTaken as exc:
        raise HttpError(409, "email_taken", client_message(exc, 409)) from exc
    return 201, created


def post_login(request, match):
    try:
        return 200, auth.authenticate(request.json_body())
    except auth.MalformedRequest as exc:
        raise _malformed(exc) from exc
    except auth.BadCredentials as exc:
        raise HttpError(401, "unauthenticated", client_message(exc, 401)) from exc


def list_restaurants(request, match):
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, timezone, slot_minutes, reservation_duration_minutes,"
            " cancellation_cutoff_minutes FROM restaurants ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return 200, {"restaurants": [
        {"id": row["id"], "name": row["name"], "timezone": row["timezone"]} for row in rows
    ]}


def get_restaurant(request, match):
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT id, name, timezone, slot_minutes, reservation_duration_minutes,"
            " cancellation_cutoff_minutes, combinable FROM restaurants WHERE id = ?",
            (match.group("id"),),
        ).fetchone()
        if row is None:
            raise HttpError(404, "not_found", "no such restaurant")
        tables = conn.execute(
            "SELECT id, label, capacity FROM tables WHERE restaurant_id = ? ORDER BY ordinal, id",
            (row["id"],),
        ).fetchall()
        hours = conn.execute(
            "SELECT weekday, opens, closes FROM opening_hours WHERE restaurant_id = ?"
            " ORDER BY ordinal, weekday, opens, closes", (row["id"],),
        ).fetchall()
    finally:
        conn.close()
    return 200, _restaurant_body(row, [dict(t) for t in tables], [dict(h) for h in hours])


def _restaurant_body(row, tables, hours):
    body = {
        "id": row["id"],
        "name": row["name"],
        "timezone": row["timezone"],
        "slot_minutes": row["slot_minutes"],
        "reservation_duration_minutes": row["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": row["cancellation_cutoff_minutes"],
        "combinable": _combinable_pairs(row),
    }
    if tables is not None:
        body["tables"] = tables
    if hours is not None:
        body["opening_hours"] = hours
    return body


def _json_list(value) -> list:
    """A JSON array stored in a TEXT column, or `[]` if it is absent or unusable."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _reservation_table_ids(row) -> list[str]:
    """The tables a reservation holds: its stored set, or its single `table_id`."""
    parsed = _json_list(row["table_ids"] if "table_ids" in row.keys() else None)
    if parsed:
        return [str(member) for member in parsed]
    return [row["table_id"]]


def _combinable_pairs(restaurant) -> list[list[str]]:
    """The restaurant's declared combinable pairs, each a two-member list, in fixture order."""
    raw = restaurant["combinable"] if "combinable" in restaurant.keys() else None
    pairs = []
    for pair in _json_list(raw):
        if isinstance(pair, list) and len(pair) == 2:
            pairs.append([str(pair[0]), str(pair[1])])
    return pairs


def _policy_from_row(row) -> dict:
    return {
        "policy_version": row["policy_version"],
        "slot_minutes": row["slot_minutes"],
        "reservation_duration_minutes": row["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": row["cancellation_cutoff_minutes"],
        "opening_hours": json.loads(row["opening_hours"]),
        "capacities": json.loads(row["capacities"]),
        "effective_from": row["effective_from"],
    }


def _selected_policy(conn, restaurant_id, day_iso) -> dict:
    """§Policies: the policy for a booking's local start date, or policy 0 when none applies.

    Greatest `effective_from` not later than the date, ties broken by greatest `policy_version`; a
    date before every published policy (or a restaurant with none) gets the fixture's own rules.
    """
    row = conn.execute(
        "SELECT * FROM policies WHERE restaurant_id = ? AND effective_from <= ?"
        " ORDER BY effective_from DESC, policy_version DESC LIMIT 1",
        (restaurant_id, day_iso)).fetchone()
    if row is None:
        return store.policy_zero(conn, restaurant_id)
    return _policy_from_row(row)


def _accepted_terms(policy: dict) -> dict:
    """The snapshot §accepted-terms requires: the whole selected policy less `effective_from`."""
    return {key: value for key, value in policy.items() if key != "effective_from"}


def _context(conn, restaurant, day_iso) -> dict:
    """A restaurant's effective rules for one local date: the selected policy plus its tables.

    Table capacity comes from the policy, not from the fixture `tables` row, because §Policies
    requires capacity to be the *selected policy's* capacity. Order still comes from the fixture,
    which is the order `available_table_ids` and the explanations both use.
    """
    policy = _selected_policy(conn, restaurant["id"], day_iso)
    capacities = policy["capacities"]
    tables = []
    for table in conn.execute(
            "SELECT id, label, capacity FROM tables WHERE restaurant_id = ? ORDER BY ordinal, id",
            (restaurant["id"],)).fetchall():
        tables.append({"id": table["id"], "label": table["label"],
                       "capacity": capacities.get(table["id"], table["capacity"])})
    return {
        "id": restaurant["id"],
        "timezone": restaurant["timezone"],
        "combinable": restaurant["combinable"],
        "policy": policy,
        "slot_minutes": policy["slot_minutes"],
        "reservation_duration_minutes": policy["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": policy["cancellation_cutoff_minutes"],
        "opening_hours": policy["opening_hours"],
        "tables": tables,
        "capacities": capacities,
    }


def _day_of(starts_at_local: str) -> str:
    return starts_at_local[:10]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _row_accepted_terms(conn, row) -> dict:
    """The terms a reservation accepted, or policy 0 for a pre-stage-3 row import left unset."""
    raw = row["accepted_terms"] if "accepted_terms" in row.keys() else None
    if raw:
        try:
            terms = json.loads(raw)
        except ValueError:
            terms = None
        if isinstance(terms, dict):
            return terms
    return store.policy_zero(conn, row["restaurant_id"])


def _accepted_cutoff_minutes(conn, row) -> int:
    return int(_row_accepted_terms(conn, row)["cancellation_cutoff_minutes"])


def _write_history(conn, reference, event, changes, revision, terms, plan_id=None) -> None:
    """Append one history entry, numbered one past the reservation's current last.

    `plan_id` is set only by a seating repair, which §Seating changes requires the entry to
    carry; every other event leaves it null.
    """
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM reservation_history WHERE reference = ?",
        (reference,)).fetchone()[0]
    conn.execute(
        "INSERT INTO reservation_history (reference, seq, at, event, changes, revision,"
        " accepted_terms, plan_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (reference, seq, _now_iso(), event, json.dumps(changes), revision,
         json.dumps(terms) if terms is not None else None, plan_id))


def _bump_restaurant_revision(conn, restaurant_id) -> None:
    conn.execute("UPDATE restaurants SET restaurant_revision = restaurant_revision + 1"
                 " WHERE id = ?", (restaurant_id,))


def _bump_series_revision(conn, series_id) -> None:
    conn.execute("UPDATE series SET revision = revision + 1 WHERE series_id = ?", (series_id,))


def _canonical_table_set(ctx, table_ids) -> list[str]:
    """Reorder a two-member declared combination to the order the restaurant declared it."""
    ids = list(table_ids)
    if len(ids) == 2:
        wanted = set(ids)
        for pair in _combinable_pairs(ctx):
            if set(pair) == wanted:
                return list(pair)
    return ids


def _table_taken(table_id, occupancy, starts, ends) -> bool:
    return any(table_id in held and overlaps(starts, ends, other_start, other_end)
               for held, other_start, other_end in occupancy)


def _available_options(restaurant, tables, occupancy, starts, ends, party_size):
    """Every single table then every declared pair that can seat the party at this slot (§ stage 2).

    Singles first, in fixture order, then the combinable pairs in the order the restaurant declared
    them. Membership and capacity are both required: a pair whose total seating is short of the
    party, or one of whose members is already held across this interval, is not an option.
    """
    options = []
    for table in tables:
        if table["capacity"] >= party_size \
                and not _table_taken(table["id"], occupancy, starts, ends):
            options.append({"table_ids": [table["id"]], "capacity": table["capacity"]})
    by_id = {table["id"]: table for table in tables}
    for pair in _combinable_pairs(restaurant):
        members = [by_id.get(table_id) for table_id in pair]
        if any(member is None for member in members):
            continue
        capacity = sum(member["capacity"] for member in members)
        if capacity < party_size:
            continue
        if any(_table_taken(table_id, occupancy, starts, ends) for table_id in pair):
            continue
        options.append({"table_ids": list(pair), "capacity": capacity})
    return options


def get_availability(request, match):
    params = request.query
    for required in ("restaurant_id", "date", "party_size"):
        if required not in params:
            raise _invalid(f"{required} is required")

    if not _DATE_RE.match(params["date"]):
        raise _invalid("date must be YYYY-MM-DD")
    try:
        day = dt.date.fromisoformat(params["date"])
    except ValueError as exc:
        raise _invalid("date is not a real calendar date") from exc

    if not _POSITIVE_INT_RE.match(params["party_size"]):
        raise _invalid("party_size must be a positive integer")
    party_size = int(params["party_size"])
    if party_size < 1:
        raise _invalid("party_size must be a positive integer")

    # `explain` is optional, and `true` is its only accepted value: `false`, `1` and the empty
    # string are all 422., and its absence keeps stage 1's shape with no explanation fields.
    explain = False
    if "explain" in params:
        if params["explain"] != "true":
            raise _invalid("explain's only accepted value is true")
        explain = True

    conn = store.connect()
    try:
        restaurant = conn.execute(
            "SELECT * FROM restaurants WHERE id = ?", (params["restaurant_id"],)
        ).fetchone()
        if restaurant is None:
            raise HttpError(404, "not_found", "no such restaurant")
        ctx = _context(conn, restaurant, params["date"])
        booked = conn.execute(
            "SELECT table_id, table_ids, starts_at_utc, ends_at_utc FROM reservations"
            " WHERE restaurant_id = ? AND status != 'cancelled'", (restaurant["id"],),
        ).fetchall()
        closures = _closure_rows(conn, restaurant["id"])
    finally:
        conn.close()

    hours = [w for w in ctx["opening_hours"] if w["weekday"] == WEEKDAYS[day.weekday()]]
    if not hours:
        # A weekday with no opening_hours entry is closed: an empty list, not a 404.
        return 200, {"restaurant_id": restaurant["id"], "date": params["date"],
                     "timezone": restaurant["timezone"], "party_size": party_size, "slots": []}

    zone = ZoneInfo(ctx["timezone"])
    duration = ctx["reservation_duration_minutes"]
    step = ctx["slot_minutes"]
    version = ctx["policy"]["policy_version"]
    occupancy = []
    for row in booked:
        other_start = dt.datetime.fromisoformat(row["starts_at_utc"])
        other_end = (dt.datetime.fromisoformat(row["ends_at_utc"]) if row["ends_at_utc"]
                     else slot_end(other_start, duration))
        occupancy.append((_reservation_table_ids(row), other_start, other_end))
    # §Seating changes: an applied closure occupies its table for its half-open interval, so the
    # grid drops it and `no_overlap` reports false there exactly as for a conflicting booking.
    for closure in closures:
        occupancy.append(([closure["table_id"]],
                          dt.datetime.fromisoformat(closure["from_utc"]),
                          dt.datetime.fromisoformat(closure["to_utc"])))

    slots = []
    for cursor in _slot_starts(hours, step, duration):
        label = format_minutes(cursor)
        naive = parse_local(f"{day.isoformat()}T{label}")
        try:
            starts = resolve(naive, zone)
        except NonExistentLocalTime:
            # The wall time is skipped by a clock change; there is no such slot to offer.
            continue
        ends = slot_end(starts, duration)

        free = []
        explanations = []
        for table in ctx["tables"]:
            holds_capacity = table["capacity"] >= party_size
            holds_no_overlap = not any(
                table["id"] in other_tables and overlaps(starts, ends, other_start, other_end)
                for other_tables, other_start, other_end in occupancy
            )
            available = holds_capacity and holds_no_overlap
            if available:
                free.append(table["id"])
            if explain:
                explanations.append({
                    "table_id": table["id"], "policy_version": version, "available": available,
                    "rules": [{"rule": "capacity", "holds": holds_capacity},
                              {"rule": "no_overlap", "holds": holds_no_overlap}],
                })

        # A slot with no free table still appears, carrying an empty list: the restaurant is
        # open and the grid exists, there is simply nothing left to seat at that moment.
        slot = {
            "starts_at_local": f"{day.isoformat()}T{label}",
            "starts_at": format_instant(starts),
            "available_table_ids": free,
            "available_options": _available_options(ctx, ctx["tables"], occupancy, starts, ends,
                                                     party_size),
        }
        if explain:
            slot["explain"] = explanations
        slots.append(slot)

    return 200, {"restaurant_id": restaurant["id"], "date": params["date"],
                 "timezone": restaurant["timezone"], "party_size": party_size, "slots": slots}


def list_reservations(request, match):
    request.require_user()
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE user_id = ? ORDER BY starts_at_utc DESC, reference",
            (request.user["id"],),
        ).fetchall()
    finally:
        conn.close()
    return 200, {"reservations": [_reservation_body(row) for row in rows]}


def get_reservation(request, match):
    request.require_user()
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM reservations WHERE reference = ? AND user_id = ?",
            (match.group("reference"), request.user["id"]),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HttpError(404, "not_found", "no such reservation")
    return 200, _reservation_body(row)


def post_reservation(request, match):
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key is required")
    if not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scope = request.idempotency_scope()

    # §7:243-246: "After the body has been parsed as a JSON object and the caller authenticated,
    # idempotency is resolved before endpoint-specific field validation or current-resource checks.
    # Thus a used key with a different JSON body returns 409 idempotency_key_reuse even when that
    # new body would otherwise be invalid." The field-type checks therefore run AFTER the receipt is
    # looked up -- a spent key answers 409 before a malformed body check can fire -- but inside the
    # same transaction, so a first-use body that fails them still ROLLBACKs and leaves the key
    # unspent (§7:254: a key reused after a 4xx failed write is treated as a first use).
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute(
            "SELECT request_hash, response_body FROM idempotency"
            " WHERE key = ? AND user_id = ? AND scope = ?",
            (key, user["id"], scope),
        ).fetchone()
        if stored is not None:
            if stored["request_hash"] != request_hash:
                raise HttpError(409, "idempotency_key_reuse",
                                "this key was used with a different request body")
            # Replay returns the original response verbatim, and 200 rather than the original 201:
            # the operation is not being performed a second time.
            replay = json.loads(stored["response_body"])
            conn.execute("COMMIT")
            return 200, replay

        for field in ("restaurant_id", "starts_at_local"):
            if field not in body:
                raise _invalid(f"{field} is required")
            if not isinstance(body[field], str):
                raise _malformed(f"{field} must be a string")
        if "table_id" in body and not isinstance(body["table_id"], str):
            raise _malformed("table_id must be a string")
        if "table_ids" in body and (not isinstance(body["table_ids"], list)
                                    or not all(isinstance(m, str) for m in body["table_ids"])):
            raise _invalid("table_ids must be an array of table ids")
        if "table_id" not in body and "table_ids" not in body:
            raise _invalid("table_id is required")
        if "party_size" not in body:
            raise _invalid("party_size is required")
        if not isinstance(body["party_size"], int) or isinstance(body["party_size"], bool):
            # §5:172-174: invalid `party_size` values -- including strings and booleans -- are
            # 422 validation_failed, the specific field rule beating §5:48's generic wrong-type 400.
            raise _invalid("party_size must be an integer")

        created = _create_reservation(conn, body, user["id"])
        conn.execute(
            "INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
            " response_body) VALUES (?, ?, ?, ?, ?, ?)",
            (key, user["id"], scope, request_hash, created[0], json.dumps(created[1])),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return created


def _enforce_cutoff(row, cutoff_minutes, what):
    """409 `cutoff_passed` once `now` is inside the window before the CURRENT start.

    Shared by amendment (:140) and cancellation (:138), which state the same rule. Measured against
    the stored start and never the requested one: re-anchoring to the new start would let a caller
    inside the window walk the booking forward to a later slot and keep amending it, which is the
    thing the cutoff exists to prevent. §4:43 keeps a past start from being an error by itself --
    the window is the only test, and a booking already inside it simply cannot be touched.

    Stage 3 passes the reservation's **accepted** cutoff (§Policies: "Cancel checks the accepted
    cutoff", and an amendment checks "the old accepted cutoff first"), which is the policy the
    diner agreed to rather than whatever a later publication happens to say today.
    """
    cutoff_at = (dt.datetime.fromisoformat(row["starts_at_utc"])
                 - dt.timedelta(minutes=cutoff_minutes))
    if dt.datetime.now(dt.timezone.utc) >= cutoff_at:
        raise HttpError(409, "cutoff_passed", f"too close to the start to {what}")


def _slot_starts(hours, step, duration):
    """The wall-clock minute-of-day of every slot one day's windows offer as a booking start.

    **The single copy of the grid arithmetic** (§4:101, §8:117-118): a slot is
    `opens + k * slot_minutes` for every step such that `slot + duration <= closes`, window by
    window in fixture order. `get_availability` lists these to build its answers and
    `_assert_within_opening_slot` validates a single requested start against them, so the two
    booking questions cannot drift apart the day `slot_minutes` stops being 30.
    """
    starts = []
    for window in hours:
        cursor = minutes_of(window["opens"])
        closes = minutes_of(window["closes"])
        while cursor + duration <= closes:
            starts.append(cursor)
            cursor += step
    return starts


def _assert_within_opening_slot(ctx, starts):
    """§8:126-127 -- the start is an offered slot, or this day's two specific 422 codes.

    Runs on every booking surface -- create and PATCH through `_resolve_booking`, and each move in
    `POST /reservation-moves` directly -- after the local time has resolved against the
    restaurant's zone, so `starts` is the correct instant before any grid or hour question is
    asked. Everything here is a wall-clock question: `weekday`, `%H:%M` and the `opening_hours`
    strings are all minutes of the restaurant's local day, and the booking's wall-clock span is
    the absolute duration added to local minutes (§9).

    The two codes stay apart the way §8:349-350 write them: `not_on_slot_grid` is only asked of a
    start that sits *inside* the serving window but not on a slot boundary, while any slot that is
    not inside the window -- before `opens`, or one that would end after `closes` -- is
    `outside_opening_hours`. So on 18:00-23:00 with a 90-minute booking, 19:07 is `not_on_slot_grid`,
    and 17:30 (before opening) and 22:45 (ending at 00:15) are both `outside_opening_hours`.
    A weekday without an `opening_hours` row is closed, and booking into it is hours, not grid.

    Stage 3 reads hours, grid and duration from the **selected policy** rather than from the
    restaurant's original fixture configuration.
    """
    weekday = WEEKDAYS[starts.weekday()]
    hours = [w for w in ctx["opening_hours"] if w["weekday"] == weekday]
    if not hours:
        raise HttpError(422, "outside_opening_hours",
                        f"{ctx['id']} has no opening hours on {weekday}")

    start_minutes = minutes_of(starts.strftime("%H:%M"))
    step = ctx["slot_minutes"]
    duration = ctx["reservation_duration_minutes"]
    if start_minutes in set(_slot_starts(hours, step, duration)):
        return

    for window in hours:
        opens = minutes_of(window["opens"])
        closes = minutes_of(window["closes"])
        if start_minutes < opens or start_minutes + duration > closes:
            continue
        raise HttpError(
            422, "not_on_slot_grid",
            f"{starts.strftime('%H:%M')} is not on the {step}-minute slot grid")
    raise HttpError(422, "outside_opening_hours",
                    f"{starts.strftime('%H:%M')} is outside this restaurant's opening hours")


def _requested_table_ids(body):
    """The table set a create/amend body asks for, from `table_ids` or the single `table_id`.

    Exactly one of the two spellings is allowed, which is what §8 means by "a table set of one
    member" once combinations exist: `table_id` is a set of one and `table_ids` names the set
    directly. Carrying both is ambiguous rather than merely redundant, so it is a 422.
    """
    table_id = body.get("table_id")
    table_ids = body.get("table_ids")
    if table_ids is not None and table_id is not None:
        raise _invalid("provide either table_id or table_ids, not both")
    if table_ids is not None:
        if not isinstance(table_ids, list) or not all(isinstance(m, str) for m in table_ids):
            raise _invalid("table_ids must be an array of table ids")
        return list(table_ids)
    if table_id is not None:
        return [table_id]
    raise _invalid("table_id is required")


def _resolve_table_set(ctx, table_ids):
    """The table rows a requested set names, after the membership and combination rules.

    Unknown table is 404, a repeat is 422 `validation_failed`, more than two is 422
    `combination_not_allowed`, and two tables the restaurant does not declare combinable is 422
    `combination_not_allowed`. Membership and the declared pair are both required; combining is not
    transitive, so `[t_1, t_3]` is refused even when `[t_1, t_2]` and `[t_2, t_3]` are declared.

    Capacity is the **selected policy's** capacity (§Combined-table history), taken from the
    context's table rows, which `_context` already overlaid with the policy's capacities.
    """
    if not table_ids:
        raise _invalid("at least one table is required")
    if len(table_ids) > 2:
        raise HttpError(422, "combination_not_allowed", "at most two tables may be combined")
    if len(set(table_ids)) != len(table_ids):
        raise _invalid("table_ids must be distinct")
    by_id = {table["id"]: table for table in ctx["tables"]}
    tables = []
    for table_id in table_ids:
        row = by_id.get(table_id)
        if row is None:
            raise HttpError(404, "not_found", "no such table at this restaurant")
        tables.append(row)
    if len(tables) == 2:
        wanted = {table["id"] for table in tables}
        declared = next((pair for pair in _combinable_pairs(ctx) if set(pair) == wanted), None)
        if declared is None:
            raise HttpError(422, "combination_not_allowed",
                            "these two tables are not a declared combination")
        # Table-set order is the declared combination order, so a reversed input pair names the
        # same set rather than a different one (§Combined-table history).
        order = {table_id: index for index, table_id in enumerate(declared)}
        tables.sort(key=lambda table: order[table["id"]])
    return tables


def _validate_booking_fields(ctx, tables, starts_at_local, party_size):
    """Every check on a requested booking except occupancy. Returns `starts`.

    Split out from `_assert_tables_free` because §11:197 requires a batch's *non-occupancy* errors to
    be settled before any occupancy error, which a single combined pass cannot express.
    """
    if party_size < 1:
        raise _invalid("party_size must be at least 1")
    capacity = sum(table["capacity"] for table in tables)
    if capacity < party_size:
        raise HttpError(422, "party_exceeds_capacity",
                        "party_size exceeds the table's capacity")

    try:
        naive = parse_local(starts_at_local)
    except InvalidLocalTime as exc:
        raise _invalid(exc) from exc
    zone = ZoneInfo(ctx["timezone"])
    try:
        starts = resolve(naive, zone)
    except NonExistentLocalTime as exc:
        raise HttpError(422, "invalid_local_time", str(exc)) from exc
    _assert_within_opening_slot(ctx, starts)
    return starts


def _assert_tables_free(conn, ctx, tables, starts, exclude_reference=None):
    """409 `table_unavailable` if any table in the set collides with a live booking.

    `exclude_reference` drops the reservation being amended from the check, which would otherwise
    collide with itself.

    The query is scoped to `restaurant_id` because `table_id` is only unique *within* a restaurant.
    Keying `tables` by `(restaurant_id, id)` is what makes two restaurants able to own a `t_2`
    each, and this is the query that has to follow from that: without the scope, a booking at one
    restaurant occupies an identically-numbered table at every other restaurant. A held set is
    compared as a set, so a combination collides if *any* member is taken.

    Stage 4 adds applied closures to the same test: a closed table rejects a create, an amendment
    and a move during `[from,to)` with the same 409 `table_unavailable`, exactly as a conflicting
    booking does.
    """
    ends = slot_end(starts, ctx["reservation_duration_minutes"])
    wanted = {table["id"] for table in tables}
    clash = ("SELECT table_id, table_ids, starts_at_utc, ends_at_utc FROM reservations"
             " WHERE restaurant_id = ? AND status != 'cancelled'")
    params = [ctx["id"]]
    if exclude_reference is not None:
        clash += " AND reference != ?"
        params.append(exclude_reference)
    for row in conn.execute(clash, params).fetchall():
        if not (set(_reservation_table_ids(row)) & wanted):
            continue
        other_start = dt.datetime.fromisoformat(row["starts_at_utc"])
        other_end = (dt.datetime.fromisoformat(row["ends_at_utc"]) if row["ends_at_utc"]
                     else slot_end(other_start, ctx["reservation_duration_minutes"]))
        if overlaps(starts, ends, other_start, other_end):
            raise HttpError(409, "table_unavailable", "that table is already booked")
    _assert_no_closure(conn, ctx["id"], wanted, starts, ends)


def _closure_rows(conn, restaurant_id):
    """Every applied closure at a restaurant, oldest first."""
    return conn.execute(
        "SELECT table_id, from_utc, to_utc FROM closures WHERE restaurant_id = ?"
        " ORDER BY created_at, table_id", (restaurant_id,)).fetchall()


def _assert_no_closure(conn, restaurant_id, wanted, starts, ends):
    """409 `table_unavailable` if a closed table overlaps `[starts, ends)`."""
    for closure in _closure_rows(conn, restaurant_id):
        if closure["table_id"] not in wanted:
            continue
        if overlaps(starts, ends,
                    dt.datetime.fromisoformat(closure["from_utc"]),
                    dt.datetime.fromisoformat(closure["to_utc"])):
            raise HttpError(409, "table_unavailable", "that table is closed")


def _resolve_booking(conn, ctx, body, starts_at_local, party_size,
                     exclude_reference=None):
    """Validate a booking exactly as create does and return `(tables, starts)`.

    Shared with `PATCH /reservations/{reference}`, which the spec requires to apply "same
    validation as create" -- so it calls this rather than keeping a second copy that has to be
    kept in agreement.
    """
    tables = _resolve_table_set(ctx, _requested_table_ids(body))
    starts = _validate_booking_fields(ctx, tables, starts_at_local, party_size)
    _assert_tables_free(conn, ctx, tables, starts, exclude_reference)
    return tables, starts


def _create_reservation(conn, body, user_id):
    """Insert the reservation on an already-open transaction. Does not commit.

    The conflict check lives inside the caller's transaction on purpose: §11 requires that the
    decision to book and the booking itself cannot be separated by a concurrent writer.
    """
    restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                              (body["restaurant_id"],)).fetchone()
    if restaurant is None:
        raise HttpError(404, "not_found", "no such restaurant")
    party_size = body["party_size"]
    ctx = _context(conn, restaurant, _day_of(body["starts_at_local"]))
    tables, starts = _resolve_booking(conn, ctx, body, body["starts_at_local"], party_size)

    reference = _new_reference()
    created_at = _now_iso()
    table_ids = [table["id"] for table in tables]
    terms = _accepted_terms(ctx["policy"])
    ends = slot_end(starts, ctx["reservation_duration_minutes"])
    conn.execute(
        "INSERT INTO reservations (reference, restaurant_id, table_id, table_ids, user_id,"
        " starts_at_utc, starts_at_local, ends_at_utc, party_size, status, created_at,"
        " revision, accepted_terms, series_id, series_index, exception)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, 1, ?, NULL, NULL, 0)",
        (reference, restaurant["id"], table_ids[0], json.dumps(table_ids), user_id,
         starts.astimezone(dt.timezone.utc).isoformat(),
         body["starts_at_local"], ends.astimezone(dt.timezone.utc).isoformat(),
         party_size, created_at, json.dumps(terms)),
    )
    _write_history(conn, reference, "created", [
        {"field": "table_id" if len(table_ids) == 1 else "table_ids", "from": None,
         "to": table_ids[0] if len(table_ids) == 1 else table_ids},
        {"field": "starts_at_local", "from": None, "to": body["starts_at_local"]},
        {"field": "party_size", "from": None, "to": party_size},
    ], 1, terms)
    _bump_restaurant_revision(conn, restaurant["id"])
    stored = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                          (reference,)).fetchone()
    return 201, _reservation_body(stored, restaurant)


def _new_reference() -> str:
    return "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(6))


def patch_reservation(request, match):
    """Amend a reservation in place. `any subset` of the three mutable fields, no key required.

    §7 makes the idempotency key mandatory on create and moves only, so one sent here is neither
    required nor recorded -- and per §7 a key already used on another path is not a replay, so a
    PATCH carrying a create's key must still succeed normally. Validating rather than ignoring a
    present key is the one thing done about it: §5 states the 1..255 length rule without scoping
    it to the endpoints that demand the header.
    """
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is not None and not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")
    if "table_id" in body and not isinstance(body["table_id"], str):
        raise _malformed("table_id must be a string")
    if "table_ids" in body and (not isinstance(body["table_ids"], list)
                                or not all(isinstance(m, str) for m in body["table_ids"])):
        raise _invalid("table_ids must be an array of table ids")
    if "table_id" in body and "table_ids" in body:
        raise _invalid("provide either table_id or table_ids, not both")
    if "starts_at_local" in body and not isinstance(body["starts_at_local"], str):
        raise _malformed("starts_at_local must be a string")
    if "party_size" in body and (not isinstance(body["party_size"], int)
                                 or isinstance(body["party_size"], bool)):
        # §5:172-174: `party_size` strings and booleans are 422 validation_failed; the other two
        # amendment fields keep §5:48's generic wrong-type 400.
        raise _invalid("party_size must be an integer")
    if "expected_revision" in body:
        expected = body["expected_revision"]
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise _invalid("expected_revision must be a positive integer")

    reference = match.group("reference")
    conn = store.connect()
    try:
        # One transaction for the read, the checks and the write: §8 requires a failed amendment to
        # leave the original booking AND its occupancy untouched, which is only true if the failure
        # and the rewrite share a boundary.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                           (reference,)).fetchone()
        # Not the caller's is 404, not 403: §5 folds "not visible to this caller" into not_found,
        # so a 403 here would confirm the reference exists.
        if row is None or row["user_id"] != user["id"]:
            raise HttpError(404, "not_found", "no such reservation")
        if row["status"] == "cancelled":
            raise HttpError(409, "reservation_cancelled", "this reservation is cancelled")
        # §Policies: a mismatched expected_revision is 409 stale_revision *before* cutoff/validation.
        if "expected_revision" in body and body["expected_revision"] != row["revision"]:
            raise HttpError(409, "stale_revision", "the reservation changed since you read it")

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (row["restaurant_id"],)).fetchone()
        _enforce_cutoff(row, _accepted_cutoff_minutes(conn, row), "amend")

        # Absent fields keep their stored value, which is what makes the subset literal. A missing
        # table field keeps the reservation's current set, which may be a combination.
        if "table_ids" in body:
            requested = list(body["table_ids"])
        elif "table_id" in body:
            requested = [body["table_id"]]
        else:
            requested = _reservation_table_ids(row)
        starts_at_local = body.get("starts_at_local", row["starts_at_local"])
        party_size = body.get("party_size", row["party_size"])

        ctx = _context(conn, restaurant, _day_of(starts_at_local))
        stored_tables = _reservation_table_ids(row)
        table_changed = _canonical_table_set(ctx, requested) != stored_tables
        starts_changed = starts_at_local != row["starts_at_local"]
        party_changed = party_size != row["party_size"]

        # §Reservation history: a PATCH that changes nothing succeeds but records no entry and keeps
        # the accepted terms, end time and revision. It still requires a confirmed, editable booking,
        # which the cutoff check above already enforced.
        if not (table_changed or starts_changed or party_changed):
            conn.execute("COMMIT")
            return 200, _reservation_body(row, restaurant)

        # A real amendment validates all resulting fields against the resulting date's policy, then
        # atomically replaces terms and end time and increments the revision once.
        tables, starts = _resolve_booking(conn, ctx, {"table_ids": requested},
                                          starts_at_local, party_size,
                                          exclude_reference=reference)
        table_ids = [table["id"] for table in tables]
        terms = _accepted_terms(ctx["policy"])
        revision = row["revision"] + 1
        ends = slot_end(starts, ctx["reservation_duration_minutes"])

        # `reference` and `status` are deliberately absent from the SET list: the spec requires both
        # to survive an amendment.
        conn.execute(
            "UPDATE reservations SET table_id = ?, table_ids = ?, starts_at_local = ?,"
            " starts_at_utc = ?, ends_at_utc = ?, party_size = ?, revision = ?,"
            " accepted_terms = ?, exception = ? WHERE reference = ?",
            (table_ids[0], json.dumps(table_ids), starts_at_local,
             starts.astimezone(dt.timezone.utc).isoformat(),
             ends.astimezone(dt.timezone.utc).isoformat(), party_size, revision,
             json.dumps(terms), 1 if row["series_id"] else row["exception"], reference),
        )
        changes = []
        if table_changed:
            if len(stored_tables) == 1 and len(table_ids) == 1:
                changes.append({"field": "table_id", "from": stored_tables[0], "to": table_ids[0]})
            else:
                changes.append({"field": "table_ids", "from": stored_tables, "to": table_ids})
        if starts_changed:
            changes.append({"field": "starts_at_local", "from": row["starts_at_local"],
                            "to": starts_at_local})
        if party_changed:
            changes.append({"field": "party_size", "from": row["party_size"], "to": party_size})
        _write_history(conn, reference, "changed", changes, revision, terms)
        if row["series_id"]:
            _bump_series_revision(conn, row["series_id"])
        _bump_restaurant_revision(conn, restaurant["id"])
        updated = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                               (reference,)).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return 200, _reservation_body(updated, restaurant)


def cancel_reservation(request, match):
    """Cancel a booking in place: 200 with its current state, table freed immediately.

    `POST /reservations/{reference}/cancel` is the one endpoint whose success is defined by what it
    stops rather than what it returns -- :136 asks for "200 with current state" and then for the
    table to be free "immediately so the next GET /availability offers the slot again". Flipping
    `status` is sufficient for both, because the availability query (:226) and the booking conflict
    check both already exclude cancelled rows.

    §7 requires the idempotency key on create and moves only, so none is needed here and none is
    recorded; the endpoint's own retry story is :137, "already cancelled is 200 not an error".
    """
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is not None and not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    reference = match.group("reference")
    conn = store.connect()
    try:
        # Read, check and write in one transaction, so a refusal cannot half-apply and the slot
        # cannot be freed by a cancellation that then failed.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                           (reference,)).fetchone()
        # Not the caller's is 404, not 403: §5 folds "not visible to this caller" into not_found,
        # so a 403 would confirm the reference exists.
        if row is None or row["user_id"] != user["id"]:
            raise HttpError(404, "not_found", "no such reservation")

        # Already cancelled is 200, not an error -- and deliberately tested BEFORE the cutoff. A
        # booking cancelled inside the window is inside it forever after, so checking the cutoff
        # first would turn a successful cancellation into a permanent 409 on retry, which is the
        # opposite of what :137 asks for.
        if row["status"] == "cancelled":
            conn.execute("COMMIT")
            return 200, _reservation_body(row)

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (row["restaurant_id"],)).fetchone()
        _enforce_cutoff(row, _accepted_cutoff_minutes(conn, row), "cancel")

        # Only `status` moves. §10:175 keeps identities and timestamps from being regenerated, so
        # `reference`, `reservation_id` and `created_at` all survive a cancellation untouched.
        # §Policies: cancel increments the revision once and keeps the accepted terms; a series
        # occurrence's cancellation bumps its series revision once but is not a diner exception.
        revision = row["revision"] + 1
        terms = _row_accepted_terms(conn, row)
        conn.execute("UPDATE reservations SET status = 'cancelled', revision = ?"
                     " WHERE reference = ?", (revision, reference))
        _write_history(conn, reference, "cancelled", [], revision, terms)
        if row["series_id"]:
            _bump_series_revision(conn, row["series_id"])
        _bump_restaurant_revision(conn, restaurant["id"])
        updated = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                               (reference,)).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return 200, _reservation_body(updated)


def post_reservation_moves(request, match):
    """`POST /reservation-moves` -- §11, the batch amendment endpoint.

    Two passes inside a single transaction, and the ordering between them is the point of :197:

    1. every item's non-occupancy checks, walked in input order -- 404, cancelled, the shared
       restaurant, the cutoff, then the ordinary amendment field rules;
    2. then occupancy, also in input order, but judged against the batch's *resulting* state:
       each destination is compiled and checked against the destinations already fixed and against
       the bookings that are not moving. That is what makes :199's "overlap among resulting
       bookings or with an unlisted booking" fall out without a second copy of the conflict
       logic -- and it is what lets a conflict-free swap succeed, which a live-table check would
       wrongly refuse while the swap's partner still sits in its own old slot.

    Nothing here commits until every item is through both passes, so no caller can observe a
    half-applied batch and a failure leaves reservations and occupancy untouched, as :201 requires.

    What pass one does *not* do is read ahead. Every check an item needs is applied to that item
    when the walk reaches it, so :197's "in input order" is observable: item 1's 404 outranks item
    7's malformed field, and the caller is always told about the batch's earliest non-occupancy
    error. A validation sweep over the whole array first would answer differently depending on how
    many items the caller happened to send -- item 7's type error pre-empting item 1's missing
    booking -- and §11 nowhere asks for that.
    """
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key is required")
    if not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")

    # §7:81-83 resolves the key after the body parses as an object and after authentication, but
    # *before* endpoint-specific field validation and current-resource checks. So the receipt is
    # looked up before the shape of `moves` is judged at all -- which is why a replay of a batch
    # that has since become invalid still answers 200, and why a spent key with a different body
    # answers 409 even when that body is nonsense.
    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scope = request.idempotency_scope()

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute(
            "SELECT request_hash, response_body FROM idempotency"
            " WHERE key = ? AND user_id = ? AND scope = ?",
            (key, user["id"], scope),
        ).fetchone()
        if stored is not None:
            if stored["request_hash"] != request_hash:
                raise HttpError(409, "idempotency_key_reuse",
                                "this key was used with a different request body")
            replay = json.loads(stored["response_body"])
            conn.execute("COMMIT")
            return 200, replay

        moves = _validated_moves(body)

        # Pass one: non-occupancy only, in input order.
        restaurant = None
        planned = []
        for item in moves:
            reference = item["reference"]
            row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                               (reference,)).fetchone()
            # Not the caller's is 404, not 403: §5 folds "not visible to this caller" into
            # not_found, so a 403 would confirm the reference exists.
            if row is None or row["user_id"] != user["id"]:
                raise HttpError(404, "not_found", "no such reservation")
            if row["status"] == "cancelled":
                raise HttpError(409, "reservation_cancelled", "this reservation is cancelled")
            if restaurant is None:
                restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                          (row["restaurant_id"],)).fetchone()
            elif row["restaurant_id"] != restaurant["id"]:
                raise _invalid("every booking in a batch must be at the same restaurant")
            # §Collective moves: each move carries PATCH's optional per-move expected_revision.
            if "expected_revision" in item:
                expected = item["expected_revision"]
                if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
                    raise _invalid("expected_revision must be a positive integer")
                if expected != row["revision"]:
                    raise HttpError(409, "stale_revision",
                                    "the reservation changed since you read it")
            _enforce_cutoff(row, _accepted_cutoff_minutes(conn, row), "amend")

            # Absent fields keep their stored value, exactly as an amendment's subset does (:192).
            starts_at_local = item.get("starts_at_local", row["starts_at_local"])
            party_size = item.get("party_size", row["party_size"])
            if "table_id" in item and not isinstance(item["table_id"], str):
                raise _malformed("table_id must be a string")
            if "table_ids" in item and (not isinstance(item["table_ids"], list)
                                        or not all(isinstance(m, str)
                                                   for m in item["table_ids"])):
                raise _invalid("table_ids must be an array of table ids")
            if "table_id" in item and "table_ids" in item:
                raise _invalid("provide either table_id or table_ids, not both")
            if not isinstance(starts_at_local, str):
                raise _malformed("starts_at_local must be a string")
            if not isinstance(party_size, int) or isinstance(party_size, bool):
                raise _invalid("party_size must be an integer")

            if "table_ids" in item:
                requested = list(item["table_ids"])
            elif "table_id" in item:
                requested = [item["table_id"]]
            else:
                requested = _reservation_table_ids(row)
            ctx = _context(conn, restaurant, _day_of(starts_at_local))
            stored_tables = _reservation_table_ids(row)
            real = (_canonical_table_set(ctx, requested) != stored_tables
                    or starts_at_local != row["starts_at_local"]
                    or party_size != row["party_size"])
            if real:
                # A real change adopts the resulting date's policy and validates every field.
                tables = _resolve_table_set(ctx, requested)
                starts = _validate_booking_fields(ctx, tables, starts_at_local, party_size)
                terms = _accepted_terms(ctx["policy"])
                ends = slot_end(starts, ctx["reservation_duration_minutes"])
            else:
                # A no-op retains its terms, history and revision but still holds its slot.
                by_id = {table["id"]: table for table in ctx["tables"]}
                tables = [by_id[t] for t in stored_tables if t in by_id]
                starts = dt.datetime.fromisoformat(row["starts_at_utc"])
                terms = None
                ends = (dt.datetime.fromisoformat(row["ends_at_utc"]) if row["ends_at_utc"]
                        else slot_end(starts, ctx["reservation_duration_minutes"]))
            planned.append((reference, row, tables, starts, starts_at_local, party_size,
                            real, terms, ends))

        # Pass two: occupancy against the batch's *resulting* state, not the pre-batch one.
        # Checking each item against the live table would refuse a conflict-free swap: the first
        # mover collides with its partner, which still sits in the slot it is about to vacate.
        # So every destination is compiled first, then each item is checked against the
        # destinations already fixed by earlier items -- :199's "overlap among resulting bookings",
        # which is also how an item that stays put keeps holding (:200, by colliding with anyone
        # who tries to take its slot) -- and against every booking that is not moving (:199 "or
        # with an unlisted booking"). The checks run in input order, so :197's ordering is
        # preserved for occupancy conflicts as well as non-occupancy errors.
        listed = [reference for reference, _row, _tables, _starts, _s, _p, _r, _t, _e in planned]
        in_clause = ", ".join("?" for _ in listed)
        compiled = []
        results = []
        touched_series = set()
        any_real = False
        for (reference, row, tables, starts, starts_at_local, party_size,
             real, terms, ends) in planned:
            wanted = {table["id"] for table in tables}
            for other_tables, other_starts, other_ends in compiled:
                if wanted & {table["id"] for table in other_tables} \
                        and overlaps(starts, ends, other_starts, other_ends):
                    raise HttpError(409, "table_unavailable", "that table is already booked")
            clash = ("SELECT table_id, table_ids, starts_at_utc, ends_at_utc FROM reservations"
                     " WHERE restaurant_id = ? AND status != 'cancelled'"
                     f" AND reference NOT IN ({in_clause})")
            for other in conn.execute(clash, (restaurant["id"], *listed)).fetchall():
                if not (set(_reservation_table_ids(other)) & wanted):
                    continue
                other_start = dt.datetime.fromisoformat(other["starts_at_utc"])
                other_end = (dt.datetime.fromisoformat(other["ends_at_utc"])
                             if other["ends_at_utc"]
                             else slot_end(other_start,
                                           restaurant["reservation_duration_minutes"]))
                if overlaps(starts, ends, other_start, other_end):
                    raise HttpError(409, "table_unavailable", "that table is already booked")
            if real:
                table_ids = [table["id"] for table in tables]
                stored_tables = _reservation_table_ids(row)
                revision = row["revision"] + 1
                # `reference`, `status`, `user_id` and `created_at` are deliberately absent from
                # the SET list: §11:194 keeps identity, owner and creation time unchanged.
                conn.execute(
                    "UPDATE reservations SET table_id = ?, table_ids = ?, starts_at_local = ?,"
                    " starts_at_utc = ?, ends_at_utc = ?, party_size = ?, revision = ?,"
                    " accepted_terms = ?, exception = ? WHERE reference = ?",
                    (table_ids[0], json.dumps(table_ids), starts_at_local,
                     starts.astimezone(dt.timezone.utc).isoformat(),
                     ends.astimezone(dt.timezone.utc).isoformat(), party_size, revision,
                     json.dumps(terms), 1 if row["series_id"] else row["exception"], reference),
                )
                changes = []
                if stored_tables != table_ids:
                    if len(stored_tables) == 1 and len(table_ids) == 1:
                        changes.append({"field": "table_id", "from": stored_tables[0],
                                        "to": table_ids[0]})
                    else:
                        changes.append({"field": "table_ids", "from": stored_tables,
                                        "to": table_ids})
                if starts_at_local != row["starts_at_local"]:
                    changes.append({"field": "starts_at_local", "from": row["starts_at_local"],
                                    "to": starts_at_local})
                if party_size != row["party_size"]:
                    changes.append({"field": "party_size", "from": row["party_size"],
                                    "to": party_size})
                _write_history(conn, reference, "changed", changes, revision, terms)
                if row["series_id"]:
                    touched_series.add(row["series_id"])
                any_real = True
            moved = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                                 (reference,)).fetchone()
            results.append(_reservation_body(moved, restaurant))
            compiled.append((tables, starts, ends))

        # §Collective moves: each affected series revision increases once, and each changed series
        # occurrence becomes a permanent diner exception; the restaurant revision increases once.
        for series_id in touched_series:
            _bump_series_revision(conn, series_id)
        if any_real:
            _bump_restaurant_revision(conn, restaurant["id"])

        created = (201, {"reservations": results})
        conn.execute(
            "INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
            " response_body) VALUES (?, ?, ?, ?, ?, ?)",
            (key, user["id"], scope, request_hash, created[0], json.dumps(created[1])),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return created


def _validated_moves(body):
    """§11:185-186 -- the shape of the batch itself, before any resource is touched.

    Four rules, all of them 422 `validation_failed` because all four are the shape §11:186 names: the
    array, its length, each entry being an object, and "distinct string references". A `reference`
    of the wrong JSON type is 422 because §11:186 says *string references* in as many words.

    Batch shape only, and the boundary is §11:197's. A move object's *field* types are not the
    batch's shape -- they are that item's ordinary amendment rules, and :197 settles a batch by
    reporting its earliest non-occupancy error in input order. Judging item 7's `table_id` here
    would answer with item 7's error while item 1's booking is missing, which is the opposite of
    "in input order" and would make the answer depend on the batch's length. Those checks therefore
    run inside the walk, in `post_reservation_moves`.

    Two things this function deliberately does not decide:

    * Whether a body that is not an object, or a `moves` that is not an array, is 400 or 422. §5:48
      makes a wrong JSON type 400; §11:186 makes an invalid shape 422; a non-object body is
      answered 400 before this is reached, and a non-array `moves` is answered 422 because it is
      §11:186's own "1-8 objects" rule about an `moves` that is not a list of objects. That split is
      pinned by `BatchShape`, not inferred here.
    * `party_size`, whose wrong-typed case is 422 by §5:57 in contradiction to §5:48. The check
      travels with the other field types in the walk, keeping the 422 §5:57 asks for.

    Unknown fields in an item are ignored rather than rejected, per §3:28.
    """
    moves = body.get("moves")
    if not isinstance(moves, list):
        raise _invalid("moves must be an array")
    # 0 is out of range as much as 9 is: §11:186 says "1-8 objects", so an empty batch is invalid
    # rather than a successful no-op.
    if not 1 <= len(moves) <= _MAX_MOVES:
        raise _invalid(f"moves must hold between 1 and {_MAX_MOVES} objects")
    seen = set()
    for item in moves:
        if not isinstance(item, dict):
            raise _invalid("each move must be a JSON object")
        reference = item.get("reference")
        if not isinstance(reference, str):
            raise _invalid("each move needs a string reference")
        if reference in seen:
            raise _invalid("move references must be distinct")
        seen.add(reference)
    return moves


def _reservation_body(row, restaurant=None):
    # Callers inside a write transaction hand over the restaurant row they already hold; list and
    # get arrive without one and take the extra lookup. `ends_at` (§8:330-341) is the booking's
    # absolute end rendered in the restaurant's zone, exactly the value the guest reads off the
    # grid: slot_end applies the duration in absolute time so a fall-back night that starts at
    # 01:30 still ends 90 real minutes later (§9), not a wall-clock 90 later.
    if restaurant is None:
        conn = store.connect()
        try:
            restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                      (row["restaurant_id"],)).fetchone()
        finally:
            conn.close()
    starts = dt.datetime.fromisoformat(row["starts_at_utc"])
    zone = ZoneInfo(restaurant["timezone"])
    if row["ends_at_utc"]:
        ends = dt.datetime.fromisoformat(row["ends_at_utc"]).astimezone(zone)
    else:
        ends = slot_end(starts, restaurant["reservation_duration_minutes"]).astimezone(zone)
    created = dt.datetime.fromisoformat(row["created_at"])
    table_ids = _reservation_table_ids(row)
    terms_raw = row["accepted_terms"] if "accepted_terms" in row.keys() else None
    terms = json.loads(terms_raw) if terms_raw else None
    revision = row["revision"] if "revision" in row.keys() else 1
    body = {
        "reference": row["reference"],
        "reservation_id": row["reference"],
        "restaurant_id": row["restaurant_id"],
        "table_ids": table_ids,
        "user_id": row["user_id"],
        "starts_at_local": row["starts_at_local"],
        # §8's worked examples render both starts_at and ends_at in the restaurant's zone -- a
        # local 19:00 in September's Berlin is "19:00:00+02:00" (§8:297, §8:338-339), the wall time
        # with its offset, not the same instant read as 17:00:00+00:00 at UTC. UTC is reserved for
        # created_at, whose example carries +00:00 (§8:340). ends_at is already zone-rendered;
        # starts_at must match so both fields of the same booking read in one zone.
        "starts_at": format_instant(starts.astimezone(zone)),
        "ends_at": format_instant(ends),
        "party_size": row["party_size"],
        "status": row["status"],
        "created_at": format_instant(created),
        "revision": revision,
        "accepted_terms": terms,
    }
    # §8: "When the set has one member the response still carries `table_id`"; a two-member set
    # omits it, because `table_ids` is then the whole statement of what was booked.
    if len(table_ids) == 1:
        body["table_id"] = table_ids[0]
    return body


# ---- stage 3: history and decision -----------------------------------------------------------


def get_history(request, match):
    """`GET /reservations/{reference}/history` -- the owner's own record, oldest first.

    §Reservation history: only the owner may read it, and "anyone else, signed in or not, gets the
    same 404 `not_found`". That is why the owner is resolved optionally and a missing token and a
    mismatched owner are answered identically here, rather than by `require_user`'s 401.
    """
    user = request.optional_user()
    reference = match.group("reference")
    conn = store.connect()
    try:
        owner = conn.execute("SELECT user_id FROM reservations WHERE reference = ?",
                             (reference,)).fetchone()
        if owner is None or user is None or owner["user_id"] != user["id"]:
            raise HttpError(404, "not_found", "no such reservation")
        rows = conn.execute(
            "SELECT seq, at, event, changes, revision, accepted_terms, plan_id"
            " FROM reservation_history WHERE reference = ? ORDER BY seq", (reference,)).fetchall()
    finally:
        conn.close()
    entries = []
    for row in rows:
        raw = row["accepted_terms"]
        entry = {
            "seq": row["seq"],
            "at": row["at"],
            "event": row["event"],
            "changes": json.loads(row["changes"]),
            "revision": row["revision"],
            "accepted_terms": json.loads(raw) if raw else None,
        }
        # §Seating changes: a repair's entry carries its `plan_id`; every other event leaves it off.
        if row["plan_id"] is not None:
            entry["plan_id"] = row["plan_id"]
        entries.append(entry)
    return 200, {"reference": reference, "entries": entries}


def get_decision(request, match):
    """`GET /reservations/{reference}/decision` -- current revision and accepted terms.

    Same owner-only 404 rule as history, including "even without authentication", and it still
    answers for a cancelled booking.
    """
    user = request.optional_user()
    reference = match.group("reference")
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                           (reference,)).fetchone()
        if row is None or user is None or row["user_id"] != user["id"]:
            raise HttpError(404, "not_found", "no such reservation")
        terms = _row_accepted_terms(conn, row)
        revision = row["revision"]
    finally:
        conn.close()
    return 200, {"reference": reference, "revision": revision, "accepted_terms": terms}


# ---- stage 3: policies -----------------------------------------------------------------------


def _policy_body(row) -> dict:
    return {
        "effective_from": row["effective_from"],
        "slot_minutes": row["slot_minutes"],
        "reservation_duration_minutes": row["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": row["cancellation_cutoff_minutes"],
        "opening_hours": json.loads(row["opening_hours"]),
        "capacities": json.loads(row["capacities"]),
        "policy_version": row["policy_version"],
    }


def _valid_hhmm(value) -> bool:
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        return False
    hour, minute = value.split(":")
    return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59


def _validate_policy(body, table_ids) -> dict:
    """A complete policy, or 422 `validation_failed`.

    §Policies: all six fields are required; grid and duration are integers 1..1440; cutoff is an
    integer 0..10080; booleans are not integers; opening hours follow stage 1 with no duplicate
    weekdays; `capacities` names exactly the restaurant's tables with integer capacities 1..100.
    Table ids, labels, timezone and combinations cannot be changed, and unknown fields are ignored.
    """
    if not isinstance(body, dict):
        raise _invalid("policy must be a JSON object")
    for field in ("effective_from", "slot_minutes", "reservation_duration_minutes",
                  "cancellation_cutoff_minutes", "opening_hours", "capacities"):
        if field not in body:
            raise _invalid(f"{field} is required")

    effective_from = body["effective_from"]
    if not isinstance(effective_from, str) or not _DATE_RE.match(effective_from):
        raise _invalid("effective_from must be a YYYY-MM-DD date")
    try:
        dt.date.fromisoformat(effective_from)
    except ValueError as exc:
        raise _invalid("effective_from is not a real calendar date") from exc

    for field in ("slot_minutes", "reservation_duration_minutes"):
        value = body[field]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1440:
            raise _invalid(f"{field} must be an integer between 1 and 1440")
    cutoff = body["cancellation_cutoff_minutes"]
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or not 0 <= cutoff <= 10080:
        raise _invalid("cancellation_cutoff_minutes must be an integer between 0 and 10080")

    hours = body["opening_hours"]
    if not isinstance(hours, list):
        raise _invalid("opening_hours must be an array")
    seen = set()
    windows = []
    for window in hours:
        if not isinstance(window, dict):
            raise _invalid("each opening_hours entry must be an object")
        weekday = window.get("weekday")
        if weekday not in WEEKDAYS:
            raise _invalid("opening_hours.weekday is not a day of the week")
        if weekday in seen:
            raise _invalid("opening_hours must not repeat a weekday")
        seen.add(weekday)
        opens, closes = window.get("opens"), window.get("closes")
        if not _valid_hhmm(opens) or not _valid_hhmm(closes):
            raise _invalid("opening hours must be a valid HH:MM time")
        if minutes_of(opens) >= minutes_of(closes):
            raise _invalid("opening hours must open before they close")
        windows.append({"weekday": weekday, "opens": opens, "closes": closes})

    capacities = body["capacities"]
    if not isinstance(capacities, dict):
        raise _invalid("capacities must be an object")
    if set(capacities) != set(table_ids):
        raise _invalid("capacities must name exactly this restaurant's tables")
    clean = {}
    for table_id, value in capacities.items():
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
            raise _invalid("each capacity must be an integer between 1 and 100")
        clean[table_id] = value

    return {
        "effective_from": effective_from,
        "slot_minutes": body["slot_minutes"],
        "reservation_duration_minutes": body["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": windows,
        "capacities": clean,
    }


def post_policy(request, match):
    """`POST /restaurants/{id}/policies` -- publish an immutable, versioned policy."""
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key is required")
    if not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    scope = request.idempotency_scope()

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute("SELECT request_hash, response_body FROM idempotency"
                              " WHERE key = ? AND user_id = ? AND scope = ?",
                              (key, user["id"], scope)).fetchone()
        if stored is not None:
            if stored["request_hash"] != request_hash:
                raise HttpError(409, "idempotency_key_reuse",
                                "this key was used with a different request body")
            replay = json.loads(stored["response_body"])
            conn.execute("COMMIT")
            return 200, replay

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (match.group("id"),)).fetchone()
        if restaurant is None:
            raise HttpError(404, "not_found", "no such restaurant")
        if user["id"] not in _json_list(restaurant["manager_user_ids"]):
            raise HttpError(403, "forbidden", "only a manager may publish a policy")
        table_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM tables WHERE restaurant_id = ? ORDER BY ordinal, id",
            (restaurant["id"],)).fetchall()]
        policy = _validate_policy(body, table_ids)
        # A failed write or replay allocates no version, so the next successful one takes the
        # smallest unused integer past the current maximum.
        version = conn.execute(
            "SELECT COALESCE(MAX(policy_version), 0) + 1 FROM policies WHERE restaurant_id = ?",
            (restaurant["id"],)).fetchone()[0]
        conn.execute(
            "INSERT INTO policies (restaurant_id, policy_version, effective_from, slot_minutes,"
            " reservation_duration_minutes, cancellation_cutoff_minutes, opening_hours, capacities,"
            " published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (restaurant["id"], version, policy["effective_from"], policy["slot_minutes"],
             policy["reservation_duration_minutes"], policy["cancellation_cutoff_minutes"],
             json.dumps(policy["opening_hours"]), json.dumps(policy["capacities"]), _now_iso()))
        published = {**policy, "policy_version": version}
        _bump_restaurant_revision(conn, restaurant["id"])
        conn.execute("INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
                     " response_body) VALUES (?, ?, ?, ?, 201, ?)",
                     (key, user["id"], scope, request_hash, json.dumps(published)))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return 201, published


def list_policies(request, match):
    """`GET /restaurants/{id}/policies` -- public, publication order, policy 0 omitted."""
    conn = store.connect()
    try:
        restaurant = conn.execute("SELECT id FROM restaurants WHERE id = ?",
                                  (match.group("id"),)).fetchone()
        if restaurant is None:
            raise HttpError(404, "not_found", "no such restaurant")
        rows = conn.execute("SELECT * FROM policies WHERE restaurant_id = ?"
                            " ORDER BY policy_version", (restaurant["id"],)).fetchall()
    finally:
        conn.close()
    return 200, {"policies": [_policy_body(row) for row in rows]}


# ---- stage 3: recurring reservations ---------------------------------------------------------


def _new_series_id() -> str:
    return "s_" + secrets.token_hex(8)


def _unique_reference(conn) -> str:
    while True:
        reference = _new_reference()
        if conn.execute("SELECT 1 FROM reservations WHERE reference = ?",
                        (reference,)).fetchone() is None:
            return reference


def _series_occurrence(row, restaurant) -> dict:
    return {
        "index": row["series_index"],
        "reference": row["reference"],
        "exception": bool(row["exception"]),
        "reservation": _reservation_body(row, restaurant),
    }


def post_series(request, match):
    """`POST /series` -- adopt a booking as occurrence zero of a recurring agreement."""
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key is required")
    if not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    scope = request.idempotency_scope()

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute("SELECT request_hash, response_body FROM idempotency"
                              " WHERE key = ? AND user_id = ? AND scope = ?",
                              (key, user["id"], scope)).fetchone()
        if stored is not None:
            if stored["request_hash"] != request_hash:
                raise HttpError(409, "idempotency_key_reuse",
                                "this key was used with a different request body")
            replay = json.loads(stored["response_body"])
            conn.execute("COMMIT")
            return 200, replay

        anchor_reference = body.get("anchor_reference")
        if not isinstance(anchor_reference, str):
            raise _invalid("anchor_reference is required")
        count = body.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 12:
            raise _invalid("count must be an integer between 2 and 12")
        interval_weeks = body.get("interval_weeks")
        if (not isinstance(interval_weeks, int) or isinstance(interval_weeks, bool)
                or not 1 <= interval_weeks <= 4):
            raise _invalid("interval_weeks must be an integer between 1 and 4")

        anchor = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                              (anchor_reference,)).fetchone()
        if anchor is None or anchor["user_id"] != user["id"]:
            raise HttpError(404, "not_found", "no such reservation")
        if anchor["status"] == "cancelled":
            raise HttpError(409, "reservation_cancelled", "this reservation is cancelled")
        if anchor["series_id"] is not None:
            raise HttpError(409, "already_in_series",
                            "this reservation already belongs to a series")
        _enforce_cutoff(anchor, _accepted_cutoff_minutes(conn, anchor), "amend")

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (anchor["restaurant_id"],)).fetchone()
        base_date = dt.date.fromisoformat(_day_of(anchor["starts_at_local"]))
        clock = anchor["starts_at_local"][11:]
        party_size = anchor["party_size"]
        anchor_tables = _reservation_table_ids(anchor)

        series_id = _new_series_id()
        conn.execute(
            "INSERT INTO series (series_id, user_id, restaurant_id, anchor_reference,"
            " interval_weeks, revision, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (series_id, user["id"], restaurant["id"], anchor_reference, interval_weeks,
             _now_iso()))
        # Occurrence zero is the anchor itself: only its series membership changes.
        conn.execute("UPDATE reservations SET series_id = ?, series_index = 0, exception = 0"
                     " WHERE reference = ?", (series_id, anchor_reference))

        anchor_row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                                  (anchor_reference,)).fetchone()
        occurrences = [{"index": 0, "reference": anchor_reference, "exception": False,
                        "reservation": _reservation_body(anchor_row, restaurant)}]

        # Each generated occurrence independently selects its date's policy and obeys the ordinary
        # opening, DST and occupancy rules. The first failure in index order is the batch's error,
        # and nothing partial survives because every write shares this one transaction.
        for index in range(1, count):
            date_i = base_date + dt.timedelta(weeks=index * interval_weeks)
            starts_at_local = f"{date_i.isoformat()}T{clock}"
            ctx = _context(conn, restaurant, date_i.isoformat())
            tables = _resolve_table_set(ctx, anchor_tables)
            starts = _validate_booking_fields(ctx, tables, starts_at_local, party_size)
            _assert_tables_free(conn, ctx, tables, starts)
            reference = _unique_reference(conn)
            table_ids = [table["id"] for table in tables]
            terms = _accepted_terms(ctx["policy"])
            created_at = _now_iso()
            ends = slot_end(starts, ctx["reservation_duration_minutes"])
            conn.execute(
                "INSERT INTO reservations (reference, restaurant_id, table_id, table_ids, user_id,"
                " starts_at_utc, starts_at_local, ends_at_utc, party_size, status, created_at,"
                " revision, accepted_terms, series_id, series_index, exception)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, 1, ?, ?, ?, 0)",
                (reference, restaurant["id"], table_ids[0], json.dumps(table_ids), user["id"],
                 starts.astimezone(dt.timezone.utc).isoformat(), starts_at_local,
                 ends.astimezone(dt.timezone.utc).isoformat(), party_size, created_at,
                 json.dumps(terms), series_id, index))
            _write_history(conn, reference, "created", [
                {"field": "table_id" if len(table_ids) == 1 else "table_ids", "from": None,
                 "to": table_ids[0] if len(table_ids) == 1 else table_ids},
                {"field": "starts_at_local", "from": None, "to": starts_at_local},
                {"field": "party_size", "from": None, "to": party_size},
            ], 1, terms)
            generated_row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                                         (reference,)).fetchone()
            occurrences.append({"index": index, "reference": reference, "exception": False,
                                "reservation": _reservation_body(generated_row, restaurant)})

        # §Recurring reservations: adoption increments the restaurant revision once.
        _bump_restaurant_revision(conn, restaurant["id"])
        result = {"series_id": series_id, "revision": 1, "interval_weeks": interval_weeks,
                  "occurrences": occurrences}
        conn.execute("INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
                     " response_body) VALUES (?, ?, ?, ?, 201, ?)",
                     (key, user["id"], scope, request_hash, json.dumps(result)))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return 201, result


def get_series(request, match):
    """`GET /series/{series_id}` -- the owner's agreement with current reservation states."""
    user = request.optional_user()
    series_id = match.group("series_id")
    conn = store.connect()
    try:
        series = conn.execute("SELECT * FROM series WHERE series_id = ?",
                              (series_id,)).fetchone()
        if series is None or user is None or series["user_id"] != user["id"]:
            raise HttpError(404, "not_found", "no such series")
        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (series["restaurant_id"],)).fetchone()
        rows = conn.execute("SELECT * FROM reservations WHERE series_id = ?"
                            " ORDER BY series_index", (series_id,)).fetchall()
        result = {
            "series_id": series_id,
            "revision": series["revision"],
            "interval_weeks": series["interval_weeks"],
            "occurrences": [_series_occurrence(row, restaurant) for row in rows],
        }
    finally:
        conn.close()
    return 200, result


# ---- stage 4: seating changes and recurring amendments --------------------------------------


def _row_span(row, duration):
    """A reservation's `(start, end)` instants, falling back to the fixture duration."""
    start = dt.datetime.fromisoformat(row["starts_at_utc"])
    if row["ends_at_utc"]:
        end = dt.datetime.fromisoformat(row["ends_at_utc"])
    else:
        end = slot_end(start, duration)
    return start, end


def _parse_replan_instant(value):
    """An explicit-offset ISO-8601 instant, or 422 `validation_failed`."""
    if not isinstance(value, str):
        raise _invalid("from and to must be ISO-8601 instants")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise _invalid("from and to must be ISO-8601 instants") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid("from and to must carry an explicit offset")
    return parsed


def _plan_options(tables, pairs, accepted):
    """Every candidate table set in the canonical rank order: singles then declared pairs.

    Ranks are positions in this full list; the caller keeps an option only when it seats the party.
    Capacity is the booking's **own accepted terms'** capacity, which is what §Seating changes
    means by "enough capacity under its own accepted terms".
    """
    capacities = accepted.get("capacities", {}) if isinstance(accepted, dict) else {}
    by_id = {table["id"]: table["capacity"] for table in tables}
    ranked = []

    def capacity(table_id):
        return capacities.get(table_id, by_id.get(table_id))

    for table in tables:
        value = capacity(table["id"])
        if value is not None:
            ranked.append(([table["id"]], value))
    for pair in pairs:
        members = [capacity(table_id) for table_id in pair]
        if any(value is None for value in members):
            continue
        ranked.append((list(pair), sum(members)))
    return [(rank, table_ids, value) for rank, (table_ids, value) in enumerate(ranked)]


def post_replan(request, match):
    """`POST /restaurants/{id}/replans` -- preview a seating plan after a table closure.

    Read-only: it stores the plan and nothing else. No closure, occupancy, revision or history is
    touched, so §Seating changes' "previews do not increment" holds by construction.
    """
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key is required")
    if not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    scope = request.idempotency_scope()

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute("SELECT request_hash, response_body FROM idempotency"
                              " WHERE key = ? AND user_id = ? AND scope = ?",
                              (key, user["id"], scope)).fetchone()
        if stored is not None:
            if stored["request_hash"] != request_hash:
                raise HttpError(409, "idempotency_key_reuse",
                                "this key was used with a different request body")
            replay = json.loads(stored["response_body"])
            conn.execute("COMMIT")
            return 200, replay

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (match.group("id"),)).fetchone()
        if restaurant is None:
            raise HttpError(404, "not_found", "no such restaurant")
        if user["id"] not in _json_list(restaurant["manager_user_ids"]):
            raise HttpError(403, "forbidden", "only a manager may plan seating changes")

        table_id = body.get("table_id")
        if not isinstance(table_id, str):
            raise _invalid("table_id is required")
        from_dt = _parse_replan_instant(body.get("from"))
        to_dt = _parse_replan_instant(body.get("to"))
        if not from_dt < to_dt:
            raise _invalid("from must be before to")

        tables = conn.execute(
            "SELECT id, label, capacity FROM tables WHERE restaurant_id = ? ORDER BY ordinal, id",
            (restaurant["id"],)).fetchall()
        tables = [{"id": t["id"], "capacity": t["capacity"]} for t in tables]
        if table_id not in {t["id"] for t in tables}:
            raise HttpError(404, "not_found", "no such table at this restaurant")
        pairs = [list(pair) for pair in _combinable_pairs(restaurant)]

        duration = restaurant["reservation_duration_minutes"]
        rows = conn.execute(
            "SELECT * FROM reservations WHERE restaurant_id = ? AND status != 'cancelled'",
            (restaurant["id"],)).fetchall()
        considered = sorted(
            (row for row in rows
             if overlaps(*_row_span(row, duration), from_dt, to_dt)),
            key=lambda row: row["reference"])
        if (len(tables) > _MAX_PLAN_TABLES or len(pairs) > _MAX_PLAN_PAIRS
                or len(considered) > _MAX_PLAN_BOOKINGS):
            raise HttpError(422, "planning_limit",
                            "too many tables, pairs or bookings to plan")

        considered_refs = {row["reference"] for row in considered}
        fixed = [row for row in rows if row["reference"] not in considered_refs]
        closures = _closure_rows(conn, restaurant["id"])

        bookings = []
        for row in considered:
            start, end = _row_span(row, duration)
            accepted = _row_accepted_terms(conn, row)
            wanted_now = frozenset(_reservation_table_ids(row))
            candidates = []
            for rank, table_ids, capacity in _plan_options(tables, pairs, accepted):
                if capacity < row["party_size"] or table_id in table_ids:
                    continue
                if _replan_blocked(table_ids, start, end, fixed, closures, duration):
                    continue
                candidates.append((rank, table_ids, capacity))
            bookings.append({"row": row, "start": start, "end": end,
                             "party_size": row["party_size"], "current": wanted_now,
                             "candidates": candidates})

        chosen = _best_seating_plan(bookings)
        if chosen is None:
            raise HttpError(409, "no_feasible_plan",
                            "no seating plan keeps every booking")

        assignments = []
        unused_seats = 0
        for index, booking in enumerate(bookings):
            _rank, table_ids, capacity = chosen[index]
            changed = frozenset(table_ids) != booking["current"]
            assignments.append({"reference": booking["row"]["reference"],
                                "table_ids": list(table_ids), "changed": bool(changed)})
            unused_seats += capacity - booking["party_size"]
        moved_count = sum(1 for assignment in assignments if assignment["changed"])

        plan_id = "p_" + secrets.token_hex(8)
        conn.execute(
            "INSERT INTO plans (plan_id, restaurant_id, table_id, from_utc, to_utc,"
            " preview_revision, assignments, applied, apply_response, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
            (plan_id, restaurant["id"], table_id,
             from_dt.astimezone(dt.timezone.utc).isoformat(),
             to_dt.astimezone(dt.timezone.utc).isoformat(),
             restaurant["restaurant_revision"], json.dumps(assignments), _now_iso()))
        result = {
            "plan_id": plan_id,
            "restaurant_revision": restaurant["restaurant_revision"],
            "closure": {"table_id": table_id, "from": format_instant(from_dt),
                        "to": format_instant(to_dt)},
            "assignments": assignments,
            "moved_count": moved_count,
            "unused_seats": unused_seats,
        }
        conn.execute("INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
                     " response_body) VALUES (?, ?, ?, ?, 201, ?)",
                     (key, user["id"], scope, request_hash, json.dumps(result)))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return 201, result


def _replan_blocked(table_ids, start, end, fixed, closures, duration):
    """Whether a candidate collides with a fixed booking or any closure."""
    wanted = set(table_ids)
    for closure in closures:
        if closure["table_id"] in wanted and overlaps(
                start, end, dt.datetime.fromisoformat(closure["from_utc"]),
                dt.datetime.fromisoformat(closure["to_utc"])):
            return True
    for row in fixed:
        if not (set(_reservation_table_ids(row)) & wanted):
            continue
        if overlaps(start, end, *_row_span(row, duration)):
            return True
    return False


def _best_seating_plan(bookings):
    """The feasible assignment minimising `(moved, unused, rank vector)` in reference order.

    A depth-first walk in reference order with the objective's own monotonicity as its pruning:
    both `moved` and `unused` only grow as bookings are placed, so once a complete plan is known a
    partial one that is already worse on the primary key cannot lead to a better leaf. Candidate
    lists are in rank order, so the first plans found are already rank-tight. The reference order is
    the list order, which the caller sorted by reference.
    """
    count = len(bookings)
    if count == 0:
        return []
    best_key = None
    best = None
    current = [None] * count

    def walk(index, moved, unused):
        nonlocal best_key, best
        if best_key is not None:
            if moved > best_key[0]:
                return
            if moved == best_key[0] and unused > best_key[1]:
                return
        if index == count:
            ranks = tuple(current[i][0] for i in range(count))
            key = (moved, unused, ranks)
            if best_key is None or key < best_key:
                best_key = key
                best = list(current)
            return
        booking = bookings[index]
        for option in booking["candidates"]:
            _rank, table_ids, _capacity = option
            clash = False
            for earlier in range(index):
                fixed_option = current[earlier]
                if (set(fixed_option[1]) & set(table_ids)
                        and overlaps(booking["start"], booking["end"],
                                     bookings[earlier]["start"], bookings[earlier]["end"])):
                    clash = True
                    break
            if clash:
                continue
            current[index] = option
            walk(index + 1,
                 moved + (0 if frozenset(table_ids) == booking["current"] else 1),
                 unused + _capacity - booking["party_size"])
            current[index] = None

    walk(0, 0, 0)
    return best


def apply_replan(request, match):
    """`POST /restaurants/{id}/replans/{plan_id}/apply` -- record the closure and every move."""
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key is required")
    if not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    scope = request.idempotency_scope()

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute("SELECT request_hash, response_body FROM idempotency"
                              " WHERE key = ? AND user_id = ? AND scope = ?",
                              (key, user["id"], scope)).fetchone()
        if stored is not None:
            if stored["request_hash"] != request_hash:
                raise HttpError(409, "idempotency_key_reuse",
                                "this key was used with a different request body")
            replay = json.loads(stored["response_body"])
            conn.execute("COMMIT")
            return 200, replay

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (match.group("id"),)).fetchone()
        if restaurant is None:
            raise HttpError(404, "not_found", "no such restaurant")
        if user["id"] not in _json_list(restaurant["manager_user_ids"]):
            raise HttpError(403, "forbidden", "only a manager may apply seating changes")

        plan = conn.execute("SELECT * FROM plans WHERE plan_id = ?",
                            (match.group("plan_id"),)).fetchone()
        if plan is None or plan["restaurant_id"] != restaurant["id"]:
            raise HttpError(404, "not_found", "no such plan")
        if plan["applied"]:
            raise HttpError(409, "plan_already_applied",
                            "this plan has already been applied")
        if plan["preview_revision"] != restaurant["restaurant_revision"]:
            raise HttpError(409, "stale_plan",
                            "the restaurant changed since this plan was previewed")

        conn.execute(
            "INSERT INTO closures (plan_id, restaurant_id, table_id, from_utc, to_utc, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (plan["plan_id"], restaurant["id"], plan["table_id"], plan["from_utc"],
             plan["to_utc"], _now_iso()))

        assignments = json.loads(plan["assignments"])
        touched_series = set()
        for assignment in assignments:
            if not assignment["changed"]:
                continue
            reference = assignment["reference"]
            row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                               (reference,)).fetchone()
            table_ids = list(assignment["table_ids"])
            revision = row["revision"] + 1
            # Identity, owner, times, party size, accepted terms and the exception flag are all
            # preserved: a seating repair only changes which tables the booking holds.
            conn.execute(
                "UPDATE reservations SET table_id = ?, table_ids = ?, revision = ?"
                " WHERE reference = ?",
                (table_ids[0], json.dumps(table_ids), revision, reference))
            changes = [{"field": "table_ids", "from": _reservation_table_ids(row),
                        "to": table_ids}]
            _write_history(conn, reference, "reassigned", changes, revision,
                           _row_accepted_terms(conn, row), plan_id=plan["plan_id"])
            if row["series_id"]:
                touched_series.add(row["series_id"])

        for series_id in touched_series:
            _bump_series_revision(conn, series_id)
        _bump_restaurant_revision(conn, restaurant["id"])
        new_revision = restaurant["restaurant_revision"] + 1

        results = []
        for assignment in assignments:
            row = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                               (assignment["reference"],)).fetchone()
            results.append(_reservation_body(row, restaurant))
        result = {"plan_id": plan["plan_id"], "restaurant_revision": new_revision,
                  "reservations": results}
        conn.execute("UPDATE plans SET applied = 1, apply_response = ? WHERE plan_id = ?",
                     (json.dumps(result), plan["plan_id"]))
        conn.execute("INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
                     " response_body) VALUES (?, ?, ?, ?, 201, ?)",
                     (key, user["id"], scope, request_hash, json.dumps(result)))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return 201, result


_LOCAL_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def post_series_amend(request, match):
    """`POST /series/{series_id}/amend` -- change the clock time of later occurrences."""
    user = request.require_user()
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key is required")
    if not 1 <= len(key) <= 255:
        raise _invalid("Idempotency-Key must be 1..255 characters")

    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("request body must be a JSON object")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    scope = request.idempotency_scope()

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute("SELECT request_hash, response_body FROM idempotency"
                              " WHERE key = ? AND user_id = ? AND scope = ?",
                              (key, user["id"], scope)).fetchone()
        if stored is not None:
            if stored["request_hash"] != request_hash:
                raise HttpError(409, "idempotency_key_reuse",
                                "this key was used with a different request body")
            replay = json.loads(stored["response_body"])
            conn.execute("COMMIT")
            return 200, replay

        series = conn.execute("SELECT * FROM series WHERE series_id = ?",
                              (match.group("series_id"),)).fetchone()
        if series is None or series["user_id"] != user["id"]:
            raise HttpError(404, "not_found", "no such series")

        expected = body.get("expected_revision")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise _invalid("expected_revision must be a positive integer")
        from_index = body.get("from_index")
        if not isinstance(from_index, int) or isinstance(from_index, bool) or from_index < 0:
            raise _invalid("from_index must be a non-negative integer")
        local_time = body.get("local_time")
        if not isinstance(local_time, str) or not _LOCAL_TIME_RE.match(local_time):
            raise _invalid("local_time must be HH:MM between 00:00 and 23:59")

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (series["restaurant_id"],)).fetchone()
        occurrences = conn.execute("SELECT * FROM reservations WHERE series_id = ?"
                                   " ORDER BY series_index", (series["series_id"],)).fetchall()
        if from_index > len(occurrences) - 1:
            raise _invalid("from_index is past the series' last occurrence")
        if expected != series["revision"]:
            raise HttpError(409, "stale_revision",
                            "the series changed since you read it")

        eligible = [row for row in occurrences
                    if row["series_index"] >= from_index
                    and row["status"] != "cancelled" and not row["exception"]]

        # Pass one: every non-occupancy rule, in occurrence-index order.
        planned = []
        for row in eligible:
            date = _day_of(row["starts_at_local"])
            new_local = f"{date}T{local_time}"
            if new_local == row["starts_at_local"]:
                planned.append({"row": row, "real": False})
                continue
            _enforce_cutoff(row, _accepted_cutoff_minutes(conn, row), "amend")
            ctx = _context(conn, restaurant, date)
            tables = _resolve_table_set(ctx, _reservation_table_ids(row))
            starts = _validate_booking_fields(ctx, tables, new_local, row["party_size"])
            terms = _accepted_terms(ctx["policy"])
            ends = slot_end(starts, ctx["reservation_duration_minutes"])
            planned.append({"row": row, "real": True, "ctx": ctx, "tables": tables,
                            "starts": starts, "starts_at_local": new_local, "terms": terms,
                            "ends": ends})

        # Pass two: occupancy against the operation's resulting state. The occurrences being
        # re-timed are dropped from the fixed set and re-checked against one another as they are
        # compiled, so a change that would collide with an unchanged booking, another re-timed
        # occurrence or an applied closure is refused with `table_unavailable`.
        amended_refs = {entry["row"]["reference"] for entry in planned if entry["real"]}
        duration = restaurant["reservation_duration_minutes"]
        fixed = [row for row in conn.execute(
            "SELECT * FROM reservations WHERE restaurant_id = ? AND status != 'cancelled'",
            (restaurant["id"],)).fetchall() if row["reference"] not in amended_refs]
        closures = _closure_rows(conn, restaurant["id"])
        compiled = []
        for entry in planned:
            if not entry["real"]:
                continue
            wanted = {table["id"] for table in entry["tables"]}
            for other_tables, other_start, other_end in compiled:
                if wanted & {table["id"] for table in other_tables} \
                        and overlaps(entry["starts"], entry["ends"], other_start, other_end):
                    raise HttpError(409, "table_unavailable", "that table is already booked")
            for closure in closures:
                if closure["table_id"] in wanted and overlaps(
                        entry["starts"], entry["ends"],
                        dt.datetime.fromisoformat(closure["from_utc"]),
                        dt.datetime.fromisoformat(closure["to_utc"])):
                    raise HttpError(409, "table_unavailable", "that table is closed")
            for other in fixed:
                if not (set(_reservation_table_ids(other)) & wanted):
                    continue
                if overlaps(entry["starts"], entry["ends"], *_row_span(other, duration)):
                    raise HttpError(409, "table_unavailable", "that table is already booked")
            compiled.append((entry["tables"], entry["starts"], entry["ends"]))

        any_real = False
        for entry in planned:
            if not entry["real"]:
                continue
            row = entry["row"]
            revision = row["revision"] + 1
            conn.execute(
                "UPDATE reservations SET starts_at_local = ?, starts_at_utc = ?,"
                " ends_at_utc = ?, revision = ?, accepted_terms = ? WHERE reference = ?",
                (entry["starts_at_local"],
                 entry["starts"].astimezone(dt.timezone.utc).isoformat(),
                 entry["ends"].astimezone(dt.timezone.utc).isoformat(), revision,
                 json.dumps(entry["terms"]), row["reference"]))
            _write_history(conn, row["reference"], "changed", [
                {"field": "starts_at_local", "from": row["starts_at_local"],
                 "to": entry["starts_at_local"]}], revision, entry["terms"])
            any_real = True

        if any_real:
            _bump_series_revision(conn, series["series_id"])
            _bump_restaurant_revision(conn, restaurant["id"])
        new_revision = series["revision"] + 1 if any_real else series["revision"]
        current = conn.execute("SELECT * FROM reservations WHERE series_id = ?"
                               " ORDER BY series_index", (series["series_id"],)).fetchall()
        result = {
            "series_id": series["series_id"],
            "revision": new_revision,
            "interval_weeks": series["interval_weeks"],
            "occurrences": [_series_occurrence(row, restaurant) for row in current],
        }
        conn.execute("INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
                     " response_body) VALUES (?, ?, ?, ?, 201, ?)",
                     (key, user["id"], scope, request_hash, json.dumps(result)))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the rollback failure is not the interesting error
            pass
        raise
    finally:
        conn.close()
    return 201, result


class Html:
    """A handler result that is rendered as HTML rather than the JSON envelope."""

    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.body = body
        self.content_type = content_type


def ui(request, match):
    """Serve the single-page UI. All four browser routes share one document (§9)."""
    return 200, Html(_UI_HTML.encode("utf-8"))


_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tablekeeper</title>
<style>
 :root{
   --bg:#f6f4ef;
   --surface:#ffffff;
   --ink:#1f211e;
   --muted:#6a6f68;
   --line:#e5e1d8;
   --brand:#1f5f4a;
   --brand-ink:#123c2f;
   --brand-soft:#e8f2ec;
   --accent:#c2591f;
   --ok:#1f7a4d;
   --ok-soft:#e7f4ec;
   --ok-line:#bfe0cd;
   --warn:#8a5a00;
   --warn-soft:#fdf4e0;
   --warn-line:#ecd9b0;
   --danger:#a4262c;
   --danger-soft:#fbeaeb;
   --danger-line:#f0c9cc;
   --radius:14px;
   --shadow:0 1px 2px rgba(20,25,20,.05),0 10px 28px rgba(20,25,20,.07);
   --maxw:960px;
 }
 *{box-sizing:border-box}
 html,body{margin:0}
 body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
   color:var(--ink);background:linear-gradient(180deg,#fbfaf7 0%,var(--bg) 220px);
   line-height:1.5;-webkit-font-smoothing:antialiased}
 a{color:var(--brand);text-decoration:none}
 a:hover{text-decoration:underline}
 :focus-visible{outline:3px solid var(--brand);outline-offset:2px;border-radius:8px}
 #header{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.94);
   backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
 #header nav{max-width:var(--maxw);margin:0 auto;padding:.6rem 1rem;
   display:flex;align-items:center;gap:.35rem;flex-wrap:wrap}
 .brand{font-weight:800;font-size:1.1rem;letter-spacing:-.02em;color:var(--brand-ink);
   margin-right:auto;padding:.3rem .4rem}
 .brand:hover{text-decoration:none}
 .brand .dot{color:var(--accent)}
 .navlink{padding:.4rem .65rem;border-radius:999px;color:var(--ink);font-weight:500}
 .navlink:hover{background:var(--brand-soft);text-decoration:none}
 [data-testid='current-user']{font-weight:600;color:var(--brand-ink);padding:0 .3rem}
 main{max-width:var(--maxw);margin:0 auto;padding:1.5rem 1rem 3.5rem}
 h1{font-size:1.55rem;line-height:1.2;margin:.2rem 0 1.1rem;letter-spacing:-.02em}
 .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
   box-shadow:var(--shadow);padding:1.1rem 1.2rem;margin:0 0 1rem}
 .auth{max-width:420px}
 .stack{display:flex;flex-direction:column;gap:.85rem}
 .search{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
   gap:.85rem;align-items:end}
 .field{display:flex;flex-direction:column;gap:.35rem;margin:0}
 .field-label{font-size:.82rem;font-weight:600;color:var(--muted)}
 input,select,button{font:inherit}
 input,select{width:100%;padding:.6rem .65rem;border:1px solid var(--line);
   border-radius:10px;background:#fff;color:var(--ink)}
 input:hover,select:hover{border-color:#d2ccbf}
 input:focus,select:focus{border-color:var(--brand);outline:none;
   box-shadow:0 0 0 3px rgba(31,95,74,.15)}
 button{cursor:pointer}
 .btn{background:var(--brand);color:#fff;border:1px solid var(--brand);
   padding:.6rem 1rem;border-radius:10px;font-weight:600;transition:background .12s}
 .btn:hover{background:var(--brand-ink)}
 .btn.secondary{background:#fff;color:var(--brand-ink);border-color:var(--line);font-weight:500}
 .btn.secondary:hover{background:var(--brand-soft)}
 .btn.danger{background:#fff;color:var(--danger);border-color:var(--danger-line);font-weight:600}
 .btn.danger:hover{background:var(--danger-soft)}
 [data-testid='availability-grid']{margin-top:1.25rem;display:flex;flex-direction:column;gap:.55rem}
 .slot{display:grid;grid-template-columns:3.6rem 1fr;gap:.6rem;align-items:center}
 .slot-time{font-variant-numeric:tabular-nums;font-weight:600;color:var(--muted);font-size:.85rem}
 .slot-cells{display:flex;flex-wrap:wrap;gap:.45rem}
 .cell{padding:.45rem .7rem;border:1px solid var(--line);background:#fff;
   border-radius:9px;color:var(--ink);font-size:.9rem;transition:background .12s,border-color .12s}
 .cell[data-available='false']{background:#f1efe9;color:#a9a79f;border-color:#e6e2d9;cursor:not-allowed}
 .cell[data-available='true']{background:var(--ok-soft);border-color:var(--ok-line);
   color:#155c3a;font-weight:600}
 .cell[data-available='true']:hover{background:#d8ede0;border-color:var(--ok)}
 .cell.combo{background:var(--brand-soft);border-color:#c4ddd2;color:var(--brand-ink)}
 .cell.combo:hover{background:#d5e8df}
 [data-testid='no-slots']{color:var(--muted);padding:1.1rem 1.2rem;background:var(--surface);
   border:1px dashed var(--line);border-radius:var(--radius);margin-top:1.25rem}
 [data-testid='booking-form'],[data-testid='confirmation']{margin-top:1.25rem;
   border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);
   box-shadow:var(--shadow);padding:1rem 1.1rem}
 [data-testid='booking-summary']{font-weight:600;margin-bottom:.75rem;color:var(--brand-ink)}
 [data-testid='confirmation']{border-color:var(--ok-line);background:var(--ok-soft)}
 [data-testid='confirmation-reference']{font-size:1.2rem;font-weight:800;
   color:var(--brand-ink);letter-spacing:.02em;margin-bottom:.35rem}
 [data-testid='auth-error'],[data-testid='booking-error'],[data-testid='reservation-error']{
   display:block;background:var(--danger-soft);color:var(--danger);border:1px solid var(--danger-line);
   border-radius:10px;padding:.6rem .75rem;margin:.7rem 0;font-weight:500}
 [data-testid='booking-uncertain']{display:block;background:var(--warn-soft);color:var(--warn);
   border:1px solid var(--warn-line);border-radius:10px;padding:.6rem .75rem;margin:.7rem 0;font-weight:500}
 [data-testid='reservation-detail']{border:1px solid var(--line);border-radius:var(--radius);
   background:var(--surface);box-shadow:var(--shadow);padding:1rem 1.1rem;margin-top:1.1rem}
 [data-testid='reservation-status']{display:inline-block;font-size:.75rem;font-weight:700;
   letter-spacing:.03em;padding:.22rem .6rem;border-radius:999px;background:var(--brand-soft);
   color:var(--brand-ink);margin-bottom:.5rem}
 [data-testid='reservation-tables']{color:var(--muted)}
 @media (max-width:420px){
   main{padding:1rem .8rem 2.5rem}
   .slot{grid-template-columns:3rem 1fr;gap:.45rem}
   .cell{padding:.4rem .55rem;font-size:.85rem}
 }
</style>
</head>
<body>
<header id="header"></header>
<main id="app"></main>
<script>
const $ = function(id){ return document.querySelector("[data-testid='" + id + "']"); };
function esc(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
    return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
  });
}
let token = localStorage.getItem('tk_token');
let currentUser = null;
try { currentUser = JSON.parse(localStorage.getItem('tk_user') || 'null'); } catch (e) { currentUser = null; }
let booking = null;
let searchSeq = 0;
let lastSearch = null;

function setSession(t, u){
  token = t; currentUser = u;
  if (t) { localStorage.setItem('tk_token', t); } else { localStorage.removeItem('tk_token'); }
  if (u) { localStorage.setItem('tk_user', JSON.stringify(u)); } else { localStorage.removeItem('tk_user'); }
  renderHeader();
}
async function api(method, path, body, extra){
  const headers = { 'Content-Type': 'application/json' };
  if (token) { headers['Authorization'] = 'Bearer ' + token; }
  if (extra) { for (const k in extra) { headers[k] = extra[k]; } }
  const opts = { method: method, headers: headers };
  if (body !== undefined) { opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  return { ok: res.ok, status: res.status, data: data };
}
function messageOf(data){
  if (data && data.error && data.error.message) { return data.error.message; }
  return 'Request failed';
}
function newKey(){
  if (window.crypto && crypto.randomUUID) { return crypto.randomUUID(); }
  return 'k' + Math.random().toString(36).slice(2) + Date.now().toString(36);
}
function labelOf(tables, id){
  for (let i = 0; i < tables.length; i++) { if (tables[i].id === id) { return tables[i].label; } }
  return id;
}
function defaultDate(){
  const d = new Date();
  d.setDate(d.getDate() + 7);
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return d.getFullYear() + '-' + m + '-' + day;
}

function renderHeader(){
  const h = document.getElementById('header');
  let html = '<nav>' +
    '<a class="brand" href="/">Tablekeeper<span class="dot">.</span></a>' +
    '<a class="navlink" href="/">Search</a>' +
    '<a class="navlink" href="/lookup">Lookup</a>';
  if (currentUser) {
    html += '<span data-testid="current-user">' + esc(currentUser.display_name) + '</span>';
    html += '<button class="btn secondary" data-testid="logout-button">Log out</button>';
  } else {
    html += '<a class="navlink" href="/login">Log in</a>' +
            '<a class="navlink" href="/signup">Sign up</a>';
  }
  html += '</nav>';
  h.innerHTML = html;
  const out = $('logout-button');
  if (out) { out.onclick = function(){ setSession(null, null); route(); }; }
}

function route(){
  renderHeader();
  const path = location.pathname;
  if (path === '/signup') { return renderSignup(); }
  if (path === '/login') { return renderLogin(); }
  if (path === '/lookup') { return renderLookup(); }
  return renderSearch();
}

function showAuthError(message){
  let el = $('auth-error');
  if (!el) {
    el = document.createElement('div');
    el.setAttribute('data-testid', 'auth-error');
    const host = document.getElementById('auth-error-slot') || document.getElementById('app');
    host.appendChild(el);
  }
  el.textContent = message;
}
function clearAuthError(){
  const el = $('auth-error');
  if (el) { el.remove(); }
}

function renderLogin(){
  document.getElementById('app').innerHTML =
    '<div class="card auth">' +
    '<h1>Log in</h1>' +
    '<form id="login-form" class="stack">' +
    '<label class="field"><span class="field-label">Email</span><input type="email" autocomplete="email" data-testid="login-email"></label>' +
    '<label class="field"><span class="field-label">Password</span><input type="password" autocomplete="current-password" data-testid="login-password"></label>' +
    '<button class="btn" type="submit" data-testid="login-submit">Log in</button>' +
    '</form>' +
    '<div id="auth-error-slot"></div>' +
    '</div>';
  document.getElementById('login-form').onsubmit = async function(e){
    e.preventDefault();
    clearAuthError();
    const r = await api('POST', '/auth/login',
      { email: $('login-email').value, password: $('login-password').value });
    if (r.ok && r.data && r.data.token) { setSession(r.data.token, r.data); route(); }
    else { showAuthError(messageOf(r.data)); }
  };
}

function renderSignup(){
  document.getElementById('app').innerHTML =
    '<div class="card auth">' +
    '<h1>Sign up</h1>' +
    '<form id="signup-form" class="stack">' +
    '<label class="field"><span class="field-label">Email</span><input type="email" autocomplete="email" data-testid="signup-email"></label>' +
    '<label class="field"><span class="field-label">Password</span><input type="password" autocomplete="new-password" data-testid="signup-password"></label>' +
    '<label class="field"><span class="field-label">Display name</span><input autocomplete="name" data-testid="signup-display-name"></label>' +
    '<button class="btn" type="submit" data-testid="signup-submit">Sign up</button>' +
    '</form>' +
    '<div id="auth-error-slot"></div>' +
    '</div>';
  document.getElementById('signup-form').onsubmit = async function(e){
    e.preventDefault();
    clearAuthError();
    const r = await api('POST', '/auth/signup', {
      email: $('signup-email').value,
      password: $('signup-password').value,
      display_name: $('signup-display-name').value
    });
    if (r.ok && r.data && r.data.token) { setSession(r.data.token, r.data); route(); }
    else { showAuthError(messageOf(r.data)); }
  };
}

function renderSearch(){
  document.getElementById('app').innerHTML =
    '<div class="card">' +
    '<h1>Find a table</h1>' +
    '<div class="search">' +
    '<label class="field"><span class="field-label">Restaurant</span><select data-testid="restaurant-select"></select></label>' +
    '<label class="field"><span class="field-label">Date</span><input type="date" data-testid="date-input"></label>' +
    '<label class="field"><span class="field-label">Party size</span><input type="number" min="1" value="2" data-testid="party-size-input"></label>' +
    '<button class="btn" data-testid="search-button">Search</button>' +
    '</div>' +
    '</div>' +
    '<div id="auth-error-slot"></div>' +
    '<div id="grid-area"></div>' +
    '<div data-testid="booking-area"></div>';
  $('date-input').value = defaultDate();
  loadRestaurants();
  $('search-button').onclick = doSearch;
}
async function loadRestaurants(){
  const r = await api('GET', '/restaurants');
  const select = $('restaurant-select');
  if (!select || !r.data) { return; }
  select.innerHTML = '';
  (r.data.restaurants || []).forEach(function(x){
    const option = document.createElement('option');
    option.value = x.id;
    option.textContent = x.name;
    select.appendChild(option);
  });
}
function renderNoSlots(){
  const area = document.getElementById('grid-area');
  if (area) { area.innerHTML = '<div data-testid="no-slots">No tables available</div>'; }
}
async function doSearch(){
  const seq = ++searchSeq;
  const area = $('booking-area');
  if (area) { area.innerHTML = ''; }
  const rid = $('restaurant-select').value;
  const date = $('date-input').value;
  const party = parseInt($('party-size-input').value, 10);
  const detail = await api('GET', '/restaurants/' + encodeURIComponent(rid));
  const avail = await api('GET', '/availability?restaurant_id=' + encodeURIComponent(rid) +
    '&date=' + encodeURIComponent(date) + '&party_size=' + encodeURIComponent(party));
  if (seq !== searchSeq) { return; }
  if (!detail.ok || !avail.ok) { renderNoSlots(); return; }
  lastSearch = { detail: detail.data, rid: rid, date: date, party: party };
  renderGrid(detail.data, avail.data, party);
}
async function refreshGrid(){
  if (!lastSearch) { return; }
  const avail = await api('GET', '/availability?restaurant_id=' + encodeURIComponent(lastSearch.rid) +
    '&date=' + encodeURIComponent(lastSearch.date) + '&party_size=' + encodeURIComponent(lastSearch.party));
  if (avail.ok && avail.data) { renderGrid(lastSearch.detail, avail.data, lastSearch.party); }
}
function renderGrid(detail, avail, party){
  const area = document.getElementById('grid-area');
  const slots = avail.slots || [];
  if (slots.length === 0) { renderNoSlots(); return; }
  const tables = detail.tables || [];
  const grid = document.createElement('div');
  grid.setAttribute('data-testid', 'availability-grid');
  slots.forEach(function(slot){
    const time = slot.starts_at_local.split('T')[1];
    const row = document.createElement('div');
    row.className = 'slot';
    const label = document.createElement('span');
    label.className = 'slot-time';
    label.textContent = time;
    row.appendChild(label);
    const cells = document.createElement('div');
    cells.className = 'slot-cells';
    const available = {};
    (slot.available_table_ids || []).forEach(function(id){ available[id] = true; });
    tables.forEach(function(t){
      const cell = document.createElement('button');
      cell.setAttribute('data-testid', 'slot-' + t.id + '-' + time);
      cell.setAttribute('data-available', available[t.id] ? 'true' : 'false');
      cell.className = 'cell';
      cell.textContent = t.label;
      cell.onclick = function(){
        if (cell.getAttribute('data-available') !== 'true') { return; }
        openBooking(detail, [t.id], time, slot.starts_at_local, party);
      };
      cells.appendChild(cell);
    });
    (slot.available_options || []).forEach(function(opt){
      if (!opt.table_ids || opt.table_ids.length < 2) { return; }
      const ids = opt.table_ids;
      const cell = document.createElement('button');
      cell.setAttribute('data-testid', 'slot-' + ids.join('+') + '-' + time);
      cell.setAttribute('data-available', 'true');
      cell.className = 'cell combo';
      cell.textContent = ids.map(function(id){ return labelOf(tables, id); }).join('+');
      cell.onclick = function(){ openBooking(detail, ids, time, slot.starts_at_local, party); };
      cells.appendChild(cell);
    });
    row.appendChild(cells);
    grid.appendChild(row);
  });
  area.innerHTML = '';
  area.appendChild(grid);
}
function openBooking(detail, tableIds, time, startsLocal, party){
  clearAuthError();
  if (!token) { showAuthError('Sign in to book a table'); return; }
  booking = { detail: detail, tableIds: tableIds, startsLocal: startsLocal, party: party, key: newKey(), busy: false };
  const area = $('booking-area');
  area.innerHTML = '';
  const form = document.createElement('div');
  form.setAttribute('data-testid', 'booking-form');
  form.className = 'stack';
  const labels = tableIds.map(function(id){ return labelOf(detail.tables, id); }).join(', ');
  form.innerHTML =
    '<div data-testid="booking-summary">' + esc(detail.name) + ' — Table ' + esc(labels) +
    ' at ' + esc(time) + '</div>' +
    '<label class="field"><span class="field-label">Party size</span><input type="number" min="1" data-testid="booking-party-size" value="' + party + '"></label>' +
    '<button class="btn" data-testid="booking-submit">Book</button>';
  area.appendChild(form);
  const size = $('booking-party-size');
  size.oninput = function(){
    booking.party = parseInt(size.value, 10) || 0;
    booking.key = newKey();
    clearBookingError();
    clearUncertain();
  };
  $('booking-submit').onclick = submitBooking;
}
function formEl(testid){
  const form = $('booking-form');
  if (!form) { return null; }
  let el = form.querySelector("[data-testid='" + testid + "']");
  if (!el) {
    el = document.createElement('div');
    el.setAttribute('data-testid', testid);
    form.appendChild(el);
  }
  return el;
}
function setBookingError(message){ const el = formEl('booking-error'); if (el) { el.textContent = message; } }
function clearBookingError(){ const form = $('booking-form'); if (form) { const e = form.querySelector("[data-testid='booking-error']"); if (e) { e.remove(); } } }
function setUncertain(message){ const el = formEl('booking-uncertain'); if (el) { el.textContent = message; } }
function clearUncertain(){ const form = $('booking-form'); if (form) { const e = form.querySelector("[data-testid='booking-uncertain']"); if (e) { e.remove(); } } }
async function submitBooking(){
  if (!booking || booking.busy) { return; }
  const body = {
    restaurant_id: booking.detail.id,
    table_ids: booking.tableIds,
    starts_at_local: booking.startsLocal,
    party_size: booking.party
  };
  booking.busy = true;
  let r = null;
  try {
    r = await api('POST', '/reservations', body, { 'Idempotency-Key': booking.key });
  } catch (e) {
    booking.busy = false;
    setUncertain('We could not confirm that booking. Press Book to try again.');
    return;
  }
  booking.busy = false;
  if (r.ok && r.data) {
    clearBookingError();
    clearUncertain();
    renderConfirmation(r.data);
  } else {
    setBookingError(messageOf(r.data));
    if (r.status === 409) { await refreshGrid(); }
  }
}
function renderConfirmation(data){
  const area = $('booking-area');
  if (!area) { return; }
  let el = area.querySelector("[data-testid='confirmation']");
  if (!el) {
    el = document.createElement('div');
    el.setAttribute('data-testid', 'confirmation');
    area.appendChild(el);
  }
  const ids = data.table_ids || booking.tableIds;
  const labels = ids.map(function(id){ return labelOf(booking.detail.tables, id); }).join(', ');
  const time = (data.starts_at_local || booking.startsLocal).split('T')[1];
  el.innerHTML =
    '<div data-testid="confirmation-reference">' + esc(data.reference) + '</div>' +
    '<div data-testid="confirmation-details">' + esc(booking.detail.name) + ' — Table ' +
    esc(labels) + ' at ' + esc(time) + '</div>' +
    '<div data-testid="confirmation-tables">' + esc(labels) + '</div>';
}

function renderLookup(){
  document.getElementById('app').innerHTML =
    '<div class="card auth">' +
    '<h1>Look up a booking</h1>' +
    '<form id="lookup-form" class="search">' +
    '<label class="field"><span class="field-label">Reference</span><input data-testid="lookup-reference-input"></label>' +
    '<button class="btn" type="submit" data-testid="lookup-submit">Look up</button>' +
    '</form>' +
    '<div data-testid="lookup-result"></div>' +
    '</div>';
  document.getElementById('lookup-form').onsubmit = async function(e){
    e.preventDefault();
    await doLookup();
  };
}
async function doLookup(){
  const reference = $('lookup-reference-input').value.trim();
  const r = await api('GET', '/reservations/' + encodeURIComponent(reference));
  const out = $('lookup-result');
  if (r.ok && r.data) { renderReservation(out, r.data); }
  else { out.innerHTML = '<div data-testid="reservation-error">' + esc(messageOf(r.data)) + '</div>'; }
}
async function renderReservation(out, data){
  const ids = data.table_ids || (data.table_id ? [data.table_id] : []);
  let labels = ids.join(', ');
  try {
    const detail = await api('GET', '/restaurants/' + encodeURIComponent(data.restaurant_id));
    if (detail.ok && detail.data) {
      labels = ids.map(function(id){ return labelOf(detail.data.tables || [], id); }).join(', ');
    }
  } catch (e) { /* keep the technical ids as a fallback */ }
  let html = '<div data-testid="reservation-detail">' +
    '<div data-testid="reservation-status">' + esc(data.status) + '</div>' +
    '<div data-testid="reservation-tables">' + esc(labels) + '</div>';
  if (data.status === 'confirmed') {
    html += '<button class="btn danger" data-testid="reservation-cancel-button">Cancel</button>';
  }
  html += '</div>';
  out.innerHTML = html;
  const button = $('reservation-cancel-button');
  if (button) {
    button.onclick = async function(){
      const r = await api('POST', '/reservations/' + encodeURIComponent(data.reference) + '/cancel');
      if (r.ok && r.data) { renderReservation(out, r.data); }
      else {
        let err = out.querySelector("[data-testid='reservation-error']");
        if (!err) {
          err = document.createElement('div');
          err.setAttribute('data-testid', 'reservation-error');
          out.appendChild(err);
        }
        err.textContent = messageOf(r.data);
      }
    };
  }
}

route();
</script>
</body>
</html>
"""


ROUTES = [
    ("GET", re.compile(r"^/$"), ui),
    ("GET", re.compile(r"^/signup$"), ui),
    ("GET", re.compile(r"^/login$"), ui),
    ("GET", re.compile(r"^/lookup$"), ui),
    ("GET", re.compile(r"^/health$"), health),
    ("POST", re.compile(r"^/_test/reset$"), reset),
    ("GET", re.compile(r"^/_test/export$"), export_snapshot),
    ("POST", re.compile(r"^/_test/import$"), import_snapshot),
    ("POST", re.compile(r"^/auth/signup$"), post_signup),
    ("POST", re.compile(r"^/auth/login$"), post_login),
    ("GET", re.compile(r"^/restaurants$"), list_restaurants),
    ("GET", re.compile(r"^/restaurants/(?P<id>[^/]+)$"), get_restaurant),
    ("GET", re.compile(r"^/restaurants/(?P<id>[^/]+)/policies$"), list_policies),
    ("POST", re.compile(r"^/restaurants/(?P<id>[^/]+)/policies$"), post_policy),
    ("GET", re.compile(r"^/availability$"), get_availability),
    ("POST", re.compile(r"^/reservations$"), post_reservation),
    ("GET", re.compile(r"^/reservations$"), list_reservations),
    ("GET", re.compile(r"^/reservations/(?P<reference>[^/]+)$"), get_reservation),
    ("PATCH", re.compile(r"^/reservations/(?P<reference>[^/]+)$"), patch_reservation),
    ("GET", re.compile(r"^/reservations/(?P<reference>[^/]+)/history$"), get_history),
    ("GET", re.compile(r"^/reservations/(?P<reference>[^/]+)/decision$"), get_decision),
    ("POST", re.compile(r"^/reservation-moves$"), post_reservation_moves),
    ("POST", re.compile(r"^/reservations/(?P<reference>[^/]+)/cancel$"), cancel_reservation),
    ("POST", re.compile(r"^/series$"), post_series),
    ("GET", re.compile(r"^/series/(?P<series_id>[^/]+)$"), get_series),
    ("POST", re.compile(r"^/series/(?P<series_id>[^/]+)/amend$"), post_series_amend),
    ("POST", re.compile(r"^/restaurants/(?P<id>[^/]+)/replans$"), post_replan),
    ("POST", re.compile(r"^/restaurants/(?P<id>[^/]+)/replans/(?P<plan_id>[^/]+)/apply$"),
     apply_replan),
]


class Request:
    """One parsed HTTP request."""

    def __init__(self, handler):
        self.handler = handler
        self.headers = handler.headers
        self.path = urllib.parse.urlsplit(handler.path).path
        self.query = {k: v[-1] for k, v in
                      urllib.parse.parse_qs(handler.path.partition("?")[2]).items()}
        self.user = None
        self._body = None

    def idempotency_scope(self) -> str:
        """§7:79 scopes a replay to "same user, same method, same path, same body".

        Moves is §7's second idempotency-required path, so the path has to be part of the lookup:
        without it a key spent on create would make the same body on moves look like a replay of
        the *other* endpoint, which §7:80 forbids in as many words.
        """
        return f"{self.handler.command} {self.path}"

    def raw_body(self) -> bytes:
        if self._body is None:
            length = int(self.headers.get("Content-Length") or 0)
            self._body = self.handler.rfile.read(length) if length else b""
        return self._body

    def drain_body(self) -> None:
        """Consume an unread request body so the connection stays in sync.

        `BaseHTTPRequestHandler` keeps the connection alive for HTTP/1.1 and reads the next request
        line straight from the socket. A handler that refuses before touching the body -- every 401
        and the 404s raised before field checks -- would otherwise leave that many bytes in the
        stream, and the *next* request on the same connection would be parsed starting mid-body
        (a 400, or a 501 for a nonsense verb). Reading the body here costs nothing and keeps a
        caller that reuses one connection honest.
        """
        if self._body is None:
            self.raw_body()

    def json_body(self):
        raw = self.raw_body()
        if not raw:
            raise _malformed("a JSON object is required")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _malformed("body is not valid JSON") from exc

    def bearer_token(self):
        header = self.headers.get("Authorization") or ""
        prefix, _, value = header.partition(" ")
        if prefix.lower() != "bearer" or not value.strip():
            return None
        return value.strip()

    def require_user(self):
        """The authenticated user, or 401.

        A missing header and an unknown token both land here and are answered identically, so a
        caller cannot use the response to learn whether a token exists.
        """
        if self.user is None:
            self.user = auth.user_for_token(self.bearer_token())
        if self.user is None:
            raise HttpError(401, "unauthenticated", "a valid bearer token is required")
        return self.user

    def optional_user(self):
        """The authenticated user, or `None` -- for history/decision, whose non-owner reply is 404.

        §Policies: "History and decision return 404 even without authentication, resolving the
        exception to stage 1's general 401 rule." So these routes must not call `require_user`.
        """
        if self.user is None:
            self.user = auth.user_for_token(self.bearer_token())
        return self.user


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tablekeeper/1.0"

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        request = Request(self)
        try:
            for candidate_method, pattern, handler in ROUTES:
                if candidate_method != method:
                    continue
                match = pattern.match(path)
                if match is None:
                    continue
                status, body = handler(request, match)
                break
            else:
                status = 404
                body = {"error": {"code": "not_found",
                                  "message": f"no route for {method} {path}"}}
        except HttpError as exc:
            status, body = exc.status, {"error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 - a bug must be a 500, not a hung connection
            correlation = _new_correlation_id()
            log.exception("unhandled exception (correlation %s)", correlation)
            status, body = 500, {"error": {"code": "internal_error",
                                           "message": GENERIC_500_MESSAGE,
                                           "correlation_id": correlation}}
        # Drain before answering so an error raised ahead of the body read cannot desync the next
        # request on this keep-alive connection.
        request.drain_body()
        self._respond(status, body)

    def _respond(self, status: int, body) -> None:
        if isinstance(body, Html):
            payload = body.body
            content_type = body.content_type
        elif status == 204 or body is None:
            payload = b""
            content_type = JSON_CONTENT_TYPE
        else:
            payload = json.dumps(body).encode("utf-8")
            content_type = JSON_CONTENT_TYPE
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def send_error(self, code, message=None, explain=None) -> None:
        """Answer the framework's own errors in the same envelope the application uses.

        Called by `BaseHTTPRequestHandler` for an unsupported verb, an unparseable request line, a
        request line past the length limit, too many headers, or an unsupported HTTP version. The
        base implementation emits an HTML page, which the spec forbids for every 4xx and 5xx.

        The structure mirrors the base method exactly -- `Connection: close`, the same no-body
        statuses, the same `HEAD` suppression -- so the only thing that changes is the body. Two
        deliberate choices: the wire gets `shortmsg`, the static reason phrase, so a client's own
        bytes are never reflected back at it; and `Connection: close` is preserved because on the
        unsupported-verb path the request body has not been read, so the stream cannot be reused.
        The full `message` still goes to `log_error` for the operator.
        """
        code = int(code)
        try:
            shortmsg, longmsg = self.responses[code]
        except KeyError:
            shortmsg, longmsg = "???", "???"
        if message is None:
            message = shortmsg
        if explain is None:
            explain = longmsg
        self.log_error("code %d, message %s", code, message)

        self.send_response(code, message)
        self.send_header("Connection", "close")

        # Same exclusions as the base method: RFC 7230 1xx/204/304 and RFC 7231 205 carry no body.
        body = None
        if code >= 200 and code not in (204, 205, 304):
            body = json.dumps({"error": {"code": _framework_error_code(code),
                                         "message": shortmsg}}).encode("utf-8")
            self.send_header("Content-Type", JSON_CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def log_message(self, *args) -> None:
        pass


def serve(port: int | None = None) -> None:
    """Run until interrupted. Binds 0.0.0.0 so a published port reaches it."""
    if port is None:
        port = int(os.environ.get("PORT", "8080"))
    store.ensure_schema()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"tablekeeper stage-1 listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()