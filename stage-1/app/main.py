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
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_POSITIVE_INT_RE = re.compile(r"^\d+$")
_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: §11:186 bounds a batch at 1-8 move objects. Zero is out of range too, which is why an empty
#: `moves` array is a validation error rather than a successful no-op.
_MAX_MOVES = 8

log = logging.getLogger(__name__)

# The only text a client ever sees for an unhandled exception, and it interpolates nothing. A
# `sqlite3` message names tables and columns ("UNIQUE constraint failed: tables.id"), so sending
# `str(exc)` to the wire publishes the schema to whoever happened to trigger the bug -- and the
# catch-all is reachable with no token at all, since an unauthenticated `POST /_test/reset` is
# enough. The detail goes to the server log instead; the correlation id is what lets an operator
# join the two.
GENERIC_500_MESSAGE = "an internal error occurred"

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


class HttpError(Exception):
    """An error with the status and `error.code` the spec assigns it."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _malformed(message: str) -> HttpError:
    return HttpError(400, "malformed_request", message)


def _invalid(message: str) -> HttpError:
    return HttpError(422, "validation_failed", message)


# ---- handlers -------------------------------------------------------------

def health(request, match):
    return 200, {"status": "ok"}


def reset(request, match):
    body = request.json_body()
    if not isinstance(body, dict):
        raise _malformed("fixture must be a JSON object")
    store.reset_database(body)
    return 204, None


def post_signup(request, match):
    user_id = "u_" + secrets.token_hex(6)
    try:
        created = auth.signup(request.json_body(), user_id)
    except auth.MalformedRequest as exc:
        raise _malformed(str(exc)) from exc
    except auth.ValidationFailure as exc:
        raise _invalid(str(exc)) from exc
    except auth.CredentialsTaken as exc:
        raise HttpError(409, "email_taken", str(exc)) from exc
    return 201, created


def post_login(request, match):
    try:
        return 200, auth.authenticate(request.json_body())
    except auth.MalformedRequest as exc:
        raise _malformed(str(exc)) from exc
    except auth.BadCredentials as exc:
        raise HttpError(401, "unauthenticated", str(exc)) from exc


def list_restaurants(request, match):
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, timezone, slot_minutes, reservation_duration_minutes,"
            " cancellation_cutoff_minutes FROM restaurants ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return 200, [_restaurant_body(conn_row, None, []) for conn_row in rows]


def get_restaurant(request, match):
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT id, name, timezone, slot_minutes, reservation_duration_minutes,"
            " cancellation_cutoff_minutes FROM restaurants WHERE id = ?",
            (match.group("id"),),
        ).fetchone()
        if row is None:
            raise HttpError(404, "not_found", "no such restaurant")
        tables = conn.execute(
            "SELECT id, label, capacity FROM tables WHERE restaurant_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        hours = conn.execute(
            "SELECT weekday, opens, closes FROM opening_hours WHERE restaurant_id = ?"
            " ORDER BY weekday", (row["id"],),
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
    }
    if tables is not None:
        body["tables"] = tables
    if hours is not None:
        body["opening_hours"] = hours
    return body


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

    conn = store.connect()
    try:
        restaurant = conn.execute(
            "SELECT * FROM restaurants WHERE id = ?", (params["restaurant_id"],)
        ).fetchone()
        if restaurant is None:
            raise HttpError(404, "not_found", "no such restaurant")
        tables = conn.execute(
            "SELECT id, capacity FROM tables WHERE restaurant_id = ? ORDER BY id",
            (restaurant["id"],),
        ).fetchall()
        hours = conn.execute(
            "SELECT opens, closes FROM opening_hours WHERE restaurant_id = ? AND weekday = ?",
            (restaurant["id"], WEEKDAYS[day.weekday()]),
        ).fetchall()
        booked = conn.execute(
            "SELECT table_id, starts_at_utc FROM reservations WHERE restaurant_id = ?"
            " AND status != 'cancelled'", (restaurant["id"],),
        ).fetchall()
    finally:
        conn.close()

    if not hours:
        # A weekday with no opening_hours entry is closed: an empty list, not a 404.
        return 200, {"restaurant_id": restaurant["id"], "date": params["date"],
                     "party_size": party_size, "slots": []}

    zone = ZoneInfo(restaurant["timezone"])
    duration = restaurant["reservation_duration_minutes"]
    step = restaurant["slot_minutes"]
    occupancy = [(row["table_id"], dt.datetime.fromisoformat(row["starts_at_utc"])) for row in booked]

    slots = []
    for window in hours:
        start_minutes = minutes_of(window["opens"])
        close_minutes = minutes_of(window["closes"])
        cursor = start_minutes
        while cursor + duration <= close_minutes:
            label = format_minutes(cursor)
            naive = parse_local(f"{day.isoformat()}T{label}")
            try:
                starts = resolve(naive, zone)
            except NonExistentLocalTime:
                # The wall time is skipped by a clock change; there is no such slot to offer.
                cursor += step
                continue
            ends = slot_end(starts, duration)

            free = []
            for table in tables:
                if table["capacity"] < party_size:
                    continue
                taken = any(
                    table_id == table["id"] and overlaps(starts, ends, other_start, other_end)
                    for table_id, other_start in occupancy
                    for other_end in [slot_end(other_start, duration)]
                )
                if not taken:
                    free.append(table["id"])

            # A slot with no free table still appears, carrying an empty list: the restaurant is
            # open and the grid exists, there is simply nothing left to seat at that moment.
            slots.append({
                "starts_at_local": f"{day.isoformat()}T{label}",
                "starts_at": format_instant(starts),
                "available_table_ids": free,
            })
            cursor += step

    return 200, {"restaurant_id": restaurant["id"], "date": params["date"],
                 "party_size": party_size, "slots": slots}


def list_reservations(request, match):
    request.require_user()
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE user_id = ? ORDER BY created_at, reference",
            (request.user["id"],),
        ).fetchall()
    finally:
        conn.close()
    return 200, [_reservation_body(row) for row in rows]


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
    for field in ("restaurant_id", "table_id", "starts_at_local"):
        if not isinstance(body.get(field), str):
            raise _malformed(f"{field} must be a string")
    if not isinstance(body.get("party_size"), int) or isinstance(body.get("party_size"), bool):
        raise _malformed("party_size must be an integer")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    for field in ("restaurant_id", "table_id", "starts_at_local"):
        if not isinstance(body.get(field), str):
            raise _malformed(f"{field} must be a string")
    if not isinstance(body.get("party_size"), int) or isinstance(body.get("party_size"), bool):
        raise _malformed("party_size must be an integer")

    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scope = request.idempotency_scope()

    # The replay check, the conflict check, the insert and the idempotency record all happen in
    # ONE transaction. That is what makes the §7 burst work: BEGIN IMMEDIATE takes the write lock
    # up front, so concurrent identical requests serialise and all but the first see the stored
    # response. Reading the key outside the transaction, as an earlier version did, let every
    # request in the burst find nothing, race past the conflict check and answer 409.
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


def _enforce_cutoff(row, restaurant, what):
    """409 `cutoff_passed` once `now` is inside the window before the CURRENT start.

    Shared by amendment (:140) and cancellation (:138), which state the same rule. Measured against
    the stored start and never the requested one: re-anchoring to the new start would let a caller
    inside the window walk the booking forward to a later slot and keep amending it, which is the
    thing the cutoff exists to prevent. §4:43 keeps a past start from being an error by itself --
    the window is the only test, and a booking already inside it simply cannot be touched.
    """
    cutoff_at = (dt.datetime.fromisoformat(row["starts_at_utc"])
                 - dt.timedelta(minutes=restaurant["cancellation_cutoff_minutes"]))
    if dt.datetime.now(dt.timezone.utc) >= cutoff_at:
        raise HttpError(409, "cutoff_passed", f"too close to the start to {what}")


def _validate_booking_fields(conn, restaurant, table_id, starts_at_local, party_size):
    """Every check on a requested booking except occupancy. Returns `(table, starts)`.

    Split out from `_assert_slot_free` because §11:197 requires a batch's *non-occupancy* errors to
    be settled before any occupancy error, which a single combined pass cannot express.
    """
    table = conn.execute(
        "SELECT * FROM tables WHERE id = ? AND restaurant_id = ?",
        (table_id, restaurant["id"]),
    ).fetchone()
    if table is None:
        raise HttpError(404, "not_found", "no such table at this restaurant")
    if party_size < 1:
        raise _invalid("party_size must be at least 1")
    if table["capacity"] < party_size:
        raise _invalid("table is too small for this party")

    try:
        naive = parse_local(starts_at_local)
    except InvalidLocalTime as exc:
        raise _invalid(str(exc)) from exc
    zone = ZoneInfo(restaurant["timezone"])
    try:
        starts = resolve(naive, zone)
    except NonExistentLocalTime as exc:
        raise _invalid(str(exc)) from exc
    return table, starts


def _assert_slot_free(conn, restaurant, table, starts, exclude_reference=None):
    """409 `table_unavailable` if the requested interval collides with a live booking.

    `exclude_reference` drops the reservation being amended from the check, which would otherwise
    collide with itself.

    The query is scoped to `restaurant_id` because `table_id` is only unique *within* a restaurant.
    Keying `tables` by `(restaurant_id, id)` is what makes two restaurants able to own a `t_2`
    each, and this is the query that has to follow from that: without the scope, a booking at one
    restaurant occupies an identically-numbered table at every other restaurant. The symptom is
    worse than a lost booking, because it contradicts the read path -- `GET /availability` filters
    by `restaurant_id`, so it *offers* `t_2`, and the booking then answers 409. An offered table
    that cannot be booked is the defect, and only one of the two paths was scoped.
    """
    ends = slot_end(starts, restaurant["reservation_duration_minutes"])
    clash = ("SELECT starts_at_utc FROM reservations"
             " WHERE table_id = ? AND restaurant_id = ? AND status != 'cancelled'")
    params = [table["id"], restaurant["id"]]
    if exclude_reference is not None:
        clash += " AND reference != ?"
        params.append(exclude_reference)
    for row in conn.execute(clash, params).fetchall():
        other_start = dt.datetime.fromisoformat(row["starts_at_utc"])
        if overlaps(starts, ends, other_start,
                    slot_end(other_start, restaurant["reservation_duration_minutes"])):
            raise HttpError(409, "table_unavailable", "that table is already booked")


def _resolve_booking(conn, restaurant, table_id, starts_at_local, party_size,
                     exclude_reference=None):
    """Validate a booking exactly as create does and return `(table, starts)`.

    Shared with `PATCH /reservations/{reference}`, which the spec requires to apply "same
    validation as create" -- so it calls this rather than keeping a second copy that has to be
    kept in agreement.
    """
    table, starts = _validate_booking_fields(conn, restaurant, table_id,
                                             starts_at_local, party_size)
    _assert_slot_free(conn, restaurant, table, starts, exclude_reference)
    return table, starts


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
    table, starts = _resolve_booking(conn, restaurant, body["table_id"],
                                      body["starts_at_local"], party_size)

    reference = _new_reference()
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO reservations (reference, restaurant_id, table_id, user_id,"
        " starts_at_utc, starts_at_local, party_size, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)",
        (reference, restaurant["id"], table["id"], user_id,
         starts.astimezone(dt.timezone.utc).isoformat(),
         body["starts_at_local"], party_size, created_at),
    )
    stored = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                          (reference,)).fetchone()
    return 201, _reservation_body(stored)


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
    for field in ("table_id", "starts_at_local"):
        if field in body and not isinstance(body[field], str):
            raise _malformed(f"{field} must be a string")
    if "party_size" in body and (not isinstance(body["party_size"], int)
                                 or isinstance(body["party_size"], bool)):
        raise _malformed("party_size must be an integer")

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

        restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?",
                                  (row["restaurant_id"],)).fetchone()
        _enforce_cutoff(row, restaurant, "amend")

        # Absent fields keep their stored value, which is what makes the subset literal.
        table_id = body.get("table_id", row["table_id"])
        starts_at_local = body.get("starts_at_local", row["starts_at_local"])
        party_size = body.get("party_size", row["party_size"])

        _table, starts = _resolve_booking(conn, restaurant, table_id, starts_at_local,
                                          party_size, exclude_reference=reference)

        # `reference` and `status` are deliberately absent from the SET list: the spec requires both
        # to survive an amendment.
        conn.execute(
            "UPDATE reservations SET table_id = ?, starts_at_local = ?, starts_at_utc = ?,"
            " party_size = ? WHERE reference = ?",
            (table_id, starts_at_local, starts.astimezone(dt.timezone.utc).isoformat(),
             party_size, reference),
        )
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
        _enforce_cutoff(row, restaurant, "cancel")

        # Only `status` moves. §10:175 keeps identities and timestamps from being regenerated, so
        # `reference`, `reservation_id` and `created_at` all survive a cancellation untouched.
        conn.execute("UPDATE reservations SET status = 'cancelled' WHERE reference = ?",
                     (reference,))
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
    2. then occupancy, also in input order, applying each item as it clears so the next item is
       checked against its predecessor's *destination*. That is what makes :199's "overlap among
       resulting bookings" fall out without a second copy of the conflict logic.

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
            _enforce_cutoff(row, restaurant, "amend")

            # Absent fields keep their stored value, exactly as an amendment's subset does (:192).
            table_id = item.get("table_id", row["table_id"])
            starts_at_local = item.get("starts_at_local", row["starts_at_local"])
            party_size = item.get("party_size", row["party_size"])

            # The item's own wrong-JSON-type rules, here rather than in `_validated_moves`, because
            # :197 makes the *batch's* earliest non-occupancy error the answer. `table_id` and
            # `starts_at_local` answer 400 `malformed_request` -- §5:48's generic rule, and the code
            # `patch_reservation` already gives the same two fields. `party_size` answers 422
            # because §5:57 says so in as many words; that row contradicts §5:48 and the
            # contradiction is an open named defect (`party_size_wrong_type_is_422`) that this
            # endpoint inherits rather than settles.
            #
            # Position matters as much as the code. Above the cutoff is §11:198's "cutoff errors
            # preceding other changes for that booking"; below it, because a booking already inside
            # its window cannot be amended whatever the amendment says.
            if not isinstance(table_id, str):
                raise _malformed("table_id must be a string")
            if not isinstance(starts_at_local, str):
                raise _malformed("starts_at_local must be a string")
            if not isinstance(party_size, int) or isinstance(party_size, bool):
                raise _invalid("party_size must be an integer")

            table, starts = _validate_booking_fields(conn, restaurant, table_id,
                                                     starts_at_local, party_size)
            planned.append((reference, table, starts, table_id, starts_at_local, party_size))

        # Pass two: occupancy, in input order, each item applied as it clears.
        results = []
        for reference, table, starts, table_id, starts_at_local, party_size in planned:
            _assert_slot_free(conn, restaurant, table, starts, exclude_reference=reference)
            # `reference`, `status`, `user_id` and `created_at` are deliberately absent from the SET
            # list: §11:194 keeps identity, owner and creation time unchanged through a move.
            conn.execute(
                "UPDATE reservations SET table_id = ?, starts_at_local = ?, starts_at_utc = ?,"
                " party_size = ? WHERE reference = ?",
                (table_id, starts_at_local,
                 starts.astimezone(dt.timezone.utc).isoformat(), party_size, reference),
            )
            moved = conn.execute("SELECT * FROM reservations WHERE reference = ?",
                                 (reference,)).fetchone()
            results.append(_reservation_body(moved))

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


def _reservation_body(row):
    return {
        "reference": row["reference"],
        "reservation_id": row["reference"],
        "restaurant_id": row["restaurant_id"],
        "table_id": row["table_id"],
        "user_id": row["user_id"],
        "starts_at_local": row["starts_at_local"],
        "starts_at": format_instant(dt.datetime.fromisoformat(row["starts_at_utc"])),
        "party_size": row["party_size"],
        "status": row["status"],
    }


ROUTES = [
    ("GET", re.compile(r"^/health$"), health),
    ("POST", re.compile(r"^/_test/reset$"), reset),
    ("POST", re.compile(r"^/auth/signup$"), post_signup),
    ("POST", re.compile(r"^/auth/login$"), post_login),
    ("GET", re.compile(r"^/restaurants$"), list_restaurants),
    ("GET", re.compile(r"^/restaurants/(?P<id>[^/]+)$"), get_restaurant),
    ("GET", re.compile(r"^/availability$"), get_availability),
    ("POST", re.compile(r"^/reservations$"), post_reservation),
    ("GET", re.compile(r"^/reservations$"), list_reservations),
    ("GET", re.compile(r"^/reservations/(?P<reference>[^/]+)$"), get_reservation),
    ("PATCH", re.compile(r"^/reservations/(?P<reference>[^/]+)$"), patch_reservation),
    ("POST", re.compile(r"^/reservation-moves$"), post_reservation_moves),
    ("POST", re.compile(r"^/reservations/(?P<reference>[^/]+)/cancel$"), cancel_reservation),
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

    def idempotency_scope(self) -> str:
        """§7:79 scopes a replay to "same user, same method, same path, same body".

        Moves is §7's second idempotency-required path, so the path has to be part of the lookup:
        without it a key spent on create would make the same body on moves look like a replay of
        the *other* endpoint, which §7:80 forbids in as many words.
        """
        return f"{self.handler.command} {self.path}"

    def raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.handler.rfile.read(length) if length else b""

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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tablekeeper/1.0"

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            for candidate_method, pattern, handler in ROUTES:
                if candidate_method != method:
                    continue
                match = pattern.match(path)
                if match is None:
                    continue
                request = Request(self)
                status, body = handler(request, match)
                self._respond(status, body)
                return
            self._respond(404, {"error": {"code": "not_found",
                                          "message": f"no route for {method} {path}"}})
        except HttpError as exc:
            self._respond(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # noqa: BLE001 - a bug must be a 500, not a hung connection
            correlation = _new_correlation_id()
            log.exception("unhandled exception (correlation %s)", correlation)
            self._respond(500, {"error": {"code": "internal_error",
                                          "message": GENERIC_500_MESSAGE,
                                          "correlation_id": correlation}})

    def _respond(self, status: int, body) -> None:
        if status == 204 or body is None:
            payload = b""
        else:
            payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
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
            self.send_header("Content-Type", "application/json")
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