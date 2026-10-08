"""Units 1-4: the HTTP surface — health, reset, auth, public reads, availability, booking.

Everything here runs against the real server over a real socket, started in a thread by
`tests.support`. No mocks: the point is to exercise routing, status codes, the error envelope and
SQLite concurrency exactly as the harness will.
"""
from __future__ import annotations

import threading
import unittest

from tests.support import (
    ADA,
    BOB,
    Client,
    all_week,
    book,
    expected_slots,
    fixture,
    local,
    restaurant,
    service,
    weekday_of,
    world,
)


class Health(unittest.TestCase):
    def test_health_is_ok(self):
        with service() as base_url:
            resp = Client(base_url).get("/health")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json, {"status": "ok"})

    def test_health_needs_no_authentication(self):
        with service() as base_url:
            self.assertEqual(Client(base_url, token=None).get("/health").status, 200)


class Reset(unittest.TestCase):
    def test_reset_needs_no_authentication(self):
        with service() as base_url:
            self.assertEqual(
                Client(base_url, token=None).post("/_test/reset", json_body=fixture()).status, 204)

    def test_reset_can_be_repeated(self):
        with service() as base_url:
            anon = Client(base_url)
            for _ in range(3):
                self.assertEqual(anon.post("/_test/reset", json_body=fixture()).status, 204)

    def test_reset_leaves_only_the_supplied_fixture(self):
        with service() as base_url:
            anon = Client(base_url)
            anon.post("/_test/reset", json_body=fixture())
            self.assertEqual(anon.login(ADA["email"], ADA["password"]).status, 200)
            anon.post("/_test/reset", json_body=fixture(
                users=[ADA], restaurants=[restaurant("r_other", name="Other")]))
            self.assertEqual(anon.login(ADA["email"], ADA["password"]).status, 200)
            self.assertEqual(anon.login(BOB["email"], BOB["password"]).status, 401)
            self.assertEqual(anon.get("/restaurants/r_anker").status, 404)

    def test_reset_rejects_a_body_that_is_not_an_object(self):
        with service() as base_url:
            resp = Client(base_url).post("/_test/reset", json_body=["not", "an", "object"])
            self.assertEqual(resp.status, 400)
            self.assertEqual(resp.code, "malformed_request")


class Authentication(unittest.TestCase):
    def test_a_seeded_user_can_log_in_immediately(self):
        with world() as w:
            self.assertEqual(w.anon.login(ADA["email"], ADA["password"]).status, 200)

    def test_login_returns_a_usable_token(self):
        with world() as w:
            resp = w.anon.login(ADA["email"], ADA["password"])
            self.assertIsInstance(resp.json["token"], str)
            self.assertTrue(resp.json["token"])
            self.assertEqual(Client(w.base_url, resp.json["token"]).get("/reservations").status, 200)

    def test_signup_creates_an_account(self):
        with world() as w:
            resp = w.anon.request("POST", "/auth/signup", json_body={
                "email": "new@example.com", "password": "correct horse",
                "display_name": "New"}, token=None)
            self.assertEqual(resp.status, 201)
            self.assertEqual(Client(w.base_url).login("new@example.com", "correct horse").status, 200)

    def test_signup_twice_is_email_taken(self):
        with world() as w:
            resp = w.anon.request("POST", "/auth/signup", json_body={
                "email": ADA["email"], "password": "correct horse", "display_name": "X"},
                token=None)
            self.assertEqual((resp.status, resp.code), (409, "email_taken"))

    def test_signup_value_rejections_are_validation_failures(self):
        cases = [("short@example.com", "1234567"), ("not-an-email", "correct horse"),
                 ("a@b.co", "")]
        with world() as w:
            for email, password in cases:
                with self.subTest(email=email, password=password):
                    resp = w.anon.request("POST", "/auth/signup", json_body={
                        "email": email, "password": password, "display_name": "X"}, token=None)
                    self.assertEqual((resp.status, resp.code), (422, "validation_failed"))

    def test_signup_type_errors_are_malformed_not_validation(self):
        """A field of the wrong JSON type is 400; a bad value is 422. §5 keeps them apart."""
        with world() as w:
            resp = w.anon.request("POST", "/auth/signup", json_body={
                "email": 17, "password": "correct horse", "display_name": "X"}, token=None)
            self.assertEqual((resp.status, resp.code), (400, "malformed_request"))

    def test_login_failures_are_401_and_do_not_distinguish_cause(self):
        with world() as w:
            unknown_user = w.anon.login("nobody@example.com", "correct horse")
            wrong_password = w.anon.login(ADA["email"], "wrong password")
            for resp in (unknown_user, wrong_password):
                self.assertEqual((resp.status, resp.code), (401, "unauthenticated"))
            self.assertEqual(unknown_user.json["error"]["message"],
                             wrong_password.json["error"]["message"])

    def test_passwords_are_not_stored_in_the_clear(self):
        with world() as w:
            from app import store

            conn = store.connect()
            try:
                stored = [row["password_hash"] for row in conn.execute(
                    "SELECT password_hash FROM users").fetchall()]
            finally:
                conn.close()
            self.assertTrue(stored)
            for value in stored:
                self.assertNotIn(ADA["password"], value)
                self.assertNotIn(BOB["password"], value)
                self.assertTrue(value.startswith("pbkdf2_sha256$"))

    def test_two_users_get_different_hashes_for_the_same_password(self):
        """Both fixtures share one password, so equal hashes would mean no salt."""
        with world() as w:
            from app import store

            conn = store.connect()
            try:
                hashes = {row["password_hash"] for row in conn.execute(
                    "SELECT password_hash FROM users").fetchall()}
            finally:
                conn.close()
            self.assertEqual(len(hashes), 2)


