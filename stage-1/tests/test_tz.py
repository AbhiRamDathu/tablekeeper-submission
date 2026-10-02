"""Unit 0: DST resolution and absolute-time occupancy (spec §9 and §1).

Pure functions, so this module needs no server and no database. That is deliberate: these are the
rules the rest of the service is built on, and they should fail loudly in isolation rather than as
a puzzling availability bug.
"""
from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from app.intervals import overlaps, slot_end
from app.tz import (
    InvalidLocalTime,
    NonExistentLocalTime,
    is_ambiguous,
    is_nonexistent,
    parse_local,
    resolve,
)

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")

# The four named transitions from the acceptance criteria.
SPRING_FORWARD = [
    ("Europe/Berlin", "2026-03-29T02:30"),
    ("America/New_York", "2026-03-08T02:30"),
]
FALL_BACK = [
    ("Europe/Berlin", "2026-10-25T02:30"),
    ("America/New_York", "2026-11-01T01:30"),
]


class ParseLocal(unittest.TestCase):
    def test_parses_the_shape_the_api_accepts(self):
        self.assertEqual(parse_local("2026-10-09T18:30"), dt.datetime(2026, 10, 9, 18, 30))

    def test_rejects_shapes_that_are_not_local_timestamps(self):
        for bad in ["2026-10-09", "2026-10-09T18:30:00", "18:30", "2026-10-09 18:30", "", "x"]:
            with self.subTest(bad=bad), self.assertRaises(InvalidLocalTime):
                parse_local(bad)

    def test_rejects_impossible_calendar_values(self):
        for bad in ["2026-02-30T19:00", "2026-13-01T19:00", "2026-10-09T25:00"]:
            with self.subTest(bad=bad), self.assertRaises(InvalidLocalTime):
                parse_local(bad)

    def test_rejects_non_strings(self):
        for bad in [17, None, 20261009, ["2026-10-09T19:00"]]:
            with self.subTest(bad=bad), self.assertRaises(InvalidLocalTime):
                parse_local(bad)


class SpringForward(unittest.TestCase):
    """The clock skips an hour: the skipped wall time never happened."""

    def test_the_skipped_hour_is_detected(self):
        for zone_name, value in SPRING_FORWARD:
            with self.subTest(zone=zone_name):
                naive = parse_local(value)
                self.assertTrue(is_nonexistent(naive, ZoneInfo(zone_name)))

    def test_the_skipped_hour_is_rejected_rather_than_shifted(self):
        """Silently moving a guest forward an hour would book a time they did not ask for."""
        for zone_name, value in SPRING_FORWARD:
            with self.subTest(zone=zone_name), self.assertRaises(NonExistentLocalTime):
                resolve(parse_local(value), ZoneInfo(zone_name))

    def test_the_hour_before_the_gap_is_unaffected(self):
        self.assertEqual(
            resolve(parse_local("2026-03-29T01:30"), BERLIN).utcoffset(), dt.timedelta(hours=1))

    def test_the_hour_after_the_gap_is_unaffected(self):
        self.assertEqual(
            resolve(parse_local("2026-03-29T03:30"), BERLIN).utcoffset(), dt.timedelta(hours=2))


