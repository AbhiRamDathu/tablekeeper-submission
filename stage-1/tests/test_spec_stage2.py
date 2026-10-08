"""Stage 2 acceptance tests: the REQUIREMENTS.md rows the Stage 1 suite asserts by nothing.

The Stage 1 suite is 248 tests and the gate in `tests/test_spec_stage1.py` reports nine red names,
so the shape of the suite is not the problem. The problem is coverage: walking every row of
REQUIREMENTS.md against the shipped test names leaves whole rows with no assertion at all, and every
one below was verified against the running service rather than inferred from the source.

    SS3   `application/json; charset=utf-8` out   -- no test reads a Content-Type header anywhere
    SS5   integer query params are ASCII digits    -- `\\d` in Python matches Unicode digits
    SS8   `available_table_ids` "in fixture order" -- served by `ORDER BY id`
    SS8   `tables` "in the fixture's shape"        -- served by `ORDER BY id`
    SS10  export/import                            -- 18 rows, no route, no test

**Why these are named defects and not assertions bolted onto an existing name.** The gate fails the
entire run on any failing test whose name is not in `NAMED_DEFECTS`, so an unregistered red test is
not merely red -- it reports "the tree and the specification have diverged somewhere the named list
does not know about". Every red test here therefore has a name in this module's `NAMED_DEFECTS` *and*
in the gate's list in `tests/test_spec_stage1.py`, which stays the single place a reader looks to
learn what is known broken. `NamedDefectsHaveTests` closes the gate from the other side: a registered
name with no test would inflate the green count, which is the same counting-instead-of-naming failure
pointed the other way.

**Two of the six names are red for one cause.** `GET /_test/export` and `POST /_test/import` are both
absent from `app/main.py` ROUTES, so any test that exercises SS10 is red until both are written. That
is stated rather than hidden: the names are split because they are two separable pieces of work (the
routes, then the preservation semantics), not because they are two defects, and the count will move
by two when the feature lands.

Deliberately NOT asserted here, each with the reason on the record:

* `ends_at` / `created_at` in a reservation body. `_reservation_body` omits both, which is the named
  defect `reservation_body_has_ends_at_and_created_at`. SS10's preservation half needs `created_at` to
  prove identities "are not regenerated" (SS10:175), so this module compares `reference`,
  `reservation_id`, `starts_at`, `status` and `party_size` instead -- all of which are emitted today,
  so a red leg here is always about SS10 and never about that other defect.
* The order of `GET /restaurants`. `list_restaurants` uses `ORDER BY id`, so a fixture of
  `[zz_top, aa_top]` comes back `[aa_top, zz_top]`. SS8:99 specifies the envelope and the three fields
  and says nothing about order, unlike SS8:112 which says "in fixture order" in as many words.
  Declined, on the same reasoning as `starts_at_rendered_in_restaurant_zone` in the Stage 1 gate.
* `tests/test_moves.py`'s `@unittest.skip("GET /_test/export is 404; SS10 is unreachable until SS10
  lands")`. Left in place: un-skipping it would produce a failing test named
  `a_batch_receipt_survives_an_export_import_round_trip`, which is in neither named list, and the gate
  would fail the whole run on it. SS11:205 is covered here instead, under
  `import_is_replacement_and_preserves_receipts`.
"""
from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from tests.support import (
    ADA,
    BOB,
    Client,
    Response,
    assert_all,
    booking_date,
    fixture,
    local,
    restaurant,
    service,
    world,
)

#: The Stage 2 named defects. Mirrored into `tests/test_spec_stage1.py:NAMED_DEFECTS`, which is the
#: list the gate's subset assertion reads; see the module docstring for why the list is not defined
#: here and only here.
NAMED_DEFECTS = (
    "export_and_import_are_served",
    "import_is_replacement_and_preserves_receipts",
    "import_rejects_a_bad_envelope_without_changing_the_destination",
    "table_ids_are_returned_in_fixture_order",
    "json_responses_declare_utf8",
    "non_ascii_digits_are_not_decimal_digits",
)

#: Names a plan could propose and this gate declines to carry, with the reason on the record.
DECLINED = {
    "restaurants_are_listed_in_fixture_order": (
        "SS8:99 specifies the {restaurants:[...]} envelope and the three fields {id,name,timezone} "
        "and says nothing about order. SS8:112 says 'in fixture order' for available_table_ids in "
        "as many words, which is why that half is carried as a defect and this half is not: the "
        "difference is the specification's wording, not the amount of evidence."
    ),
}

