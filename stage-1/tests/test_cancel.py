"""`POST /reservations/{reference}/cancel` -- REQUIREMENTS.md:136-138.

The cancellation endpoint, which shipped unimplemented: before this module every cancel fell through
the router to a 404, so nothing could be cancelled at all. The rows below are named for the spec
line each one pins:

* :136 `-> 200` with current state, and the table is freed immediately, "so the next
  `GET /availability` offers the slot again"
* :137 already cancelled is **200, not an error**
* :138 within the cutoff of start -> 409 `cutoff_passed`; not the caller's -> 404

Two things this module deliberately proves by *booking* rather than by reading the response:
:136-137's "frees the table immediately", and :138's refusals leaving occupancy alone. Both are
claims about what a second caller can now do, and `GET /availability` is not a trustworthy witness
while `slot_grid_and_opening_hours_codes` is still red in the gate -- so the availability half of
:136 is pinned transitively, by the conflicting booking succeeding.

Not asserted here, because they belong to other units: the response body's `ends_at`/`created_at`
(`reservation_body_has_ends_at_and_created_at`). Cancellation reuses the reservation body, so it
inherits that gap by design rather than papering over it.
"""
from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

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


class CancelCase(unittest.TestCase):
    """A reset world with Ada and Bob signed in and a booking to cancel."""

    def setUp(self):
        self._ctx = service()
        self.base_url = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        self.anon = Client(self.base_url)
        self.day = booking_date()
        self.reset()
        self.ada = self.sign_in(ADA)
        self.bob = self.sign_in(BOB)

    def reset(self, *, restaurant_kwargs=None, reservations=None):
        resp = self.anon.post("/_test/reset", json_body=fixture(
            users=[ADA, BOB],
            restaurants=[restaurant(**(restaurant_kwargs or {}))],
            reservations=reservations or []))
        self.assertEqual(resp.status, 204)

    def sign_in(self, user):
        resp = self.anon.login(user["email"], user["password"])
        self.assertEqual(resp.status, 200)
        return Client(self.base_url, token=resp.json["token"])

    def book(self, client, table_id, at, *, key="k-book", party_size=2):
        return client.post("/reservations", json_body={
            "restaurant_id": RESTAURANT, "table_id": table_id,
            "starts_at_local": local(self.day, at), "party_size": party_size,
        }, headers={"Idempotency-Key": key})

    def create(self, client, table_id, at, **kwargs):
        resp = self.book(client, table_id, at, **kwargs)
        self.assertEqual(resp.status, 201)
        return resp.json["reference"]

    def cancel(self, client, reference, *, token=...):
        # `token` defaults to `...`, the sentinel `Client.request` uses for "inherit the client's
        # token". Passing None would mean "send no Authorization header" and quietly turn an
        # assertion about cancellation into a test of 401.
        return client.request("POST", f"/reservations/{reference}/cancel", token=token)

    def current(self, client, reference):
        resp = client.get(f"/reservations/{reference}")
        self.assertEqual(resp.status, 200)
        return resp.json

    def seeded_cancelled(self, reference, table_id, at):
        """A fixture row already in the cancelled state, with a real instant for the cutoff."""
        naive = dt.datetime.fromisoformat(local(self.day, at))
        return {
            "reference": reference, "restaurant_id": RESTAURANT, "table_id": table_id,
            "user_id": ADA["id"],
            "starts_at_utc": naive.replace(tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
                dt.timezone.utc).isoformat(),
            "starts_at_local": local(self.day, at), "party_size": 2,
            "status": "cancelled", "created_at": "2026-05-01T00:00:00+00:00",
        }


class SuccessfulCancellation(CancelCase):
    """:136 -- 200 with current state, and the table is freed immediately."""

    def test_cancelling_returns_200_and_the_current_state(self):
        reference = self.create(self.ada, "t_3", "18:00")
        resp = self.cancel(self.ada, reference)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["status"], "cancelled")
        self.assertEqual(resp.json["table_id"], "t_3")
        self.assertEqual(resp.json["starts_at_local"], local(self.day, "18:00"))

    def test_the_cancellation_is_visible_on_a_later_read(self):
        reference = self.create(self.ada, "t_3", "18:00")
        self.cancel(self.ada, reference)
        self.assertEqual(self.current(self.ada, reference)["status"], "cancelled")

    def test_identifiers_and_the_rest_of_the_booking_survive(self):
        """§10:175 -- identities, statuses other than the one moved, and timestamps are not
        regenerated."""
        reference = self.create(self.ada, "t_2", "19:00", party_size=4)
        before = self.current(self.ada, reference)
        after = self.cancel(self.ada, reference).json
        self.assertEqual(after["reference"], reference)
        self.assertEqual(after["reservation_id"], reference)
        for field in ("restaurant_id", "table_id", "party_size", "starts_at_local", "starts_at"):
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field])

    def test_the_freed_table_is_immediately_bookable_by_someone_else(self):
        """The load-bearing half of :136-137, proven by booking rather than by reading a response."""
        reference = self.create(self.ada, "t_3", "18:00")
        self.assertEqual(self.book(self.bob, "t_3", "18:00", key="k-blocked").status, 409)
        self.assertEqual(self.cancel(self.ada, reference).status, 200)
        self.assertEqual(self.book(self.bob, "t_3", "18:00", key="k-after").status, 201)

    def test_only_the_cancelled_booking_stops_blocking_its_slot(self):
        first = self.create(self.ada, "t_3", "18:00", key="k-1")
        self.create(self.ada, "t_3", "22:00", key="k-2")
        self.cancel(self.ada, first)
        self.assertEqual(self.book(self.bob, "t_3", "18:00", key="k-free").status, 201)
        self.assertEqual(self.book(self.bob, "t_3", "22:00", key="k-busy").status, 409)