class FallBack(unittest.TestCase):
    """The clock repeats an hour: the wall time happens twice, and §9 picks the first."""

    def test_the_repeated_hour_is_detected_as_ambiguous(self):
        for zone_name, value in FALL_BACK:
            with self.subTest(zone=zone_name):
                self.assertTrue(is_ambiguous(parse_local(value), ZoneInfo(zone_name)))

    def test_ambiguous_and_nonexistent_are_disjoint(self):
        for zone_name, value in FALL_BACK:
            naive = parse_local(value)
            self.assertTrue(is_ambiguous(naive, ZoneInfo(zone_name)))
            self.assertFalse(is_nonexistent(naive, ZoneInfo(zone_name)))

    def test_resolution_takes_the_first_occurrence(self):
        """fold=0 is the earlier instant, so the booking lands before the clock is turned back."""
        berlin = resolve(parse_local("2026-10-25T02:30"), BERLIN)
        self.assertEqual(berlin.utcoffset(), dt.timedelta(hours=2))
        self.assertEqual(berlin.astimezone(dt.timezone.utc).hour, 0)

    def test_new_york_takes_the_first_occurrence_too(self):
        new_york = resolve(parse_local("2026-11-01T01:30"), NEW_YORK)
        self.assertEqual(new_york.utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(new_york.astimezone(dt.timezone.utc).hour, 5)

    def test_the_two_occurrences_are_one_hour_apart_in_absolute_time(self):
        """The wall clock says the same thing twice; the instants are an hour apart."""
        naive = parse_local("2026-10-25T02:30")
        first = resolve(naive, BERLIN)
        second = naive.replace(tzinfo=BERLIN, fold=1)
        self.assertEqual(first.utcoffset(), dt.timedelta(hours=2))
        self.assertEqual(second.utcoffset(), dt.timedelta(hours=1))
        self.assertEqual((second.astimezone(dt.timezone.utc)
                          - first.astimezone(dt.timezone.utc)), dt.timedelta(hours=1))


class UnambiguousTimes(unittest.TestCase):
    def test_an_ordinary_winter_evening_resolves_unchanged(self):
        resolved = resolve(parse_local("2026-01-15T19:00"), BERLIN)
        self.assertEqual(resolved.utcoffset(), dt.timedelta(hours=1))
        self.assertEqual(resolved.replace(tzinfo=None), parse_local("2026-01-15T19:00"))

    def test_an_ordinary_summer_evening_resolves_unchanged(self):
        resolved = resolve(parse_local("2026-07-15T19:00"), BERLIN)
        self.assertEqual(resolved.utcoffset(), dt.timedelta(hours=2))


class HalfOpenIntervals(unittest.TestCase):
    def setUp(self):
        self.start = resolve(parse_local("2026-06-01T19:00"), BERLIN)
        self.end = slot_end(self.start, 90)

    def test_a_booking_ends_after_ninety_minutes(self):
        self.assertEqual((self.end - self.start), dt.timedelta(minutes=90))

    def test_a_booking_starting_exactly_at_the_end_does_not_conflict(self):
        """The §1 adjacency case: 19:00-20:30 and a booking starting 20:30 are back to back."""
        later = resolve(parse_local("2026-06-01T20:30"), BERLIN)
        self.assertFalse(overlaps(self.start, self.end, later, slot_end(later, 90)))

    def test_a_booking_starting_one_minute_before_the_end_does_conflict(self):
        earlier = resolve(parse_local("2026-06-01T20:29"), BERLIN)
        self.assertTrue(overlaps(self.start, self.end, earlier, slot_end(earlier, 90)))

    def test_a_booking_starting_inside_the_interval_conflicts(self):
        inner = resolve(parse_local("2026-06-01T19:30"), BERLIN)
        self.assertTrue(overlaps(self.start, self.end, inner, slot_end(inner, 90)))

    def test_a_fully_enclosing_booking_conflicts(self):
        outer_start = resolve(parse_local("2026-06-01T18:00"), BERLIN)
        outer_end = slot_end(outer_start, 180)
        self.assertTrue(overlaps(self.start, self.end, outer_start, outer_end))
        self.assertTrue(overlaps(outer_start, outer_end, self.start, self.end))

    def test_separate_intervals_do_not_conflict(self):
        later = resolve(parse_local("2026-06-01T21:00"), BERLIN)
        self.assertFalse(overlaps(self.start, self.end, later, slot_end(later, 90)))

    def test_naive_datetimes_are_refused(self):
        """Ordering two naive values across a clock change is the bug this module prevents."""
        with self.assertRaises(ValueError):
            overlaps(dt.datetime(2026, 6, 1, 19, 0), dt.datetime(2026, 6, 1, 20, 30),
                     self.end, self.end)


if __name__ == "__main__":
    unittest.main()