#: SS10:163-164 -- the track name and format version an exported object must declare.
TRACK = "tablekeeper"
FORMAT_VERSION = 1

#: SS3:26 spells the charset out, so the assertion is on the whole header value rather than on the
#: media type. RFC 8259 defaults JSON to UTF-8 and SS3 does not accept the default.
EXPECTED_CONTENT_TYPE = "application/json; charset=utf-8"

#: A digit that is a digit to `re` and to `int()`, and is not an ASCII decimal digit. SS5:59 asks for
#: "plain decimal digits", so this is the whole reason the query-parameter rule is a rule.
ARABIC_INDIC_FOUR = "\u0664"
FULLWIDTH_FOUR = "\uff14"


def shuffled_tables():
    """Three tables whose ids sort differently from the order the fixture lists them in.

    SS8:112 requires `available_table_ids` "in fixture order". `zz_1, aa_1, mm_1` sorts as
    `aa_1, mm_1, zz_1`, so an implementation that orders by id returns a different list from the one
    the fixture asked for. `t_1, t_2, t_3` -- the default fixture -- is already sorted, which is why
    the shipped suite can be green and the requirement still unmet.
    """
    return [{"id": "zz_1", "label": "1", "capacity": 2},
            {"id": "aa_1", "label": "2", "capacity": 4},
            {"id": "mm_1", "label": "3", "capacity": 6}]


def reservation_rows(payload):
    """The reservations in a `GET /reservations` body, whichever envelope it arrived in.

    `list_reservations` returns a bare list today; SS8:133 requires `{"reservations":[...]}`, which is
    the named defect `list_reservations_envelope_and_desc_order`. Reading both shapes is what lets a
    test about SS10 fail for SS10's reason and not be reported against that other defect.
    """
    if isinstance(payload, dict):
        rows = payload.get("reservations")
    else:
        rows = payload
    return rows if isinstance(rows, list) else None


