"""Unit 0: the spec gate that can fail.

The shipped suite is green at 70 tests while five whole areas of the specification are asserted by
nothing at all: `GET /restaurants`' envelope, PATCH and DELETE, the whole reservation-moves
contract, export/import, multi-restaurant fixtures, and the content of every error body. Across
roughly half the spec surface, "green" is not a weaker signal than a named-defect gate — it is no
signal. This module is that signal.

**Two different instruments live here, and they are not the same thing** (plan rev. 3.16):

* **The gate** — `_GateResult` below — decides, on every run, with no human present: *is the set of
  failing tests a subset of the named defects?* An **unnamed** failure fails the gate, because it
  means the tree and the specification have diverged somewhere the named list does not know about,
  and the run must stop being believed until someone explains it.
* **Count monotonicity is NOT implemented here.** That is a procedure a human runs by hand: fix
  defect *k*, observe *k*'s name gone and every other name still present; revert, observe it back.
  A runner that recomputed an expected failure count could not tell a corrected shipped test from
  a dropped defect, which is the entire reason the count was retired. It is reported separately and
  run by hand.

**Named defects: one failing test each**, test method named after the defect. This module touches no
app file; the two tallies the shipped suite already owns (`REFERENCED_SHIPPED_TESTS`) are referenced,
never cloned.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import unittest
from unittest.runner import TextTestResult
from zoneinfo import ZoneInfo

from app.intervals import slot_end
from app.tz import parse_local, resolve
from tests.support import (
    ADA,
    BOB,
    REFERENCED_SHIPPED_TESTS,
    Client,
    assert_all,
    booking_date,
    fixture,
    local,
    opening_hours_on,
    restaurant,
    seeded_reservation,
    service,
    spec_seeded_reservation,
    two_restaurants,
)

UTC = dt.timezone.utc
DURATION = 90

# Europe/Berlin 2026-03-29 02:00->03:00, Europe/Berlin 2026-10-25 03:00->02:00, and the two named
# America/New_York transitions. Every one of these is from REQUIREMENTS.md §9.
SPRING_FORWARD = ("2026-03-29", "2026-03-08")
FALL_BACK = ("2026-10-25", "2026-11-01")


# ---- the named list -----------------------------------------------------------------------

NAMED_DEFECTS = (
    "dockerignore_excludes_copied_source",
    "reset_two_restaurants_sharing_table_ids",
    "reset_spec_shaped_seeded_reservation",
    "reservation_body_has_ends_at_and_created_at",
    "list_reservations_envelope_and_desc_order",
    "party_size_wrong_type_is_422",
    "unknown_table_is_404",
    "slot_grid_and_opening_hours_codes",
    "skipped_local_time_is_invalid_local_time",
    "party_exceeds_capacity",
    "key_resolution_outranks_field_validation_on_create",
    "reset_rejects_invalid_fixture",
    "reset_rejects_invalid_fixture_references",
    "reset_rejects_fixtures_that_would_5xx",
    "auth_returns_display_name",
    "idempotency_key_scoped_by_path",
    "restaurants_list_envelope",
    "occupancy_scoped_by_restaurant",
    "opening_hours_in_fixture_order",
    "slot_end_is_absolute_across_transitions",
    "starts_at_rendered_in_restaurant_zone",
    "internal_error_message_is_redacted",
    # Stage 2. The names live here, next to the gate that reads them, and their test methods live in
    # `tests/test_spec_stage2.py` -- a second tuple that could drift is a second source of truth, and
    # `test_spec_stage2.NamedDefectsHaveTests` fails the run if the two ever disagree. SS10's two
    # names are red for one cause: both routes are absent, so the count moves by two when they land.
    "export_and_import_are_served",
    "import_is_replacement_and_preserves_receipts",
    "import_rejects_a_bad_envelope_without_changing_the_destination",
    "table_ids_are_returned_in_fixture_order",
    "json_responses_declare_utf8",
    "non_ascii_digits_are_not_decimal_digits",
)

# A name the gate cannot honestly call red, because the thing that would fail first is not the
# thing the name is about. Reported as not-yet-provable, never as an ordinary red entry.
#
# This dict declares what *may* be blocked and on what. It does not decide what *is* blocked --
# `_report` reads the outcome of the run for that, and only a name that actually skipped is
# reported here. The distinction matters, because a static report is wrong in both directions
# without ever failing: it kept listing `occupancy_scoped_by_restaurant` as unprovable after its
# blocker went green, and then again after the name itself went green, so a passing name was
# permanently excluded from the green list and a real fix stayed invisible. `_report` now derives
# the blocked set from `result.skipped`, which is the only evidence that a cause went unchecked.
BLOCKED = {
    "occupancy_scoped_by_restaurant": "reset_two_restaurants_sharing_table_ids",
}

# Names the plan proposed and this gate declines to carry, with the reason on the record.
DECLINED = {}


class _Reset:
    """`with _Reset(restaurants) as client:` — service up, fixture loaded, Ada authenticated."""

    def __init__(self, restaurants):
        self.restaurants = restaurants
        self._cm = None
        self.client = None

    def __enter__(self):
        self._cm = service()
        base_url = self._cm.__enter__()
        anon = Client(base_url, token=None)
        reset = anon.post("/_test/reset", json_body=fixture(restaurants=self.restaurants))
        if reset.status != 204:
            raise AssertionError(f"reset failed: {reset!r}")
        self.client = anon.authenticate(ADA["email"], ADA["password"])
        return self.client

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def reset_with(restaurants):
    return _Reset(restaurants)


def reset_shared_table_ids_is_fixed() -> bool:
    """True once `tables` no longer treats `id` as globally unique.

    `store.SCHEMA` declares `tables.id` a PRIMARY KEY on its own, so two restaurants cannot both
    own `t_1..t_3` — and any test about occupancy across two restaurants has no world to run in.
    Detected from the shipped schema rather than from a version number, so the gate stops skipping
    the moment the blocker is actually gone.
    """
    from app import store

    schema = re.sub(r"\s+", " ", store.SCHEMA).upper()
    tables_at = schema.index("CREATE TABLE IF NOT EXISTS TABLES")
    declaration = schema[tables_at:schema.index(";", tables_at)]
    return "PRIMARY KEY (RESTAURANT_ID, ID)" in declaration


# ---- the named defects, one test each ---------------------------------------------------

class SpecGate(unittest.TestCase):
    """One test method per named defect, named exactly after it."""

    maxDiff = None

    def test_dockerignore_excludes_copied_source(self):
        """`COPY app ./app` fails to build if the build context excludes `app/`.

        Green as of Unit 1a (commit be7759f). Kept in the gate because it is a defect that can
        silently return: `.dockerignore` is not covered by any shipped test, and the build only
        fails once someone runs Docker.
        """
        root = pathlib.Path(__file__).resolve().parent.parent
        patterns = {line.strip() for line
                    in (root / ".dockerignore").read_text().splitlines() if line.strip()}
        copies = re.findall(r"^COPY\s+(\S+)", (root / "Dockerfile").read_text(), re.M)
        legs = [(f"{source}/ excluded by .dockerignore", False, f"{source}/" in patterns)
                for source in copies]
        legs.append(("Dockerfile still copies the app source", True, "app" in copies))
        assert_all(self, legs)

    def test_reset_two_restaurants_sharing_table_ids(self):
        """§4 constrains a table id by nothing, so two restaurants may both own `t_1..t_3`.

        `store.py` declares `tables.id` a global PRIMARY KEY, so this fixture dies at reset with a
        500 — which also blocks every test that needs a two-restaurant world.
        """
        with service() as base_url:
            anon = Client(base_url, token=None)
            shared = two_restaurants(shared_table_ids=True)
            same_ids = [t["id"] for t in shared[0]["tables"]] == \
                       [t["id"] for t in shared[1]["tables"]]
            legs = [
                ("fixture really does share table ids", True, same_ids),
                ("reset status", 204,
                 anon.post("/_test/reset", json_body=fixture(restaurants=shared)).status),
            ]
            assert_all(self, legs)

    def test_reset_spec_shaped_seeded_reservation(self):
        """§4: a fixture reservation is "same fields as a create body plus id, reference, user_id".

        There is no `starts_at_utc` in that shape and §4 never mentions one, so a fixture author
        following the specification cannot supply it.
        """
        seed = spec_seeded_reservation()
        legs = [("seed omits starts_at_utc, as §4 prescribes", False, "starts_at_utc" in seed)]
        with service() as base_url:
            anon = Client(base_url, token=None)
            reset = anon.post("/_test/reset",
                              json_body=fixture(reservations=[spec_seeded_reservation()]))
            legs.append(("reset status", 204, reset.status))
            # Only inspect the seeded world once the reset has succeeded. Today it 500s, so the
            # users are never seeded and a login here would fail with 401 — reporting a symptom
            # of the defect as if it were a defect of its own, and never reaching the assertion
            # this name is about.
            if reset.status == 204:
                listed = anon.authenticate(ADA["email"], ADA["password"]).get("/reservations")
                body = listed.json
                count = len(body) if isinstance(body, list) else len(body.get("reservations", []))
                legs.append(("seeded reservation is visible", 1, count))
            assert_all(self, legs)

    def test_reservation_body_has_ends_at_and_created_at(self):
        """§8 lists `ends_at` and `created_at` in the 201 body; neither is emitted.

        §9's worked example about `ends_at` is asserted here rather than in
        `slot_end_is_absolute_across_transitions`, and that placement is deliberate. `ends_at` is
        this defect's field; the duration arithmetic that decides its value is the other one's. If
        both were asserted under one name, neither defect could be fixed on its own and the gate's
        count would stop moving one-for-one — which is the property the soundness check measures.
        """
        legs = []
        with reset_with([restaurant("r_anker")]) as client:
            body = {"restaurant_id": "r_anker", "table_id": "t_2",
                    "starts_at_local": "2026-06-01T19:00", "party_size": 4}
            resp = client.post("/reservations", json_body=body,
                               headers={"Idempotency-Key": "body-shape"})
            payload = resp.json or {}
            legs += [
                ("201 status", 201, resp.status),
                ("body has ends_at", True, "ends_at" in payload),
                ("body has created_at", True, "created_at" in payload),
            ]
            if "created_at" in payload:
                legs.append(("created_at is RFC 3339 with an offset", True,
                             re.search(r"[+-]\d{2}:\d{2}$|Z$", payload["created_at"]) is not None))

        # §9: 90 absolute minutes from 01:30 on a fall-back night ends at local 02:00, not 03:00.
        fall = FALL_BACK[0]
        with reset_with([restaurant("r_anker", timezone="Europe/Berlin",
                                    opening_hours=opening_hours_on(fall, "00:00", "07:00"))]) as client:
            ends = client.post("/reservations", json_body={
                "restaurant_id": "r_anker", "table_id": "t_2",
                "starts_at_local": f"{fall}T01:30", "party_size": 4},
                headers={"Idempotency-Key": "ends-at"}).json or {}
            if "ends_at" in ends:
                legs.append((f"{fall} 01:30 + 90 absolute minutes ends at local 02:00, not 03:00",
                             f"{fall}T02:00", ends["ends_at"][:16]))
        assert_all(self, legs)

    def test_starts_at_rendered_in_restaurant_zone(self):
        """§8:297 and §8:338-339 -- `starts_at` carries the restaurant's offset, not UTC.

        The worked examples render a local 19:00 in September Berlin as "19:00:00+02:00": the wall
        time with the zone's offset, which is the same instant a UTC rendering would have called
        "17:00:00+00:00". Both seasons are asserted so a hard-coded summer offset cannot pass.
        """
        cases = (("2026-06-01", "+02:00"), ("2026-01-15", "+01:00"))
        legs = []
        for date, expected in cases:
            with reset_with([restaurant("r_anker", timezone="Europe/Berlin")]) as client:
                created = client.post("/reservations", json_body={
                    "restaurant_id": "r_anker", "table_id": "t_2",
                    "starts_at_local": f"{date}T19:00", "party_size": 2},
                    headers={"Idempotency-Key": f"zone-{date}"}).json or {}
                if "starts_at" in created:
                    legs.append((f"{date} create renders the restaurant offset",
                                 expected, created["starts_at"][-6:]))
                listed = client.get("/reservations").json or {}
                rows = listed.get("reservations", []) if isinstance(listed, dict) else []
                mine = next((r for r in rows if r.get("starts_at")), None)
                if mine:
                    legs.append((f"{date} list renders the restaurant offset",
                                 expected, mine["starts_at"][-6:]))
        assert_all(self, legs)

    def test_list_reservations_envelope_and_desc_order(self):
        """§8: `{"reservations":[...]}`, `starts_at` descending, confirmed and cancelled alike."""
        seeds = [
            seeded_reservation("AAAAAA", table_id="t_1", starts_at_local="2026-06-01T18:00",
                               starts_at_utc="2026-06-01T16:00:00+00:00"),
            seeded_reservation("BBBBBB", table_id="t_2", starts_at_local="2026-06-03T18:00",
                               starts_at_utc="2026-06-03T16:00:00+00:00"),
            seeded_reservation("CCCCCC", table_id="t_3", starts_at_local="2026-06-02T18:00",
                               starts_at_utc="2026-06-02T16:00:00+00:00"),
        ]
        cancelled = seeded_reservation("DDDDDD", table_id="t_1",
                                      starts_at_local="2026-06-04T18:00",
                                      starts_at_utc="2026-06-04T16:00:00+00:00")
        cancelled["status"] = "cancelled"
        with service() as base_url:
            anon = Client(base_url, token=None)
            anon.post("/_test/reset", json_body=fixture(
                restaurants=[restaurant("r_anker")], reservations=seeds + [cancelled]))
            client = anon.authenticate(ADA["email"], ADA["password"])
            payload = client.get("/reservations").json
            legs = [("body is the {reservations:[...]} envelope", True,
                     isinstance(payload, dict) and "reservations" in payload)]
            listed = payload["reservations"] if isinstance(payload, dict) and \
                "reservations" in payload else payload
            starts = [row["starts_at"] for row in listed]
            legs.append(("confirmed and cancelled alike are listed", 4, len(listed)))
            legs.append(("starts_at descending", sorted(starts, reverse=True), starts))
            assert_all(self, legs)

    def test_party_size_wrong_type_is_422(self):
        """§5: an invalid `party_size`, *including strings and booleans*, is 422 validation_failed."""
        cases = {"string": "four", "boolean": True, "null": None, "float": 4.0}
        with reset_with([restaurant("r_anker")]) as client:
            legs = []
            for label, value in cases.items():
                resp = client.post("/reservations", json_body={
                    "restaurant_id": "r_anker", "table_id": "t_2",
                    "starts_at_local": "2026-06-01T19:00", "party_size": value},
                    headers={"Idempotency-Key": f"ps-{label}"})
                legs.append((f"party_size={label}", (422, "validation_failed"),
                             (resp.status, resp.code)))
            assert_all(self, legs)

    def test_unknown_table_is_404(self):
        """§8: unknown restaurant, unknown table, or another restaurant's table → 404 not_found."""
        with reset_with(two_restaurants(shared_table_ids=False)) as client:
            unknown = client.post("/reservations", json_body={
                "restaurant_id": "r_one", "table_id": "t_nope",
                "starts_at_local": "2026-06-01T19:00", "party_size": 4},
                headers={"Idempotency-Key": "unknown-table"})
            # `u_1` exists, but at r_two: same id at another restaurant is still not found.
            foreign = client.post("/reservations", json_body={
                "restaurant_id": "r_one", "table_id": "u_1",
                "starts_at_local": "2026-06-01T19:00", "party_size": 2},
                headers={"Idempotency-Key": "foreign-table"})
            legs = [
                ("unknown table", (404, "not_found"), (unknown.status, unknown.code)),
                ("another restaurant's table", (404, "not_found"), (foreign.status, foreign.code)),
            ]
            assert_all(self, legs)

    def test_slot_grid_and_opening_hours_codes(self):
        """§8: off-grid → 422 not_on_slot_grid; outside hours or past closes → 422 outside_opening_hours.

        Hours are 18:00–23:00 with 30-minute slots and a 90-minute booking, so 19:07 is off-grid,
        12:00 is before opening, and 22:45 would end at 00:15, past closing.
        """
        cases = {
            "19:07": ("not_on_slot_grid", "off the 30-minute grid"),
            "12:00": ("outside_opening_hours", "before opening"),
            "22:45": ("outside_opening_hours", "would end after closes"),
            "17:30": ("not_on_slot_grid", "one slot before opening, so off-grid"),
        }
        with reset_with([restaurant("r_anker")]) as client:
            legs = []
            for at, (want_code, why) in cases.items():
                resp = client.post("/reservations", json_body={
                    "restaurant_id": "r_anker", "table_id": "t_2",
                    "starts_at_local": f"2026-06-01T{at}", "party_size": 4},
                    headers={"Idempotency-Key": f"grid-{at}"})
                legs.append((f"{at} ({why})", (422, want_code), (resp.status, resp.code)))
            assert_all(self, legs)

    def test_skipped_local_time_is_invalid_local_time(self):
        """§9: a local time inside the spring-forward gap never happened → 422 invalid_local_time."""
        with reset_with([restaurant("r_anker", timezone="Europe/Berlin")]) as client:
            legs = []
            for value in ("2026-03-29T02:30", "2026-03-29T02:00", "2026-03-29T02:59"):
                resp = client.post("/reservations", json_body={
                    "restaurant_id": "r_anker", "table_id": "t_2",
                    "starts_at_local": value, "party_size": 4},
                    headers={"Idempotency-Key": f"gap-{value}"})
                legs.append((f"{value} in the skipped hour",
                             (422, "invalid_local_time"), (resp.status, resp.code)))
            assert_all(self, legs)

    def test_party_exceeds_capacity(self):
        """§8: `party_size` over the table's `capacity` → 422 `party_exceeds_capacity`."""
        with reset_with([restaurant("r_anker")]) as client:
            legs = []
            for table_id, party_size in (("t_1", 3), ("t_2", 5), ("t_3", 7)):
                resp = client.post("/reservations", json_body={
                    "restaurant_id": "r_anker", "table_id": table_id,
                    "starts_at_local": "2026-06-01T19:00", "party_size": party_size},
                    headers={"Idempotency-Key": f"cap-{table_id}"})
                legs.append((f"{table_id} at {party_size}",
                             (422, "party_exceeds_capacity"), (resp.status, resp.code)))
            assert_all(self, legs)

    def test_key_resolution_outranks_field_validation_on_create(self):
        """§7:243-246 -- the key is resolved after the body parses and after auth, before field checks.

        The observable consequence of that order is the idempotency contract: a key spent on one
        body and reused with a *different* body is `409 idempotency_key_reuse`, whatever that
        second body is. Validating fields before the key is read makes a nonsense body answer
        `400 malformed_request` instead, and the receipt is then reusable -- so a caller who
        mistypes a field after a timeout can book twice.

        Three legs, and the third is what makes it an ordering defect rather than a validation one:
        the same broken body on an *unused* key is 400, and the same body replayed on the *spent*
        key is 409. If validation were simply missing the check, the second leg would pass. If key
        resolution were simply broken, the third would be 400 too. Only the order explains all three.

        The wrong-typed field is `table_id`, deliberately. `party_size` would have been the obvious
        choice and the wrong one: §5:57 and §5:48 contradict each other on it, which is why
        `party_size_wrong_type_is_422` is an open named defect, so asserting either code here would
        have this test's verdict move with someone else's fix. `table_id` is uncontested -- §5:48
        makes a non-string 400 `malformed_request` -- so this leg stays true whichever way that
        other row is settled.
        """
        with reset_with([restaurant("r_anker")]) as client:
            good = {"restaurant_id": "r_anker", "table_id": "t_2",
                    "starts_at_local": "2026-06-01T19:00", "party_size": 2}
            wrong_typed = {**good, "table_id": 17}
            first = client.post("/reservations", json_body=good,
                                headers={"Idempotency-Key": "k-ordering"})
            spent_key = client.post("/reservations", json_body=wrong_typed,
                                    headers={"Idempotency-Key": "k-ordering"})
            unused_key = client.post("/reservations", json_body=wrong_typed,
                                     headers={"Idempotency-Key": "k-ordering-unused"})
            replay = client.post("/reservations", json_body=good,
                                 headers={"Idempotency-Key": "k-ordering"})
            legs = [
                ("first use of a key", (201, None), (first.status, first.code)),
                ("the same key with a different body is key reuse, whatever that body is",
                 (409, "idempotency_key_reuse"), (spent_key.status, spent_key.code)),
                ("the same broken body on an unused key is still malformed",
                 (400, "malformed_request"), (unused_key.status, unused_key.code)),
                ("the identical body replayed is 200", (200, None), (replay.status, replay.code)),
            ]
            assert_all(self, legs)

    def test_reset_rejects_invalid_fixture(self):
        """§4 states the fixture's rules; §5 turns a violated stated rule into 422 validation_failed.

        Every case here is a rule REQUIREMENTS.md states in so many words.
        """
        cases = {
            "restaurant missing its required id":
                fixture(restaurants=[{"name": "No Id", "timezone": "Europe/Berlin",
                                      "slot_minutes": 30,
                                      "reservation_duration_minutes": 90,
                                      "cancellation_cutoff_minutes": 120,
                                      "opening_hours": [], "tables": []}]),
            "weekday outside mon..sun":
                fixture(restaurants=[restaurant(opening_hours=[
                    {"weekday": "funday", "opens": "18:00", "closes": "23:00"}])]),
            "closes earlier than opens on the same day":
                fixture(restaurants=[restaurant(opening_hours=[
                    {"weekday": "mon", "opens": "23:00", "closes": "01:00"}])]),
            "id longer than 64 characters":
                fixture(restaurants=[restaurant("r" * 65)]),
        }
        with service() as base_url:
            anon = Client(base_url, token=None)
            legs = []
            for label, body in cases.items():
                resp = anon.post("/_test/reset", json_body=body)
                legs.append((label, (422, "validation_failed"), (resp.status, resp.code)))
            assert_all(self, legs)

    def test_reset_rejects_invalid_fixture_references(self):
        """§8:344 -- `reference` is 6 to 12 characters of `A-Z0-9`; a fixture seeds reservations.

        The envelope-level rejection was already green under `reset_rejects_invalid_fixture`; this
        name isolates the reference-shape dimension, which no shipped test asserted.
        """
        bad = {
            "lowercase": "abcdef",
            "underscore": "ABCD_1",
            "too short": "AB1",
            "too long": "ABCDE0123456789",
            "only letters and digits but mixed case": "abcdEF",
        }
        with reset_with([restaurant("r_anker")]) as client:
            legs = []
            for label, reference in bad.items():
                body = fixture(restaurants=[restaurant("r_anker")],
                               reservations=[spec_seeded_reservation(reference)])
                resp = client.post("/_test/reset", json_body=body)
                legs.append((f"reference {reference!r} ({label})",
                             (422, "validation_failed"), (resp.status, resp.code)))
            assert_all(self, legs)

    def test_reset_rejects_fixtures_that_would_5xx(self):
        """§5:185 -- no 5xx on client errors; the four fixtures that used to answer 500.

        duplicate email and duplicate reference died on a UNIQUE index inside the reset
        transaction, and an unparseable `starts_at_utc` or an unknown timezone was *accepted*
        and then 500ed from the first read that resolved it. All four are now refused up front
        with 422 validation_failed, and the world they were pointed at is left intact.
        """
        with reset_with([restaurant("r_anker")]) as client:
            cases = {
                "duplicate email": fixture(users=[dict(ADA), dict(ADA)]),
                "duplicate reference": fixture(
                    restaurants=[restaurant("r_anker")],
                    reservations=[spec_seeded_reservation("AAAAAA"),
                                  spec_seeded_reservation("AAAAAA")]),
                "unparseable starts_at_utc": fixture(
                    restaurants=[restaurant("r_anker")],
                    reservations=[seeded_reservation("AAAAAA", starts_at_utc="not-a-time")]),
                "unknown timezone": fixture(restaurants=[
                    restaurant("r_anker", timezone="Mars/Olympus")]),
            }
            day = booking_date()
            before = client.get("/availability", params={
                "restaurant_id": "r_anker", "date": day, "party_size": 2})
            legs = []
            for label, body in cases.items():
                resp = client.post("/_test/reset", json_body=body)
                legs.append((label, (422, "validation_failed"), (resp.status, resp.code)))
                after = client.get("/availability", params={
                    "restaurant_id": "r_anker", "date": day, "party_size": 2})
                legs.append((f"{label} leaves the destination intact", before.json, after.json))
            assert_all(self, legs)

    def test_auth_returns_display_name(self):
        """§6: signup → 201 {user_id, display_name, token} and login → 200 the same."""
        with service() as base_url:
            anon = Client(base_url, token=None)
            signup = anon.request("POST", "/auth/signup", json_body={
                "email": "nadia@example.com", "password": "correct horse",
                "display_name": "Nadia"}, token=None)
            login = anon.login("nadia@example.com", "correct horse")
            legs = [
                ("signup status", 201, signup.status),
                ("signup body has display_name", True,
                 "display_name" in (signup.json or {}) and
                 (signup.json or {}).get("display_name") == "Nadia"),
                ("login status", 200, login.status),
                ("login body has display_name", True,
                 "display_name" in (login.json or {}) and
                 (login.json or {}).get("display_name") == "Nadia"),
            ]
            assert_all(self, legs)

    def test_idempotency_key_scoped_by_path(self):
        """§7: a replay needs same user, same method, same *path*, same body.

        §7:80 -- the same key and body on a *different* path is a first use and must succeed
        normally. The receipt table's primary key was `(key, user_id)` with no path, so a key spent
        on a create made the same body on the batch endpoint look like a replay of the other route.

        The probe has to be a request that can actually commit, and this is the second revision of
        it. `{"moves": []}` cannot: §11:186 bounds a batch at 1-8 objects, so an empty array is
        422 `validation_failed` before any resource is read. That answer is identical with and
        without the `scope` column, so the probe was reporting red for a reason that had nothing to
        do with the defect it names -- the failure mode this module's own docstring calls a cause
        the runner never checked. The batch therefore moves the booking the first leg created.

        Which means the booking cannot sit on a hard-coded past date: §11:196 applies each
        booking's existing cancellation cutoff to every move, and a start already inside that
        window answers 409 `cutoff_passed` before the scope lookup is ever reached.
        """
        body = {"restaurant_id": "r_one", "table_id": "t_2",
                "starts_at_local": local(booking_date()), "party_size": 4}
        with reset_with(two_restaurants(shared_table_ids=False)) as client:
            first = client.post("/reservations", json_body=body,
                                headers={"Idempotency-Key": "cross-path"})
            same_key_other_path = client.post(
                "/reservation-moves",
                json_body={"moves": [{"reference": first.json["reference"]}]},
                headers={"Idempotency-Key": "cross-path"})
            legs = [
                ("first use on POST /reservations", 201, first.status),
                ("POST /reservation-moves exists (§7's second idempotency-required path)",
                 True, same_key_other_path.status != 404),
                ("same key + body on a different path is a first use, not a 200 replay",
                 201, same_key_other_path.status),
            ]
            assert_all(self, legs)

    def test_restaurants_list_envelope(self):
        """§8: `GET /restaurants` → `{"restaurants":[{id,name,timezone}]}`."""
        with reset_with(two_restaurants(shared_table_ids=False)) as client:
            payload = Client(client.base_url, token=None).get("/restaurants").json
            legs = [("body is the {restaurants:[...]} envelope", True,
                     isinstance(payload, dict) and "restaurants" in payload)]
            listed = payload["restaurants"] if isinstance(payload, dict) and \
                "restaurants" in payload else payload
            listed = listed if isinstance(listed, list) else []
            legs.append(("two seeded restaurants are listed", 2, len(listed)))
            legs.append(("every entry carries id, name and timezone", True,
                         all({"id", "name", "timezone"} <= set(row) for row in listed)))
            assert_all(self, legs)

    def test_occupancy_scoped_by_restaurant(self):
        """Runs in full. No longer blocked.

        §8 requires `available_table_ids` to be "tables *of that restaurant*", and the name covers
        both halves of that: what availability offers, and what can actually be booked.

        It was blocked on `reset_two_restaurants_sharing_table_ids`, because a two-restaurant
        fixture sharing table ids used to 500 at reset on `store.py`'s global PRIMARY KEY, and
        "red because occupancy is unscoped" was then indistinguishable from "red because reset
        500s". Keying `tables` by `(restaurant_id, id)` removed that cause, and the assertion ran
        for the first time -- and went red, because `_assert_slot_free` still read occupancy by
        `table_id` alone. Keying the table made two restaurants able to own a `t_2` each, which is
        exactly the world the unscoped query gets wrong; the latent defect became a live one.
        `GET /availability` filtered by `restaurant_id`, so it *offered* `t_2` while the booking
        answered 409 -- an offered table that could not be booked. Both are now scoped.

        The `reset_shared_table_ids_is_fixed` guard stays, and it is what this docstring used to be
        wrong about: it is a fallback, not the current state. If the composite key ever regresses
        the test skips again, and `_report` will now say so, because it reads the skip rather than
        assuming it.
        """
        blocked_on = BLOCKED["occupancy_scoped_by_restaurant"]
        if reset_shared_table_ids_is_fixed():
            return self._assert_occupancy_is_scoped_by_restaurant()
        raise unittest.SkipTest(
            f"not yet provable: blocked_on {blocked_on} — a two-restaurant fixture sharing table "
            f"ids 500s at reset, and a red entry would assert a cause the runner never checked")

    def _assert_occupancy_is_scoped_by_restaurant(self):
        """Both halves of "of that restaurant": what availability offers, and what can be booked."""
        with reset_with(two_restaurants(shared_table_ids=True)) as client:
            body = {"restaurant_id": "r_one", "table_id": "t_2",
                    "starts_at_local": "2026-06-01T19:00", "party_size": 4}
            booked = client.post("/reservations", json_body=body,
                                 headers={"Idempotency-Key": "r-one"})
            slots = (client.get("/availability", params={
                "restaurant_id": "r_two", "date": "2026-06-01",
                "party_size": 4}).json or {}).get("slots", [])
            at_nineteen = {row["starts_at_local"]: row["available_table_ids"]
                           for row in slots}.get("2026-06-01T19:00", [])
            # r_two owns its own t_2; r_one's booking must not take it away.
            same_slot = client.post("/reservations", json_body={
                "restaurant_id": "r_two", "table_id": "t_2",
                "starts_at_local": "2026-06-01T19:00", "party_size": 4},
                headers={"Idempotency-Key": "r-two"})
            # ...nor may r_one's booking block r_two's.
            assert_all(self, [
                ("r_one books t_2", 201, booked.status),
                ("r_two still offers its own t_2 at the same slot", True, "t_2" in at_nineteen),
                ("r_two may book the same table id at the same time", 201, same_slot.status),
            ])

    def test_opening_hours_in_fixture_order(self):
        """§8: `GET /restaurants/{id}` returns `opening_hours` "in the fixture's shape".

        The fixture order below is deliberately not alphabetical, so an `ORDER BY weekday` cannot
        coincide with it by accident.
        """
        ordered = [{"weekday": "sat", "opens": "09:00", "closes": "12:00"},
                   {"weekday": "mon", "opens": "10:00", "closes": "13:00"},
                   {"weekday": "fri", "opens": "11:00", "closes": "14:00"}]
        with service() as base_url:
            anon = Client(base_url, token=None)
            anon.post("/_test/reset", json_body=fixture(
                restaurants=[restaurant("r_anker", opening_hours=ordered)]))
            payload = anon.get("/restaurants/r_anker").json or {}
            got = [row["weekday"] for row in payload.get("opening_hours", [])]
            legs = [("opening_hours follow the fixture's order",
                     [row["weekday"] for row in ordered], got)]
            assert_all(self, legs)

    def test_slot_end_is_absolute_across_transitions(self):
        """§9: `reservation_duration_minutes` is absolute time, not wall clock.

        Three symptoms, all reproduced on this tree, and all invisible at noon on an ordinary
        date — which is why the shipped `test_tz.py` is pinned to `2026-06-01` and passes:

        1. `slot_end` adds 90 *wall-clock* minutes, so within 90 minutes of a transition the
           returned instant is 30 or 150 real minutes out, not 90.
        2. Spring forward, a booking at 01:30 is admitted alongside one at 03:00 even though their
           true absolute intervals overlap.
        3. Fall back, a slot that is genuinely free is withheld because the end is an hour late.

        §9's worked example for the same rule — 90 absolute minutes from 01:30 on a fall-back
        night ends at local 02:00, not 03:00 — is asserted under
        `reservation_body_has_ends_at_and_created_at`, because it needs the `ends_at` field and
        that field is the other defect's. Asserting it here would mean neither defect could be
        fixed alone.
        """
        legs = []

        # (1) the absolute property itself, pinned where absolute and wall clock DISAGREE. At
        # 19:00 on an ordinary date the two arithmetic modes coincide, so that case guards
        # against a regression while proving nothing about the bug.
        for zone_name, value in [("Europe/Berlin", "2026-10-25T01:30"),
                                 ("Europe/Berlin", "2026-10-25T02:00"),
                                 ("Europe/Berlin", "2026-03-29T01:30"),
                                 ("America/New_York", "2026-11-01T00:30"),
                                 ("America/New_York", "2026-03-08T01:30"),
                                 ("Europe/Berlin", "2026-06-01T19:00")]:
            start = resolve(parse_local(value), ZoneInfo(zone_name))
            delta = (slot_end(start, DURATION).astimezone(UTC)
                     - start.astimezone(UTC))
            legs.append((f"slot_end is absolute at {value} {zone_name}",
                         dt.timedelta(minutes=DURATION), delta))

        spring = SPRING_FORWARD[0]
        with reset_with([restaurant("r_anker", timezone="Europe/Berlin",
                                    opening_hours=opening_hours_on(spring, "00:00", "07:00"))]) as client:
            # (2) the spring-forward double booking
            first = client.post("/reservations", json_body={
                "restaurant_id": "r_anker", "table_id": "t_2",
                "starts_at_local": f"{spring}T03:00", "party_size": 2},
                headers={"Idempotency-Key": "sf-late"})
            overlap = client.post("/reservations", json_body={
                "restaurant_id": "r_anker", "table_id": "t_2",
                "starts_at_local": f"{spring}T01:30", "party_size": 2},
                headers={"Idempotency-Key": "sf-early"})
            legs.append((f"{spring} 01:30 overlaps 03:00 in absolute time, so is rejected",
                         (201, 409, "table_unavailable"),
                         (first.status, overlap.status, overlap.code)))

        fall = FALL_BACK[0]
        with reset_with([restaurant("r_anker", timezone="Europe/Berlin",
                                    opening_hours=opening_hours_on(fall, "00:00", "07:00"))]) as client:
            client.post("/reservations", json_body={
                "restaurant_id": "r_anker", "table_id": "t_2",
                "starts_at_local": f"{fall}T03:00", "party_size": 2},
                headers={"Idempotency-Key": "fb-booked"})
            slots = (client.get("/availability", params={
                "restaurant_id": "r_anker", "date": fall, "party_size": 2}).json or {}).get("slots", [])
            free = {row["starts_at_local"][-5:]: row["available_table_ids"] for row in slots}
            # (3) the fall-back free slots that are withheld
            for at in ("02:00", "02:30"):
                legs.append((f"{fall} {at} is genuinely free, so t_2 is still offered",
                             True, "t_2" in free.get(at, [])))

        assert_all(self, legs)

    def test_internal_error_message_is_redacted(self):
        """The 500 body leaks the SQLite schema. It is the one defect here that is observably a leak.

        Same reset as `reset_two_restaurants_sharing_table_ids`, but the assertion is about the
        message: a fixed string that discloses none of the internals, plus a correlation id so the
        response is still actionable. Today the message is
        `"UNIQUE constraint failed: tables.id"`.
        """
        with service() as base_url:
            anon = Client(base_url, token=None)
            resp = anon.post("/_test/reset",
                             json_body=fixture(restaurants=two_restaurants(shared_table_ids=True)))
            error = (resp.json or {}).get("error") or {}
            message = error.get("message") or ""
            forbidden = [word for word in ("UNIQUE", "tables.id", "sqlite3", "Traceback")
                         if word in message]
            legs = [
                ("status", 500, resp.status),
                ("error.code is internal_error", "internal_error", error.get("code")),
                ("message leaks none of", [], forbidden),
                ("message is a fixed string, not the exception text", True,
                 message == "an internal error occurred"),
                ("a correlation id is present", True,
                 bool(error.get("correlation_id"))),
            ]
            assert_all(self, legs)