class ProtectedEndpoints(unittest.TestCase):
    PATHS = ["/reservations", "/reservations/ABC123"]

    def test_missing_token_is_rejected(self):
        with world() as w:
            for path in self.PATHS:
                with self.subTest(path=path):
                    resp = Client(w.base_url, token=None).get(path)
                    self.assertEqual((resp.status, resp.code), (401, "unauthenticated"))

    def test_unknown_token_is_rejected_identically(self):
        with world() as w:
            resp = Client(w.base_url, "not-a-real-token").get("/reservations")
            self.assertEqual((resp.status, resp.code), (401, "unauthenticated"))

    def test_a_valid_token_is_accepted(self):
        with world() as w:
            self.assertEqual(w.ada.get("/reservations").status, 200)


class PublicReads(unittest.TestCase):
    def test_restaurant_detail_is_public_and_carries_the_fixture_shape(self):
        with world() as w:
            resp = w.anon.get(f"/restaurants/{w.rid}")
            self.assertEqual(resp.status, 200)
            body = resp.json
            for field in ("slot_minutes", "reservation_duration_minutes",
                          "cancellation_cutoff_minutes"):
                self.assertEqual(body[field], w.fixture["restaurants"][0][field], field)
            self.assertEqual({t["id"] for t in body["tables"]}, set(w.tables))
            self.assertEqual(len(body["opening_hours"]), 7)

    def test_unknown_restaurant_is_404(self):
        with world() as w:
            resp = w.anon.get("/restaurants/r_nope")
            self.assertEqual((resp.status, resp.code), (404, "not_found"))

    def test_unknown_route_is_404(self):
        with world() as w:
            self.assertEqual(w.anon.get("/nope").status, 404)