class _RawProbe:
    """Issues requests through `urllib` and keeps the response headers.

    `tests.support.Client` deliberately stores just `(status, body)`, which is the right call for
    every other test in the suite and the wrong one for SS3:26 -- no test anywhere reads a response
    header. Rather than widen `Client` and so change what the other 248 tests see, SS3's header is
    probed here on its own. Also the only way to send a body that is not valid JSON, which SS10:169
    needs.
    """

    def __init__(self, base_url):
        self.base_url = base_url

    def _send(self, method, path, data, headers):
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), exc.read()

    def get(self, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self._send("GET", path, None, headers)[:2]

    def post_json(self, path, body, token=None, key=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["Idempotency-Key"] = key
        status, content_type, raw = self._send("POST", path, json.dumps(body).encode("utf-8"), headers)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, content_type, payload

    def post_bytes(self, path, data, content_type="application/json"):
        """A `Response`, so `.code` reads the error code the same way every other test reads it."""
        status, _, raw = self._send("POST", path, data, {"Content-Type": content_type})
        return Response(status, raw)


class Stage2SpecGate(unittest.TestCase):
    """One test method per Stage 2 named defect, named exactly after the defect."""

    maxDiff = None

    # ---- SS10 ---------------------------------------------------------------------------------

    def test_export_and_import_are_served(self):
        """SS10:162-167 -- both routes exist, are unauthenticated, and round-trip the object.

        `GET /_test/export` answers 200 with `track`, `format_version` and a `state` object that is
        opaque to the caller; `POST /_test/import` takes that entire object unchanged and answers
        204. Today both are `404 {"error":{"code":"not_found"}}`, because ROUTES in `app/main.py`
        mentions neither.

        Only the first leg is unconditional. Reading the export body and handing it back to import is
        meaningless while the export itself is a 404, and `tests/test_spec_stage1.py:208-215` records
        why: a probe that keeps going after its own precondition failed reports a symptom of one
        defect as if it were a defect of its own. So the round-trip legs are gated on an export that
        actually happened, and the unguarded leg is the one that names the cause.
        """
        with service() as base_url:
            anon = Client(base_url, token=None)
            anon.post("/_test/reset", json_body=fixture())

            exported = anon.get("/_test/export")
            payload = exported.json if isinstance(exported.json, dict) else {}
            legs = [("GET /_test/export answers 200 with no token", 200, exported.status)]

            state = payload.get("state")
            if exported.status == 200:
                legs += [
                    (f"export declares track {TRACK!r}", TRACK, payload.get("track")),
                    (f"export declares format_version {FORMAT_VERSION}", FORMAT_VERSION,
                     payload.get("format_version")),
                    ("state is a JSON object the caller may treat as opaque", True,
                     isinstance(state, dict)),
                    ("POST /_test/import takes that object unchanged and answers 204", 204,
                     anon.post("/_test/import", json_body=payload).status),
                ]

            # SS10:167 -- import is replacement, and repeating it restores the same state without
            # duplicating anything. Expressed on the one resource both routes can be watched through:
            # the restaurant list, which a merged or duplicated import would grow.
            if exported.status == 200 and isinstance(state, dict):
                again = anon.post("/_test/import", json_body=payload)
                listed = Client(base_url).get("/restaurants").json
                rows = listed["restaurants"] if isinstance(listed, dict) else listed
                legs += [
                    ("a second identical import still answers 204", 204, again.status),
                    ("import is replacement, so a repeated import duplicates nothing", 1,
                     len(rows) if isinstance(rows, list) else -1),
                ]
            assert_all(self, legs)

    def test_import_is_replacement_and_preserves_receipts(self):
        """SS10:172-180 and SS11:205 -- what has to survive a round trip, and what must not.

        The row SS10:178 spells out the failure this exists to catch: "replacing state with a fresh
        fixture does not satisfy this". An import that simply re-seeds from the default fixture passes
        every status-code assertion in the section and loses every identity in it.

        So nothing here is judged by a status code alone. The bearer token minted *before* the export
        is presented *after* the import; a receipt is replayed *after* the booking has been cancelled;
        and the world is mutated *between* the two calls precisely so that a replacement
        implementation has something to be caught discarding.

        Red today for the same cause as `export_and_import_are_served`, and the legs are deliberately
        left ungated so this name reports red rather than passing on preconditions it never checked.
        """
        with world() as w:
            day = booking_date()
            booking = {"restaurant_id": w.rid, "table_id": "t_2",
                       "starts_at_local": local(day, "19:00"), "party_size": 4}
            created = w.ada.post("/reservations", json_body=booking,
                                 headers={"Idempotency-Key": "keep-this-booking"})
            body = created.json or {}
            reference = body.get("reference")

            # A key that was spent and failed, which SS10:176 requires to stay reusable afterwards.
            spent_then_failed = w.ada.post(
                "/reservations",
                json_body=dict(booking, table_id="t_nope", starts_at_local=local(day, "20:00")),
                headers={"Idempotency-Key": "spent-then-failed"})
            # SS11:205 -- a successful batch receipt, so the round trip has to carry it too.
            moved = None
            if reference:
                moved = w.ada.post("/reservation-moves",
                                   json_body={"moves": [{"reference": reference, "party_size": 3}]},
                                   headers={"Idempotency-Key": "batch-receipt"})

            exported = w.anon.get("/_test/export")
            payload = exported.json if isinstance(exported.json, dict) else {}
            snapshot = payload.get("state")

            # Mutate the world after the export, so a replacing import is observably wrong.
            if reference:
                w.ada.post(f"/reservations/{reference}/cancel")
            late = w.anon.request("POST", "/auth/signup", json_body={
                "email": "late@example.com", "password": "correct horse",
                "display_name": "Late"}, token=None)
            late_token = (late.json or {}).get("token")

            imported = w.anon.post("/_test/import", json_body=payload)

            legs = [
                ("the round trip is served at all", (200, 204),
                 (exported.status, imported.status)),
                ("the account the fixture seeded still logs in", 200,
                 w.anon.login(ADA["email"], ADA["password"]).status),
                ("the bearer token minted before the export still works", 200,
                 Client(w.base_url, w.ada.token).get("/reservations").status),
                ("SS10:179 the account created after the export is gone", 401,
                 Client(w.base_url, late_token).get("/reservations").status
                 if late_token else None),
            ]

            if exported.status == 200 and isinstance(snapshot, dict):
                rows = reservation_rows(Client(w.base_url, w.ada.token).get("/reservations").json)
                after = {row.get("reference"): row for row in rows} if rows is not None else {}
                mine = after.get(reference, {})
                legs += [
                    ("the booking is back after the round trip", True, reference in after),
                    ("SS10:175 identities are not regenerated", body.get("reservation_id"),
                     mine.get("reservation_id")),
                    ("the cancellation that happened after the export is undone by the import",
                     "confirmed", mine.get("status")),
                    # The party_size=3 move runs *before* the export, so the exported state carries
                    # 3 and the import restores a world whose parties are 3 -- "4" reversed the
                    # ordering of the move and the export.
                    ("the amendment that happened before the export survives the round trip", 3,
                     mine.get("party_size")),
                    ("starts_at is the instant the export carried", body.get("starts_at"),
                     mine.get("starts_at")),
                ]

                replay = Client(w.base_url, w.ada.token).post(
                    "/reservations", json_body=booking,
                    headers={"Idempotency-Key": "keep-this-booking"})
                legs.append(("a completed create replays 200 after the import", 200, replay.status))
                legs.append(("and returns the original response body unchanged", body, replay.json))

                reused = Client(w.base_url, w.ada.token).post(
                    "/reservations",
                    json_body=dict(booking, table_id="t_3", starts_at_local=local(day, "20:00")),
                    headers={"Idempotency-Key": "spent-then-failed"})
                legs.append(("SS10:176 a key that failed 4xx before the export is reusable after it",
                             (404, 201), (spent_then_failed.status, reused.status)))

                if moved is not None and moved.status == 201:
                    batch_body = {"moves": [{"reference": reference, "party_size": 3}]}
                    replayed = Client(w.base_url, w.ada.token).post(
                        "/reservation-moves", json_body=batch_body,
                        headers={"Idempotency-Key": "batch-receipt"})
                    legs.append(("SS11:205 a batch receipt replays 200 after the round trip",
                                 (200, moved.json), (replayed.status, replayed.json)))
                    # A fresh key for the same batch is a first use and books again -- which proves
                    # the replay above came out of a receipt and not out of the request being cheap.
                    second = Client(w.base_url, w.ada.token).post(
                        "/reservation-moves", json_body=batch_body,
                        headers={"Idempotency-Key": "batch-receipt-2"})
                    legs.append(("SS11:205 and the replayed batch took effect only once", 201,
                                 second.status))

                # SS10:180 -- reset still clears everything, imported state included.
                fresh = w.anon.post("/_test/reset", json_body=fixture(
                    users=[ADA], restaurants=[restaurant("r_other", name="Other")]))
                # The pre-export token is minted against the restaurant the export described; the
                # reset fixture seeds an entirely different restaurant, so the old token's user
                # still exists in Ada but its receipts are gone. 401 is the released-shape answer
                # (SS10:179), and a fresh login rebuilds a working client for the empty world.
                stale = Client(w.base_url, w.ada.token).get("/reservations")
                login_after = w.anon.login(ADA["email"], ADA["password"])
                after_reset = Client(w.base_url, login_after.json["token"]).get("/reservations")
                rows_after = reservation_rows(after_reset.json)
                legs += [
                    ("reset after an import answers 204", 204, fresh.status),
                    ("the pre-export token died with the reset", 401, stale.status),
                    ("and clears the imported bookings too", (200, 0),
                     (after_reset.status, len(rows_after) if rows_after is not None else -1)),
                    ("and clears the imported configuration too", 404,
                     Client(w.base_url).get("/restaurants/r_anker").status),
                ]
            assert_all(self, legs)

    def test_import_rejects_a_bad_envelope_without_changing_the_destination(self):
        """SS10:169-170 -- invalid JSON follows SS5; a bad envelope is 422 and changes nothing.

        "Without changing the destination" is the half that matters, so every case is followed by a
        read of the world rather than by the rejection status alone. An import that validates the
        envelope *after* it has already truncated the tables would answer 422 to all of these and
        still lose the fixture.
        """
        with world() as w:
            before = w.anon.get("/restaurants/r_anker").json
            probe = _RawProbe(w.base_url)
            exported = w.anon.get("/_test/export")
            payload = exported.json if isinstance(exported.json, dict) else {}

            cases = [
                ("a body that is not JSON at all is 400 malformed_request",
                 probe.post_bytes("/_test/import", b"{not json"), (400, "malformed_request")),
                ("a body that is not an object is 400 malformed_request",
                 probe.post_bytes("/_test/import", json.dumps(["not", "an", "object"]).encode()),
                 (400, "malformed_request")),
                ("a missing track is 422", w.anon.post(
                    "/_test/import", json_body={k: v for k, v in payload.items() if k != "track"}),
                 (422, "validation_failed")),
                ("a missing format_version is 422", w.anon.post(
                    "/_test/import",
                    json_body={k: v for k, v in payload.items() if k != "format_version"}),
                 (422, "validation_failed")),
                ("a missing state is 422", w.anon.post(
                    "/_test/import", json_body={k: v for k, v in payload.items() if k != "state"}),
                 (422, "validation_failed")),
                ("the wrong track is 422",
                 w.anon.post("/_test/import", json_body=dict(payload, track="somebody-else")),
                 (422, "validation_failed")),
                ("the wrong format_version is 422",
                 w.anon.post("/_test/import", json_body=dict(payload, format_version=2)),
                 (422, "validation_failed")),
                ("a state that is not an object is 422",
                 w.anon.post("/_test/import", json_body=dict(payload, state="a string")),
                 (422, "validation_failed")),
            ]

            legs = [("the good object is accepted at all", 204,
                     w.anon.post("/_test/import", json_body=payload).status)]
            for label, resp, want in cases:
                legs.append((label, want, (resp.status, resp.code)))
                legs.append((f"after {label}, the destination is untouched", before,
                             w.anon.get("/restaurants/r_anker").json))

            # SS10:167 from the other side: a rejected import must not have consumed the object, so
            # the same good state still imports afterwards.
            legs.append(("the good object is still importable after all those rejections", 204,
                         w.anon.post("/_test/import", json_body=payload).status))
            legs.append(("and it carries the same state it did the first time", payload.get("state"),
                         (w.anon.get("/_test/export").json or {}).get("state")))
            assert_all(self, legs)

    # ---- SS8 -----------------------------------------------------------------------------------

    def test_table_ids_are_returned_in_fixture_order(self):
        """SS8:112 -- `available_table_ids` lists tables "in fixture order". SS8:101 -- `tables`.

        `get_availability` and `get_restaurant` both read `... WHERE restaurant_id = ? ORDER BY id`,
        so the list is alphabetical by construction. The default fixture's `t_1, t_2, t_3` is already
        sorted, which is why `test_service.py` can assert which tables fit a party of four and be
        right about the membership while saying nothing at all about the order.

        The `tables` half is the weaker of the two and is carried for consistency rather than on the
        strength of its own wording: SS8:101 says "in the fixture's shape", and this repository has
        already read that phrase as fixture *order* for `opening_hours`, where the named defect
        `opening_hours_in_fixture_order` lives. Two halves read the same way are tested the same way,
        and when `opening_hours` is fixed by dropping its `ORDER BY`, this name is what stops the same
        mistake surviving in the other two places.
        """
        with service() as base_url:
            anon = Client(base_url, token=None)
            reset = anon.post("/_test/reset", json_body=fixture(
                restaurants=[restaurant("r_anker", tables=shuffled_tables())]))
            legs = [("the shuffled fixture is accepted", 204, reset.status)]

            at_open = self._availability(base_url, "r_anker", "2026-06-01", 1).get(
                "2026-06-01T18:00")
            legs.append(("available_table_ids follow the fixture's order",
                         ["zz_1", "aa_1", "mm_1"], (at_open or {}).get("available_table_ids")))

            detail = Client(base_url).get("/restaurants/r_anker").json or {}
            legs.append(("tables follow the fixture's order", ["zz_1", "aa_1", "mm_1"],
                         [row.get("id") for row in detail.get("tables", [])]))

            # The order has to survive an occupancy change too: taking the middle table out must not
            # re-sort the ones that remain, which is the only way an ORDER BY can look right on a
            # fixture where nothing is taken.
            anon.authenticate(ADA["email"], ADA["password"]).post("/reservations", json_body={
                "restaurant_id": "r_anker", "table_id": "aa_1",
                "starts_at_local": "2026-06-01T18:00", "party_size": 2},
                headers={"Idempotency-Key": "shuffle-1"})
            at_open = self._availability(base_url, "r_anker", "2026-06-01", 1).get(
                "2026-06-01T18:00")
            legs.append(("and keep the fixture's order once the middle table is taken",
                         ["zz_1", "mm_1"], (at_open or {}).get("available_table_ids")))
            assert_all(self, legs)

    @staticmethod
    def _availability(base_url, restaurant_id, date, party_size):
        """`{starts_at_local: slot}` for one availability query, so a leg can name a single slot."""
        body = Client(base_url).get("/availability", params={
            "restaurant_id": restaurant_id, "date": date, "party_size": party_size}).json or {}
        return {row.get("starts_at_local"): row for row in body.get("slots", [])}

    # ---- SS3 and SS5 ---------------------------------------------------------------------------

    def test_json_responses_declare_utf8(self):
        """SS3:26 -- `application/json; charset=utf-8` out, on every response that carries a body.

        `_respond` and `send_error` are two separate methods and both sent the bare media type, so
        every status the service can produce answered `Content-Type: application/json` with no
        charset parameter. Nothing in the suite reads a response header -- `tests.support.Client`
        keeps `(status, body)` and no test reaches past it -- so a header that was wrong on the 200
        path and the 500 path and the framework's own 501 alike was invisible to all 248 tests.

        The cases are spread across the three response paths on purpose. `send_error` answers the
        framework's own errors and `_respond` answers everything else, so a fix applied to one and not
        the other leaves the 501 wrong, and naming a header is a claim that it is not a per-handler
        decision.
        """
        with world() as w:
            probe = _RawProbe(w.base_url)
            booking = {"restaurant_id": w.rid, "table_id": "t_2",
                       "starts_at_local": local(booking_date(), "19:00"), "party_size": 4}
            cases = [
                ("GET /health, the _respond path", probe.get("/health", token=w.ada.token)),
                ("GET /restaurants, a 200 with a list body", probe.get("/restaurants")),
                ("GET /availability, a 200 SS8 names explicitly", probe.get(
                    f"/availability?restaurant_id={w.rid}&date={booking_date()}&party_size=2")),
                ("GET /restaurants/{id}", probe.get(f"/restaurants/{w.rid}")),
                ("GET /nope, the 404 path", probe.get("/nope", token=w.ada.token)),
                ("GET /reservations with a fabricated token, a 401",
                 probe.get("/reservations", token="not-a-real-token")),
                ("POST /auth/login with a wrong password, a 401", probe.post_json(
                    "/auth/login", {"email": ADA["email"], "password": "wrong password"})[:2]),
                ("POST /reservations with no Idempotency-Key, a 400",
                 probe.post_json("/reservations", booking, token=w.ada.token)[:2]),
                ("POST /reservations, a 201",
                 probe.post_json("/reservations", booking, token=w.ada.token, key="charset-201")[:2]),
            ]
            legs = [(f"{label} ({status}) declares charset=utf-8", EXPECTED_CONTENT_TYPE, content_type)
                    for label, (status, content_type) in cases]

            # SS3:26 says "in and out". The request side is satisfied by being served at all: this
            # client declares exactly the spec's content type, so a server that insisted on a bare
            # `application/json` would answer 415 here.
            served = probe.post_json("/reservations", dict(
                booking, table_id="t_3", starts_at_local=local(booking_date(), "20:00")),
                token=w.ada.token, key="charset-in")
            legs.append(("a request declaring application/json; charset=utf-8 is served normally",
                         201, served[0]))
            assert_all(self, legs)

    def test_non_ascii_digits_are_not_decimal_digits(self):
        """SS5:59 -- an integer query parameter is "plain decimal digits".

        `_POSITIVE_INT_RE` is `^\\d+$`, and in Python `\\d` matches every Unicode decimal digit, not
        only `0`-`9`. `int()` accepts them too, so `party_size=%D9%A4` is answered 200 and seats a
        party of four. "Plain decimal digits" means ASCII, and a server that accepts any other
        numeral is accepting a value the specification does not define.

        `RegressionGuards.test_integer_query_params_are_plain_decimal_digits` covers the ASCII half of
        the same rule -- `04` and `4` stay 200, `4.0` and `+4` are 422 -- and this name covers only
        what is currently red, so a fix has one leg to move.
        """
        with world() as w:
            legs = []
            for value in (ARABIC_INDIC_FOUR, FULLWIDTH_FOUR):
                resp = w.anon.get("/availability", params={
                    "restaurant_id": w.rid, "date": booking_date(), "party_size": value})
                legs.append((f"party_size={value!r} is not an ASCII decimal digit, so 422",
                             (422, "validation_failed"), (resp.status, resp.code)))
            assert_all(self, legs)


class RegressionGuards(unittest.TestCase):
    """Behaviour that is correct today, asserted by nothing, and cheap to lose.

    Each of these was probed against the running service before being written down, and each passes.
    They are here because they are the ones a later change would break silently. The non-ASCII half
    of the query-parameter rule is not here -- it is red, so it is a named defect
    (`non_ascii_digits_are_not_decimal_digits`) rather than a guard.

    * SS5:59 -- integer *query* parameters are plain decimal digits. `tests/test_service.py:275`
      probes `1e9` only. `4.0`, `+4` and the two whitespace variants are the rest of the rule, and
      `04` is the case that must stay 200: it *is* plain decimal digits, and a stricter regex written
      to fix the defect next door would break a caller the specification permits.
    * SS4:43 -- a booking is not rejected solely for starting in the past. Every other test in the
      suite books into the future, so nothing asserts this at all, and a "reject past bookings" guard
      is the obvious thing to add by accident. The second half of the row -- "cutoff rules still
      apply" -- is asserted alongside it, because a booking in the past is inside its own window.
    * SS6:72 -- tokens do not expire and an account may hold several at once. Only the single-token
      case is covered, and a unique constraint on `tokens.user_id` added later would break every
      second session without breaking any existing test.
    * REQUIREMENTS.md:17 -- no 5xx under adversarial input, as opposed to the concurrency bound the
      suite already owns. The `POST /_test/reset` 500 lived in exactly that gap.
    """

    def test_integer_query_params_are_plain_decimal_digits(self):
        with world() as w:
            legs = []
            for value in ("4", "04", "1", "10", "007"):
                resp = w.anon.get("/availability", params={
                    "restaurant_id": w.rid, "date": booking_date(), "party_size": value})
                legs.append((f"party_size={value!r} is plain ASCII decimal, so 200",
                             (200, None), (resp.status, resp.code)))
            for value in ("4.0", "+4", "1e9", " 4", "4 ", "0x4", "", "-1", "4,000", "4 0"):
                resp = w.anon.get("/availability", params={
                    "restaurant_id": w.rid, "date": booking_date(), "party_size": value})
                legs.append((f"party_size={value!r} is not, so 422",
                             (422, "validation_failed"), (resp.status, resp.code)))
            assert_all(self, legs)

    def test_a_booking_in_the_past_is_not_rejected_for_being_in_the_past(self):
        with world() as w:
            past = w.ada.post("/reservations", json_body={
                "restaurant_id": w.rid, "table_id": "t_2",
                "starts_at_local": "2020-01-01T19:00", "party_size": 4},
                headers={"Idempotency-Key": "long-past"})
            reference = (past.json or {}).get("reference")
            legs = [("a booking that started years ago is still accepted", 201, past.status)]
            if reference:
                # SS4:43's "cutoff rules still apply": a 120-minute cutoff is measured against the
                # start, so this booking sits inside its own window and cancelling it is refused.
                cancelled = w.ada.post(f"/reservations/{reference}/cancel")
                legs.append(("SS4:43 but the cutoff still refuses it", (409, "cutoff_passed"),
                             (cancelled.status, cancelled.code)))
            assert_all(self, legs)

    def test_no_adversarial_input_produces_a_5xx(self):
        """REQUIREMENTS.md:17 -- "No 5xx responses, including under concurrent load".

        The suite's only 5xx bound is `test_fifty_concurrent_requests_produce_no_5xx`, which is about
        *load*. Nothing bounded 5xx under *adversarial input*, and that is where the defect lived:
        `POST /_test/reset` answered 500 `internal_error` for every parseable-but-invalid fixture,
        because `store.InvalidFixture` was raised and never caught on the way out. A 24-case sweep of
        malformed bodies, hostile paths and hostile headers found six 5xx there, all one cause.

        The matrix below is the shape of input a caller can produce on purpose, across every route
        that parses a body. It passes today -- measured, 0 across the wider sweep this was cut from --
        and it is here because it is the test that would have caught that bug, and because a fix that
        removes the `try` around the catch-all would put it straight back.

        Bounded on purpose: this is a guard, not an exhaustive fuzzer. `test_fifty_concurrent_...`
        already spends minutes on load, and a 5xx sweep that takes longer than the rest of the suite
        gets skipped in practice, which is worse than not having it.
        """
        bodies = [None, [], "a string", 17, True, {},
                  {"id": None}, {"tables": "not-an-array"},
                  {"restaurants": [{"id": "r", "timezone": 5}]},
                  {"moves": {"not": "an array"}},
                  {"email": {"nested": 1}}, {"starts_at_local": {"nested": 1}},
                  {"opens": "99:99"}, {"weekday": 7}, {"capacity": "big"}, {"users": [None]}]

        offenders = []
        with world() as w:
            day = booking_date()
            for index, body in enumerate(bodies):
                for method, path, token in [
                        ("POST", "/_test/reset", None),
                        ("POST", "/auth/signup", None),
                        ("POST", "/auth/login", None),
                        ("POST", "/reservations", w.ada.token),
                        ("POST", "/reservation-moves", w.ada.token),
                        ("PATCH", "/reservations/ABCDEF", w.ada.token)]:
                    resp = w.anon.request(method, path, json_body=body, token=token,
                                          headers={"Idempotency-Key": f"sweep-{index}-{method}"})
                    if resp.status >= 500:
                        offenders.append((f"{method} {path} with body {body!r}", resp.status))

            for path in ["/nope", "/restaurants/%00", "/reservations/%2e%2e%2f",
                         "/availability?date=%00", "/reservations/" + "R" * 300]:
                resp = w.anon.get(path)
                if resp.status >= 500:
                    offenders.append((f"GET {path}", resp.status))

            booking = {"restaurant_id": w.rid, "table_id": "t_2",
                       "starts_at_local": local(day, "19:00"), "party_size": 4}
            for label, headers in [("a 5000-character Idempotency-Key", {"Idempotency-Key": "x" * 5000}),
                                   ("a 5000-character bearer token",
                                    {"Authorization": "Bearer " + "y" * 5000}),
                                   ("a Basic Authorization header", {"Authorization": "Basic abc"}),
                                   ("a bearer header with no value", {"Authorization": "Bearer"}),
                                   ("a non-JSON Content-Type", {"Content-Type": "text/plain"})]:
                resp = w.ada.request("POST", "/reservations", json_body=dict(
                    booking, table_id="t_3", starts_at_local=local(day, "20:00")), headers=headers)
                if resp.status >= 500:
                    offenders.append((label, resp.status))
            assert_all(self, [(f"REQUIREMENTS.md:17 no 5xx from: {label}", None, status)
                              for label, status in offenders])

    def test_an_account_may_hold_several_live_tokens(self):
        with world() as w:
            second = w.anon.login(ADA["email"], ADA["password"]).json["token"]
            legs = [
                ("logging in twice issues two different tokens", True, second != w.ada.token),
                ("the first token still authenticates", 200,
                 Client(w.base_url, w.ada.token).get("/reservations").status),
                ("the second token authenticates too", 200,
                 Client(w.base_url, second).get("/reservations").status),
                ("SS6:72 tokens do not expire -- a later login retires neither", (200, 200),
                 (Client(w.base_url, w.ada.token).get("/reservations").status,
                  Client(w.base_url, second).get("/reservations").status)),
                ("a fabricated token is still refused", 401,
                 Client(w.base_url, "not-a-real-token").get("/reservations").status),
                ("and Bob's token still only sees Bob", 200,
                 w.bob.get("/reservations").status),
            ]
            assert_all(self, legs)


class NamedDefectsHaveTests(unittest.TestCase):
    """Every registered name has a test, in either gate module.

    The subset assertion in `tests/test_spec_stage1.py` is one-sided by construction: it fails the run
    when a failing test is *not* named, and says nothing when a name has no test at all. A name with
    no test is counted green forever, so a defect could be registered, its test deleted, and the green
    count would not move -- the exact failure the gate's own docstring says it exists to prevent,
    reached from the other direction.

    This is cheap, and it is the only thing standing between a growing named list and a list whose
    length has stopped meaning anything. It reads both modules by reflection rather than by import
    order, so it does not care which of the two unittest discovers first.
    """

    def test_every_named_defect_has_a_test_method(self):
        from tests import test_spec_stage1, test_spec_stage2

        methods = []
        for module in (test_spec_stage1, test_spec_stage2):
            for obj in vars(module).values():
                if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
                    methods.append({name for name in vars(obj) if name.startswith("test_")})

        # The gate's own list is the one that must hold no repeats: a name listed twice is counted
        # twice, so the green tally stops being a count of distinct defects. `test_spec_stage2`'s
        # tuple is a deliberate mirror of the tail of that list, so it is checked for agreement
        # below rather than for uniqueness here.
        #
        # A defect name carries no `test_` prefix and a method name does, so the comparison adds the
        # prefix back rather than stripping it off the methods -- the same direction
        # `test_spec_stage1._defect_name_of` strips it.
        registered = list(test_spec_stage1.NAMED_DEFECTS)
        duplicate = sorted({name for name in registered if registered.count(name) > 1})
        missing = sorted(name for name in set(registered)
                         if not any(f"test_{name}" in group for group in methods))
        self.assertEqual(duplicate, [], "a name is registered twice, so it cannot move one-for-one: "
                        + ", ".join(duplicate))
        self.assertEqual(missing, [], "these names are registered but no test asserts them, so the "
                        "green count includes them forever: " + ", ".join(missing))

    def test_the_stage2_names_are_registered_with_the_shared_gate(self):
        """Stage 2's names must be in `test_spec_stage1.NAMED_DEFECTS`, not only in its own list.

        A red test in this module that is missing from the shared list is an *unnamed* failure, and
        the gate fails the entire run on it with "the tree and the specification have diverged". That
        is a true statement about the wrong subject: the defect was named, just in the wrong list.
        """
        from tests import test_spec_stage1, test_spec_stage2

        unregistered = [name for name in test_spec_stage2.NAMED_DEFECTS
                        if name not in test_spec_stage1.NAMED_DEFECTS]
        self.assertEqual(unregistered, [], "named here but not in the gate's list: "
                        + ", ".join(unregistered))


if __name__ == "__main__":
    unittest.main()