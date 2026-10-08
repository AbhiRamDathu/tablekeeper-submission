"""`PATCH /reservations/{reference}` -- REQUIREMENTS.md:139-143.

The amendment endpoint, which shipped unimplemented: before this module every PATCH, authorised or
not, was a 501. The contract is unusually dense for one route, so each row below is named for the
spec line it pins rather than grouped by behaviour:

* :139 any subset of `table_id`, `starts_at_local`, `party_size`; no `Idempotency-Key` required
* :140 same validation as create; cutoff measured against the **current** start
* :141 cancelled -> 409; releases the old slot and reserves the new one together
* :142 a failed amendment leaves the original booking and its occupancy unchanged
* :143 `reference` and `reservation_id` survive

Two of these are proven by *booking*, not by inspecting the row: that a slot was released and that
a slot was reserved are both claims about what a second caller can now do, and `GET /availability`
is not a trustworthy witness while `slot_grid_and_opening_hours_codes` is still red in the gate.

Deliberately NOT asserted here, because they belong to other units and asserting them would move a
named defect's state from this commit: the response body's `ends_at`/`created_at`
(`reservation_body_has_ends_at_and_created_at`), and a missing table answering 422 where the spec
wants 404 (`unknown_table_is_404`). PATCH inherits both by reusing the create path, which is the
point of :140 -- fixing them here would fix them for create too, unasked.
"""
from __future__ import annotations

import unittest

from tests.support import (
    ADA,
    BOB,
    Client,
    booking_date,
    fixture,
    local,
    restaurant,
    service,
)

RESTAURANT = "r_anker"


class PatchCase(unittest.TestCase):
    """A reset world with Ada and Bob signed in and a booking to amend."""

    def setUp(self):
        self._ctx = service()
        self.base_url = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        self.anon = Client(self.base_url)
        self.day = booking_date()
        self.reset()
        self.ada = self.sign_in(ADA)
        self.bob = self.sign_in(BOB)

    def reset(self, **kwargs):
        resp = self.anon.post("/_test/reset", json_body=fixture(
            users=[ADA, BOB], restaurants=[restaurant(**kwargs.pop("restaurant", {}))], **kwargs))
        self.assertEqual(resp.status, 204)

    def sign_in(self, user):
        resp = self.anon.login(user["email"], user["password"])
        self.assertEqual(resp.status, 200)
        return Client(self.base_url, token=resp.json["token"])

    def create(self, client, table_id, at, *, party_size=2, key="k-create"):
        resp = client.post("/reservations", json_body={
            "restaurant_id": RESTAURANT, "table_id": table_id,
            "starts_at_local": local(self.day, at), "party_size": party_size,
        }, headers={"Idempotency-Key": key})
        self.assertEqual(resp.status, 201)
        return resp.json["reference"]

    def patch(self, client, reference, body, *, token=..., headers=None):
        # `token` defaults to `...`, the sentinel `Client.request` uses for "inherit the client's
        # token". Passing None would mean "send no Authorization header" and quietly turn every
        # assertion in this module into a test of 401.
        return client.request("PATCH", f"/reservations/{reference}",
                              json_body=body, token=token, headers=headers)

    def current(self, client, reference):
        resp = client.get(f"/reservations/{reference}")
        self.assertEqual(resp.status, 200)
        return resp.json