class AlreadyCancelled(CancelCase):
    """:137 -- already cancelled is 200, not an error."""

    def test_cancelling_twice_is_200_both_times(self):
        reference = self.create(self.ada, "t_3", "18:00")
        self.assertEqual(self.cancel(self.ada, reference).status, 200)
        for attempt in (2, 3):
            with self.subTest(attempt=attempt):
                resp = self.cancel(self.ada, reference)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.json["status"], "cancelled")

    def test_a_retry_does_not_double_free_or_alter_the_booking(self):
        reference = self.create(self.ada, "t_3", "18:00")
        self.cancel(self.ada, reference)
        after_first = self.current(self.ada, reference)
        self.cancel(self.ada, reference)
        self.assertEqual(self.current(self.ada, reference), after_first)

    def test_a_retry_releases_nothing_a_second_time(self):
        """The retry must not consume the slot the first cancellation freed."""
        reference = self.create(self.ada, "t_3", "18:00")
        self.cancel(self.ada, reference)
        self.cancel(self.ada, reference)
        self.assertEqual(self.book(self.bob, "t_3", "18:00", key="k-still-free").status, 201)

    def test_already_cancelled_beats_the_cutoff(self):
        """:137 outranks :138. Checking the cutoff first would make a successful cancellation
        permanently unretryable, because a booking cancelled inside the window stays inside it."""
        self.reset(restaurant_kwargs={"cancellation_cutoff_minutes": 60 * 24 * 30},
                   reservations=[self.seeded_cancelled("ALREADY1", "t_1", "18:00")])
        self.ada = self.sign_in(ADA)  # reset clears the tokens table
        resp = self.cancel(self.ada, "ALREADY1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["status"], "cancelled")


class Cutoff(CancelCase):
    """:138 -- within the cutoff of start, 409 cutoff_passed."""

    WIDE = {"cancellation_cutoff_minutes": 60 * 24 * 30}

    def setUp(self):
        super().setUp()
        # A 30-day cutoff swallows a booking made 7 days out, so the window is unambiguously open
        # without any test having to move the clock. Resetting clears the tokens table, so both
        # clients have to sign in again or the occupancy probes below would be answered with 401.
        self.reset(restaurant_kwargs=self.WIDE)
        self.ada = self.sign_in(ADA)
        self.bob = self.sign_in(BOB)

    def test_cancelling_inside_the_window_is_cutoff_passed(self):
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.cancel(self.ada, reference)
        self.assertEqual((resp.status, resp.json["error"]["code"]), (409, "cutoff_passed"))

    def test_a_refused_cancellation_leaves_the_booking_confirmed(self):
        reference = self.create(self.ada, "t_2", "19:00")
        self.cancel(self.ada, reference)
        self.assertEqual(self.current(self.ada, reference)["status"], "confirmed")

    def test_a_refused_cancellation_leaves_the_slot_held(self):
        """Proven by booking: the refusal must not have freed the table."""
        reference = self.create(self.ada, "t_3", "18:00")
        self.assertEqual(self.cancel(self.ada, reference).status, 409)
        self.assertEqual(self.book(self.bob, "t_3", "18:00", key="k-probe").status, 409)

    def test_the_same_window_refuses_an_amendment(self):
        """The two endpoints share one cutoff rule, so they refuse together."""
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.ada.request("PATCH", f"/reservations/{reference}", json_body={"party_size": 3})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (409, "cutoff_passed"))


class OutsideTheWindow(CancelCase):
    def test_cancelling_outside_the_window_is_allowed(self):
        # The default fixture cutoff is 120 minutes and the booking is 7 days out.
        reference = self.create(self.ada, "t_2", "19:00")
        self.assertEqual(self.cancel(self.ada, reference).status, 200)