class ReferencedCoverage(unittest.TestCase):
    """Coverage the shipped suite already owns. Referenced, never cloned.

    A gate that re-asserts the §7 burst tally and the 50-concurrent bound would duplicate two
    passing tests while making the gate's own count harder to defend — the exact
    counting-instead-of-naming failure this gate exists to prevent. These two assertions just
    confirm the shipped tests are still there and still named as expected.
    """

    def test_the_shipped_suite_still_owns_the_two_tallies(self):
        from tests import test_service

        # The tests live inside TestCase classes, so the module namespace has to be swept too.
        present = {name for _, obj in vars(test_service).items() if isinstance(obj, type)
                   for name in vars(obj) if name.startswith("test_")}
        missing = [name for name in REFERENCED_SHIPPED_TESTS if name not in present]
        self.assertEqual(
            missing, [],
            "the shipped tests this gate references rather than clones are missing: "
            + "; ".join(f"{name} ({REFERENCED_SHIPPED_TESTS[name]})" for name in missing))


# ---- the gate: subset assertion ---------------------------------------------------------

def _defect_name_of(test_id: str) -> str:
    """`tests.test_spec_stage1.SpecGate.test_x[leg 1]` -> `x`.

    The `test_` prefix is stripped because the named list is written as bare defect names, so a
    comparison that kept the prefix would match nothing and call every red entry "unnamed".
    """
    name = test_id.rsplit(".", 1)[-1].split("[", 1)[0]
    return name[len("test_"):] if name.startswith("test_") else name