class AnySubset(PatchCase):
    """:139 -- any subset of the three mutable fields."""

    def test_changing_only_party_size_leaves_the_other_fields_alone(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        self.assertEqual(self.patch(self.ada, reference, {"party_size": 3}).status, 200)
        after = self.current(self.ada, reference)
        self.assertEqual(after["party_size"], 3)
        self.assertEqual(after["table_id"], "t_2")
        self.assertEqual(after["starts_at_local"], local(self.day, "19:00"))

    def test_changing_only_table_id_leaves_the_other_fields_alone(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        self.assertEqual(self.patch(self.ada, reference, {"table_id": "t_3"}).status, 200)
        after = self.current(self.ada, reference)
        self.assertEqual(after["table_id"], "t_3")
        self.assertEqual(after["party_size"], 2)
        self.assertEqual(after["starts_at_local"], local(self.day, "19:00"))

    def test_changing_only_starts_at_local_leaves_the_other_fields_alone(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        self.assertEqual(
            self.patch(self.ada, reference, {"starts_at_local": local(self.day, "20:00")}).status, 200)
        after = self.current(self.ada, reference)
        self.assertEqual(after["starts_at_local"], local(self.day, "20:00"))
        self.assertEqual(after["table_id"], "t_2")
        self.assertEqual(after["party_size"], 2)

    def test_an_empty_subset_is_a_no_op_success(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        before = self.current(self.ada, reference)
        self.assertEqual(self.patch(self.ada, reference, {}).status, 200)
        self.assertEqual(self.current(self.ada, reference), before)

    def test_unknown_fields_are_ignored_never_an_error(self):
        """REQUIREMENTS.md:28."""
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        self.assertEqual(self.patch(self.ada, reference, {"colour": "blue"}).status, 200)


class Idempotency(PatchCase):
    """:139 and §7 -- a PATCH needs no key, and one sent is not a replay."""

    def test_no_idempotency_key_is_required(self):
        reference = self.create(self.ada, "t_2", "19:00", key="k-a")
        resp = self.patch(self.ada, reference, {"party_size": 3})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.current(self.ada, reference)["party_size"], 3)

    def test_a_key_already_used_by_a_create_is_not_treated_as_a_replay(self):
        """§7:80 -- the same key on a different path is not a replay and must succeed normally."""
        reference = self.create(self.ada, "t_2", "19:00", key="shared-key")
        resp = self.patch(self.ada, reference, {"party_size": 3},
                          headers={"Idempotency-Key": "shared-key"})
        self.assertEqual(resp.status, 200, resp.raw)
        self.assertEqual(self.current(self.ada, reference)["party_size"], 3)


class SameValidationAsCreate(PatchCase):
    """:140 -- the amendment is checked exactly as a create would be."""

    def test_party_size_below_one_is_rejected(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        resp = self.patch(self.ada, reference, {"party_size": 0})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (422, "validation_failed"))

    def test_party_size_above_the_new_tables_capacity_is_rejected(self):
        """Changing the party and the table together is validated against the NEW table."""
        reference = self.create(self.ada, "t_3", "19:00", party_size=2)
        resp = self.patch(self.ada, reference, {"table_id": "t_1", "party_size": 4})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (422, "party_exceeds_capacity"))

    def test_a_party_that_fits_the_old_table_but_not_the_new_one_is_rejected(self):
        reference = self.create(self.ada, "t_3", "19:00", party_size=6)
        resp = self.patch(self.ada, reference, {"table_id": "t_1"})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (422, "party_exceeds_capacity"))

    def test_a_wrong_json_type_is_malformed_not_validation_failed(self):
        """§5:48 -- a field of the wrong JSON type is 400, not 422."""
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        for value in ("3", 3.0, True, None):
            with self.subTest(party_size=value):
                resp = self.patch(self.ada, reference, {"party_size": value})
                # §5:172-174 carves `party_size` out of that rule: strings and booleans (and nulls
                # and floats) are 422 validation_failed. `table_id` / `starts_at_local` stay 400.
                self.assertEqual((resp.status, resp.json["error"]["code"]),
                                 (422, "validation_failed"))

    def test_a_non_object_body_is_malformed(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        resp = self.patch(self.ada, reference, "not-an-object")
        self.assertEqual((resp.status, resp.json["error"]["code"]), (400, "malformed_request"))

    def test_a_non_existent_local_time_is_rejected(self):
        """§9 -- the skipped hour of a spring-forward night does not exist."""
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        resp = self.patch(self.ada, reference, {"starts_at_local": "2026-03-29T02:30"})
        self.assertEqual(resp.status, 422)

    def test_an_unavailable_table_conflicts(self):
        other = self.create(self.bob, "t_3", "19:00", key="k-bob")
        reference = self.create(self.ada, "t_2", "19:00", key="k-ada")
        resp = self.patch(self.ada, reference,
                          {"table_id": "t_3", "starts_at_local": local(self.day, "19:00")})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (409, "table_unavailable"))
        self.assertEqual(self.current(self.bob, other)["table_id"], "t_3")


class Authorization(PatchCase):
    """§5:52 -- "no such resource, or not visible to this caller" is one 404."""

    def test_an_unknown_reference_is_not_found(self):
        resp = self.patch(self.ada, "ZZZZZZ", {"party_size": 3})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (404, "not_found"))

    def test_another_callers_reservation_is_not_found_not_forbidden(self):
        """A 403 would confirm the reference exists; §5 folds both cases into not_found."""
        reference = self.create(self.bob, "t_2", "19:00", key="k-bob")
        resp = self.patch(self.ada, reference, {"party_size": 3})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (404, "not_found"))

    def test_a_missing_token_is_unauthenticated(self):
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.patch(self.ada, reference, {"party_size": 3}, token=None)
        self.assertEqual((resp.status, resp.json["error"]["code"]), (401, "unauthenticated"))

    def test_an_unknown_token_is_unanswered_as_a_missing_one(self):
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.patch(self.ada, reference, {"party_size": 3}, token="not-a-real-token")
        self.assertEqual((resp.status, resp.json["error"]["code"]), (401, "unauthenticated"))