class Authorization(CancelCase):
    """§5:52 and §6:70 -- authentication first, then ownership, and neither leaks."""

    def test_an_unknown_reference_is_not_found(self):
        resp = self.cancel(self.ada, "ZZZZZZ")
        self.assertEqual((resp.status, resp.json["error"]["code"]), (404, "not_found"))

    def test_another_callers_reservation_is_not_found_not_forbidden(self):
        reference = self.create(self.bob, "t_2", "19:00", key="k-bob")
        resp = self.cancel(self.ada, reference)
        self.assertEqual((resp.status, resp.json["error"]["code"]), (404, "not_found"))

    def test_another_callers_reservation_is_not_cancelled_by_the_attempt(self):
        reference = self.create(self.bob, "t_2", "19:00", key="k-bob")
        self.cancel(self.ada, reference)
        self.assertEqual(self.current(self.bob, reference)["status"], "confirmed")

    def test_a_missing_token_is_unauthenticated(self):
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.cancel(self.ada, reference, token=None)
        self.assertEqual((resp.status, resp.json["error"]["code"]), (401, "unauthenticated"))

    def test_an_unknown_token_is_answered_as_a_missing_one(self):
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.cancel(self.ada, reference, token="not-a-real-token")
        self.assertEqual((resp.status, resp.json["error"]["code"]), (401, "unauthenticated"))

    def test_a_missing_token_leaves_the_booking_alone(self):
        reference = self.create(self.ada, "t_3", "18:00")
        self.cancel(self.ada, reference, token=None)
        self.assertEqual(self.current(self.ada, reference)["status"], "confirmed")
        self.assertEqual(self.book(self.bob, "t_3", "18:00", key="k-probe").status, 409)


class IdempotencyHeader(CancelCase):
    """:137 and §7:77 -- the key is required on create and moves only, so not here."""

    def test_no_idempotency_key_is_required(self):
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.cancel(self.ada, reference)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["status"], "cancelled")

    def test_a_key_already_used_by_a_create_is_not_treated_as_a_replay(self):
        reference = self.create(self.ada, "t_2", "19:00", key="shared")
        resp = self.ada.request("POST", f"/reservations/{reference}/cancel",
                                headers={"Idempotency-Key": "shared"})
        self.assertEqual(resp.status, 200, resp.raw)
        self.assertEqual(resp.json["status"], "cancelled")

    def test_an_over_length_key_is_rejected(self):
        """§5:60 states the 1..255 rule without scoping it to the endpoints that demand one."""
        reference = self.create(self.ada, "t_2", "19:00")
        resp = self.ada.request("POST", f"/reservations/{reference}/cancel",
                                headers={"Idempotency-Key": "k" * 256})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (422, "validation_failed"))
        self.assertEqual(self.current(self.ada, reference)["status"], "confirmed")


class ErrorEnvelope(CancelCase):
    """:47 -- every refusal speaks the same envelope as everything else."""

    def test_every_refusal_is_the_error_envelope(self):
        confirmed = self.create(self.ada, "t_2", "19:00", key="k-mine")
        foreign = self.create(self.bob, "t_3", "19:00", key="k-theirs")
        cases = [
            (self.cancel(self.ada, "ZZZZZZ"), 404),
            (self.cancel(self.ada, foreign), 404),
            (self.cancel(self.ada, confirmed, token=None), 401),
        ]
        for resp, status in cases:
            with self.subTest(status=status):
                self.assertEqual(resp.status, status)
                self.assertEqual(sorted(resp.json), ["error"])
                self.assertEqual(sorted(resp.json["error"]), ["code", "message"])
                self.assertIsInstance(resp.json["error"]["code"], str)
                self.assertIsInstance(resp.json["error"]["message"], str)


class AfterCancellation(CancelCase):
    """What the rest of the surface does with a cancelled booking."""

    def test_a_cancelled_booking_cannot_be_amended(self):
        """:141 -- cancelled is 409 reservation_cancelled, and cancelling is how you get there."""
        reference = self.create(self.ada, "t_2", "19:00")
        self.cancel(self.ada, reference)
        resp = self.ada.request("PATCH", f"/reservations/{reference}", json_body={"party_size": 3})
        self.assertEqual((resp.status, resp.json["error"]["code"]), (409, "reservation_cancelled"))

    def test_a_cancelled_booking_is_still_readable_by_its_owner(self):
        """:134 lists confirmed and cancelled alike in the caller's reservations."""
        reference = self.create(self.ada, "t_2", "19:00")
        self.cancel(self.ada, reference)
        self.assertEqual(self.current(self.ada, reference)["reference"], reference)

    def test_a_cancelled_booking_is_still_not_visible_to_another_caller(self):
        reference = self.create(self.ada, "t_2", "19:00")
        self.cancel(self.ada, reference)
        resp = self.bob.get(f"/reservations/{reference}")
        self.assertEqual((resp.status, resp.json["error"]["code"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()