class Availability(unittest.TestCase):
    def query(self, client, rid, date, party_size, **extra):
        params = {"restaurant_id": rid, "date": date, "party_size": party_size}
        params.update(extra)
        return client.get("/availability", params=params)

    def test_slots_follow_opening_hours_and_the_duration(self):
        with world() as w:
            slots = self.query(w.anon, w.rid, w.date, 2).json["slots"]
            self.assertEqual([s["starts_at_local"].split("T")[1] for s in slots],
                             expected_slots())

    def test_starts_at_local_is_naive_and_starts_at_carries_an_offset(self):
        with world() as w:
            slot = self.query(w.anon, w.rid, w.date, 2).json["slots"][0]
            self.assertEqual(slot["starts_at_local"], local(w.date, "18:00"))
            self.assertTrue(slot["starts_at"].startswith(f"{w.date}T18:00:00"))
            self.assertIn("+", slot["starts_at"])

    def test_every_slot_lists_the_tables_that_fit_the_party(self):
        with world() as w:
            slot = self.query(w.anon, w.rid, w.date, 2).json["slots"][0]
            self.assertEqual(slot["available_table_ids"], ["t_1", "t_2", "t_3"])

    def test_a_party_of_four_excludes_the_two_seater(self):
        with world() as w:
            slot = self.query(w.anon, w.rid, w.date, 4).json["slots"][0]
            self.assertEqual(slot["available_table_ids"], ["t_2", "t_3"])

    def test_a_booked_table_leaves_its_own_and_every_overlapping_slot(self):
        """90 minutes on a 30 minute grid covers three slots either side of the start."""
        with world() as w:
            self.assertEqual(book(w, table_id="t_2", at="19:00").status, 201)
            by_time = {s["starts_at_local"].split("T")[1]: s["available_table_ids"]
                       for s in self.query(w.anon, w.rid, w.date, 4).json["slots"]}
            for overlapping in ("18:00", "18:30", "19:00", "19:30", "20:00"):
                self.assertNotIn("t_2", by_time[overlapping], overlapping)
            self.assertIn("t_2", by_time["20:30"], "20:30 must not overlap 19:00-20:30")

    def test_a_slot_with_no_free_table_still_appears(self):
        with world() as w:
            for table in ("t_2", "t_3"):
                self.assertEqual(book(w, table_id=table, at="19:00").status, 201)
            slot = next(s for s in self.query(w.anon, w.rid, w.date, 4).json["slots"]
                        if s["starts_at_local"].endswith("19:00"))
            self.assertEqual(slot["available_table_ids"], [])

    def test_a_closed_weekday_returns_no_slots(self):
        with service() as base_url:
            anon = Client(base_url)
            date = None
            from tests.support import booking_date

            date = booking_date()
            closed = [h for h in all_week() if h["weekday"] != weekday_of(date)]
            anon.post("/_test/reset", json_body=fixture(
                restaurants=[restaurant(opening_hours=closed)]))
            resp = anon.get("/availability", params={
                "restaurant_id": "r_anker", "date": date, "party_size": 2})
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json["slots"], [])

    def test_each_of_the_three_parameters_is_required(self):
        with world() as w:
            for drop in ("restaurant_id", "date", "party_size"):
                with self.subTest(drop=drop):
                    params = {"restaurant_id": w.rid, "date": w.date, "party_size": 2}
                    params.pop(drop)
                    resp = w.anon.get("/availability", params=params)
                    self.assertEqual((resp.status, resp.code), (422, "validation_failed"))

    def test_unparseable_dates_are_validation_failures(self):
        with world() as w:
            for date in ("2026-02-30", "not-a-date", "24-09-2026"):
                with self.subTest(date=date):
                    resp = self.query(w.anon, w.rid, date, 2)
                    self.assertEqual((resp.status, resp.code), (422, "validation_failed"))

    def test_bad_party_sizes_are_validation_failures(self):
        with world() as w:
            for party in ("0", "-1", "abc", "1e9"):
                with self.subTest(party=party):
                    resp = self.query(w.anon, w.rid, w.date, party)
                    self.assertEqual((resp.status, resp.code), (422, "validation_failed"))

    def test_unknown_restaurant_is_404(self):
        with world() as w:
            resp = self.query(w.anon, "r_nope", w.date, 2)
            self.assertEqual((resp.status, resp.code), (404, "not_found"))

    def test_unknown_query_parameters_are_ignored(self):
        with world() as w:
            resp = self.query(w.anon, w.rid, w.date, 2, sort="whatever", page="3")
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.json["slots"])