class Identity(PatchCase):
    """:143 -- `reference` and `reservation_id` survive."""

    def test_both_identifiers_and_the_status_survive_an_amendment(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        resp = self.patch(self.ada, reference, {"table_id": "t_3", "party_size": 4})
        self.assertEqual(resp.status, 200)
        body = resp.json
        self.assertEqual(body["reference"], reference)
        self.assertEqual(body["reservation_id"], reference)
        self.assertEqual(body["status"], "confirmed")

    def test_the_reference_is_reachable_after_the_amendment(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        self.patch(self.ada, reference, {"table_id": "t_3"})
        self.assertEqual(self.current(self.ada, reference)["table_id"], "t_3")


class Cancelled(PatchCase):
    """:141 -- a cancelled booking cannot be amended."""

    def test_a_cancelled_reservation_is_reservation_cancelled(self):
        day = booking_date()
        seeded = {
            "reference": "CANCEL1", "restaurant_id": RESTAURANT, "table_id": "t_2",
            "user_id": ADA["id"], "starts_at_utc": f"{day}T17:00:00+00:00",
            "starts_at_local": local(day, "19:00"), "party_size": 2,
            "status": "cancelled", "created_at": "2026-05-01T00:00:00+00:00",
        }
        self.reset(reservations=[seeded])
        self.ada = self.sign_in(ADA)  # reset clears the tokens table, so the cached token is dead
        resp = self.patch(self.ada, "CANCEL1", {"party_size": 3})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (409, "reservation_cancelled"))


class Cutoff(PatchCase):
    """:140 -- the cutoff is measured against the CURRENT start, not the requested one."""

    def test_an_amendment_inside_the_cutoff_window_is_cutoff_passed(self):
        # A 30-day cutoff swallows a booking made 7 days out, so the window is unambiguously open
        # without any test having to move the clock.
        self.reset(restaurant={"cancellation_cutoff_minutes": 60 * 24 * 30})
        self.ada = self.sign_in(ADA)
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.patch(self.ada, reference, {"party_size": 3})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (409, "cutoff_passed"))
        self.assertEqual(self.current(self.ada, reference)["party_size"], 2)

    def test_moving_the_booking_later_does_not_reopen_the_window(self):
        """Re-anchoring the cutoff to the requested start would let a caller walk out of it."""
        self.reset(restaurant={"cancellation_cutoff_minutes": 60 * 24 * 30})
        self.ada = self.sign_in(ADA)
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.patch(self.ada, reference, {"starts_at_local": local(self.day, "22:00")})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (409, "cutoff_passed"))
        after = self.current(self.ada, reference)
        self.assertEqual(after["starts_at_local"], local(self.day, "19:00"))

    def test_an_amendment_outside_the_window_is_allowed(self):
        # The default fixture cutoff is 120 minutes and the booking is 7 days out.
        reference = self.create(self.ada, "t_2", "19:00")
        self.assertEqual(self.patch(self.ada, reference, {"party_size": 3}).status, 200)