def _gate_verdict(result):
    failing = {_defect_name_of(test.id()) for test, _ in
               list(result.failures) + list(result.errors)
               if _defect_name_of(test.id()) in NAMED_DEFECTS}
    unnamed = {_defect_name_of(test.id()) for test, _ in
               list(result.failures) + list(result.errors)
               if _defect_name_of(test.id()) not in NAMED_DEFECTS}
    return failing, unnamed


def _report(result) -> bool:
    """Print the gate report. Returns False when the subset property does not hold."""
    failing, unnamed = _gate_verdict(result)
    known_red = sorted(failing)
    # Blocked is read off the run, not off `BLOCKED`: a name is unprovable only when its test
    # actually skipped. Any named defect that skipped is reported here, not just the declared
    # ones, so an unexpected skip is surfaced rather than quietly counted as green -- and
    # `BLOCKED` still supplies the reason where one is on record.
    skipped = {_defect_name_of(test.id()) for test, _ in list(result.skipped)
               if _defect_name_of(test.id()) in NAMED_DEFECTS}
    blocked = sorted(skipped)
    green = sorted(set(NAMED_DEFECTS) - failing - set(blocked))
    reason = lambda name: BLOCKED.get(name, "skipped without a recorded blocker")

    lines = [
        "",
        "=" * 78,
        "SPEC GATE (Unit 0) — subset assertion",
        "=" * 78,
        f"named defects ............ {len(NAMED_DEFECTS)}",
        f"  red (named) ............ {len(known_red)}",
        f"  not yet provable ....... {len(blocked)}  {', '.join(blocked) or ''}",
        f"  green .................. {len(green)}  {', '.join(green) or ''}",
        f"declined ................ {len(DECLINED)}  {', '.join(DECLINED) or ''}",
        "",
        "RED (each is one named defect, one failing test):",
    ]
    lines += [f"  - {name}" for name in known_red] or ["  - (none)"]
    lines += ["", "NOT YET PROVABLE (never counted as red — the runner never checked the cause):"]
    lines += [f"  - {name}\n      blocked_on: {reason(name)}" for name in blocked] or ["  - (none)"]
    lines += ["", "DECLINED (the plan proposed these; the spec does not support them):"]
    lines += [f"  - {name}\n      {DECLINED[name]}" for name in DECLINED] or ["  - (none)"]

    if unnamed:
        lines += [
            "",
            "GATE FAILED — unnamed failures:",
        ]
        lines += [f"  - {name}" for name in sorted(unnamed)]
        lines += [
            "",
            "These failures are not named defects. Something has diverged between the tree and the",
            "spec that this list does not know about, so the run must not be believed until it is",
            "explained. The subset property is: failing test names IS A SUBSET OF the named list.",
        ]
    else:
        lines += [
            "",
            "GATE PASSED — every failing test is a named defect.",
            "",
            "The count above is reported, not asserted. Count monotonicity is a separate, manual",
            "experiment (fix defect k, observe exactly k-1 names left; revert, observe it back) and",
            "is deliberately NOT implemented in this runner: a runner that recomputed an expected",
            "count could not tell a corrected shipped test from a dropped defect.",
        ]
    lines.append("=" * 78)
    print("\n".join(lines))
    return not unnamed