class Booking(unittest.TestCase):
    def test_a_booking_is_created(self):
        with world() as w:
            resp = book(w)
            self.assertEqual(resp.status, 201)
            body = resp.json
            self.assertEqual(body["restaurant_id"], w.rid)
            self.assertEqual(body["table_id"], "t_2")
            self.assertEqual(body["party_size"], 4)
            self.assertEqual(body["status"], "confirmed")
            self.assertEqual(body["reference"], body["reservation_id"])
            self.assertTrue(body["starts_at"].startswith(w.date))

    def test_a_missing_idempotency_key_is_rejected(self):
        with world() as w:
            resp = w.ada.post("/reservations", json_body={
                "restaurant_id": w.rid, "table_id": "t_2",
                "starts_at_local": local(w.date), "party_size": 4})
            self.assertEqual((resp.status, resp.code), (400, "missing_idempotency_key"))

    def test_an_empty_idempotency_key_is_rejected(self):
        with world() as w:
            resp = w.ada.post("/reservations", json_body={
                "restaurant_id": w.rid, "table_id": "t_2",
                "starts_at_local": local(w.date), "party_size": 4},
                headers={"Idempotency-Key": ""})
            self.assertEqual((resp.status, resp.code), (400, "missing_idempotency_key"))

    def test_an_out_of_range_key_length_is_a_validation_failure(self):
        with world() as w:
            for key in ("x" * 256,):
                with self.subTest(length=len(key)):
                    resp = w.ada.post("/reservations", json_body={
                        "restaurant_id": w.rid, "table_id": "t_2",
                        "starts_at_local": local(w.date), "party_size": 4},
                        headers={"Idempotency-Key": key})
                    self.assertEqual((resp.status, resp.code), (422, "validation_failed"))

    def test_replay_returns_the_original_body_with_200(self):
        with world() as w:
            body = {"restaurant_id": w.rid, "table_id": "t_2",
                    "starts_at_local": local(w.date, "19:00"), "party_size": 4}
            first = w.ada.post("/reservations", json_body=body,
                               headers={"Idempotency-Key": "replay-1"})
            second = w.ada.post("/reservations", json_body=body,
                                headers={"Idempotency-Key": "replay-1"})
            self.assertEqual(first.status, 201)
            self.assertEqual(second.status, 200)
            self.assertEqual(first.json, second.json)

    def test_the_operation_is_applied_once_across_a_replay(self):
        with world() as w:
            body = {"restaurant_id": w.rid, "table_id": "t_2",
                    "starts_at_local": local(w.date, "19:00"), "party_size": 4}
            for _ in range(3):
                w.ada.post("/reservations", json_body=body,
                           headers={"Idempotency-Key": "apply-once"})
            listed = w.ada.get("/reservations").json
            self.assertEqual(len(listed["reservations"]), 1)

    def test_reusing_a_key_with_a_different_body_is_rejected(self):
        with world() as w:
            w.ada.post("/reservations", json_body={
                "restaurant_id": w.rid, "table_id": "t_2",
                "starts_at_local": local(w.date, "19:00"), "party_size": 4},
                headers={"Idempotency-Key": "reused"})
            resp = w.ada.post("/reservations", json_body={
                "restaurant_id": w.rid, "table_id": "t_3",
                "starts_at_local": local(w.date, "19:00"), "party_size": 4},
                headers={"Idempotency-Key": "reused"})
            self.assertEqual((resp.status, resp.code), (409, "idempotency_key_reuse"))

    def test_keys_are_scoped_per_user(self):
        """One user's key must not answer another user's request.

        Bob books a different slot, so a 409 could only come from the key being shared rather
        than from the table genuinely being taken.
        """
        with world() as w:
            ada_body = {"restaurant_id": w.rid, "table_id": "t_2",
                        "starts_at_local": local(w.date, "19:00"), "party_size": 4}
            bob_body = {"restaurant_id": w.rid, "table_id": "t_2",
                        "starts_at_local": local(w.date, "21:00"), "party_size": 4}
            self.assertEqual(
                w.ada.post("/reservations", json_body=ada_body,
                           headers={"Idempotency-Key": "shared"}).status, 201)
            self.assertEqual(
                w.bob.post("/reservations", json_body=bob_body,
                           headers={"Idempotency-Key": "shared"}).status, 201)
            self.assertNotEqual(w.ada.get("/reservations").json["reservations"][0]["reference"],
                                w.bob.get("/reservations").json["reservations"][0]["reference"])

    def test_double_booking_the_same_table_is_a_conflict(self):
        with world() as w:
            self.assertEqual(book(w, table_id="t_2", at="19:00").status, 201)
            resp = book(w, table_id="t_2", at="19:00", key="second-attempt")
            self.assertEqual((resp.status, resp.code), (409, "table_unavailable"))

    def test_a_table_too_small_for_the_party_is_a_validation_failure(self):
        with world() as w:
            resp = book(w, table_id="t_1", party_size=5, key="too-big")
            self.assertEqual((resp.status, resp.code), (422, "party_exceeds_capacity"))

    def test_a_nonexistent_local_time_is_rejected(self):
        """2026-03-29T02:30 never happens in Europe/Berlin, so it must not be bookable."""
        with service() as base_url:
            anon = Client(base_url)
            anon.post("/_test/reset", json_body=fixture(
                restaurants=[restaurant(timezone="Europe/Berlin")]))
            resp = anon.authenticate(ADA["email"], ADA["password"]).post("/reservations", json_body={
                "restaurant_id": "r_anker", "table_id": "t_2",
                "starts_at_local": "2026-03-29T02:30", "party_size": 4},
                headers={"Idempotency-Key": "dst-gap"})
            self.assertEqual((resp.status, resp.code), (422, "invalid_local_time"))

    def test_a_field_of_the_wrong_type_is_malformed(self):
        with world() as w:
            resp = w.ada.post("/reservations", json_body={
                "restaurant_id": w.rid, "table_id": "t_2",
                "starts_at_local": local(w.date), "party_size": "four"},
                headers={"Idempotency-Key": "wrong-type"})
            # §5:172-174 names wrong-typed `party_size` as 422 `validation_failed`; only the other
            # create fields stay 400 under §5:48's generic wrong-type row.
            self.assertEqual((resp.status, resp.code), (422, "validation_failed"))

    def test_an_unknown_table_is_not_found(self):
        """A `table_id` that names no table at this restaurant is 404, not 422.

        It was 422 `validation_failed` until the table lookup stopped being a field-shape check.
        The distinction is §5's: 422 is for "a stated rule about a field is violated", and
        `table_id: "t_nope"` violates no rule -- it is well-formed and simply names nothing. §5
        assigns 404 `not_found` to a resource that does not exist, and every other lookup in the
        service already answered that way (`no such restaurant`, `no such reservation`). This test
        and the one-line product change it corrects moved in one commit on purpose: either alone,
        one of them fails.
        """
        with world() as w:
            resp = book(w, table_id="t_nope", key="no-table")
            self.assertEqual((resp.status, resp.code), (404, "not_found"))