class ReleasedAndReservedTogether(PatchCase):
    """:141 -- the old slot is released and the new one reserved in the same breath."""

    def test_a_moved_booking_frees_its_old_slot_and_takes_its_new_one(self):
        # A four-hour gap, because a booking runs 90 minutes: adjacent slots overlap, and a
        # one-step move would confound "released" with "still occupied by the mover".
        reference = self.create(self.ada, "t_3", "18:00")
        self.assertEqual(self.bob_create("t_3", "18:00", "k-bob-0"), 409,
                         "the slot must start out held")

        moved = self.patch(self.ada, reference, {"starts_at_local": local(self.day, "22:00")})
        self.assertEqual(moved.status, 200)

        self.assertEqual(self.bob_create("t_3", "18:00", "k-bob-1"), 201,
                         "the old slot must be bookable by someone else")
        self.assertEqual(self.bob_create("t_3", "22:00", "k-bob-2"), 409,
                         "the new slot must be held")

    def bob_create(self, table_id, at, key):
        resp = self.bob.post("/reservations", json_body={
            "restaurant_id": RESTAURANT, "table_id": table_id,
            "starts_at_local": local(self.day, at), "party_size": 2,
        }, headers={"Idempotency-Key": key})
        return resp.status


class FailedAmendmentChangesNothing(PatchCase):
    """:142 -- a failed amendment leaves the booking AND its occupancy unchanged."""

    def test_a_refused_amendment_leaves_the_row_byte_identical(self):
        reference = self.create(self.ada, "t_2", "19:00", party_size=2)
        before = self.current(self.ada, reference)
        for body in ({"party_size": 0}, {"party_size": 99}, {"table_id": "nope"},
                     {"starts_at_local": "not-a-time"}, {"party_size": "three"}):
            with self.subTest(body=body):
                self.assertGreaterEqual(self.patch(self.ada, reference, body).status, 400)
                self.assertEqual(self.current(self.ada, reference), before)

    def test_a_refused_amendment_leaves_the_slot_still_held(self):
        """The occupancy half of :142, proven by booking rather than by reading the row."""
        reference = self.create(self.ada, "t_3", "18:00")
        self.assertEqual(self.patch(self.ada, reference, {"party_size": 99}).status, 422)
        resp = self.bob.post("/reservations", json_body={
            "restaurant_id": RESTAURANT, "table_id": "t_3",
            "starts_at_local": local(self.day, "18:00"), "party_size": 2,
        }, headers={"Idempotency-Key": "k-probe"})
        self.assertEqual(resp.status, 409, "the refused amendment must not have freed the slot")

    def test_a_refused_amendment_leaves_the_slot_free_if_it_was_free(self):
        reference = self.create(self.ada, "t_3", "18:00")
        self.assertEqual(self.patch(self.ada, reference, {"party_size": 99}).status, 422)
        resp = self.bob.post("/reservations", json_body={
            "restaurant_id": RESTAURANT, "table_id": "t_1",
            "starts_at_local": local(self.day, "18:00"), "party_size": 2,
        }, headers={"Idempotency-Key": "k-probe2"})
        self.assertEqual(resp.status, 201)


if __name__ == "__main__":
    unittest.main()