class _GateProbe:
    """Carries the synthetic failure when the subset property does not hold.

    Deliberately not a `TestCase` subclass: the loader collects every TestCase in this module, and a
    helper that is also a test is noise. It implements the two methods `TextTestResult` calls on a
    failure it reports.
    """

    def id(self) -> str:
        return "spec_gate.subset_assertion"

    def shortDescription(self):
        return None

    def __str__(self) -> str:
        return "spec gate subset assertion"


class _GateResult(TextTestResult):
    """Runs the subset assertion at the end of the whole run.

    This is installed as the top-level runner's resultclass so it sees every failure in the
    process, including the shipped suites'. A gate that only watched its own module could not see
    a corrected shipped test surface as an unnamed failure, which is the whole reason the subset
    assertion exists (plan rev. 3.11).
    """

    def stopTestRun(self):
        super().stopTestRun()
        if _report(self):
            return
        unnamed = sorted(_gate_verdict(self)[1])
        # Appended directly rather than through `addFailure`: `TextTestResult._exc_info_to_string`
        # unpacks its argument as an exc_info triple, so a plain message is not accepted there.
        self.failures.append((
            _GateProbe(),
            "the spec gate's subset assertion did not hold; unnamed failing tests: "
            + ", ".join(unnamed) + "\n"))


# Install the gate as the top-level runner's resultclass. This module is imported while
# `TestProgram.runTests` is still assembling the suite, which happens before the result object is
# created, so the patch is in place by the time the run starts. Idempotent.
if getattr(unittest.TextTestRunner, "resultclass", None) is not _GateResult:
    unittest.TextTestRunner.resultclass = _GateResult