class Concurrency(unittest.TestCase):
    def test_fifty_concurrent_requests_produce_no_5xx(self):
        """The spec's stated limit. A single-threaded server fails this before any logic runs."""
        with world() as w:
            results: list[int] = []
            lock = threading.Lock()

            def hit() -> None:
                client = Client(w.base_url)
                try:
                    resp = client.get("/availability", params={
                        "restaurant_id": w.rid, "date": w.date, "party_size": 2})
                    code = resp.status
                except Exception:  # noqa: BLE001
                    code = 599
                with lock:
                    results.append(code)

            threads = [threading.Thread(target=hit) for _ in range(50)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertEqual(len(results), 50)
            self.assertTrue(all(code == 200 for code in results),
                            f"non-200 statuses: {sorted(set(results))}")

    def test_concurrent_identical_requests_book_exactly_once(self):
        """The §7 burst: one 201, the rest replays, and one booking in the database."""
        with world() as w:
            body = {"restaurant_id": w.rid, "table_id": "t_2",
                    "starts_at_local": local(w.date, "19:00"), "party_size": 4}
            statuses: list[int] = []
            lock = threading.Lock()

            def attempt() -> None:
                client = Client(w.base_url, w.ada.token)
                resp = client.post("/reservations", json_body=body,
                                   headers={"Idempotency-Key": "burst"})
                with lock:
                    statuses.append(resp.status)

            threads = [threading.Thread(target=attempt) for _ in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

            self.assertEqual(statuses.count(201), 1, statuses)
            self.assertEqual(statuses.count(200), 9, statuses)
            self.assertEqual(len(w.ada.get("/reservations").json["reservations"]), 1)


if __name__ == "__main__":
    unittest.main()