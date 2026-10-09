"""Unit 0: DST resolution and absolute-time occupancy (spec §9 and §1).

Pure functions, so this module needs no server and no database. That is deliberate: these are the
rules the rest of the service is built on, and they should fail loudly in isolation rather than as
a puzzling availability bug.

Two defects live here and they are one defect class. `slot_end` does the arithmetic on wall fields
rather than on instants, and `overlaps` compares two aware datetimes that share one `tzinfo` object
on wall fields too. Only a `fold=1` datetime reaches both, which is why `FoldedOverlaps` builds one
by hand: `resolve` always returns `fold=0`, so no public entry point addresses the second occurrence
of a repeated hour.
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
UTC = dt.timezone.utc
NINETY = dt.timedelta(minutes=90)

# The four named transitions from the acceptance criteria.
SPRING_FORWARD = [
    ("Europe/Berlin", "2026-03-29T02:30"),
    ("America/New_York", "2026-03-08T02:30"),
]
FALL_BACK = [
    ("Europe/Berlin", "2026-10-25T02:30"),
    ("America/New_York", "2026-11-01T01:30"),
]


def absolute_gap(earlier: dt.datetime, later: dt.datetime) -> dt.timedelta:
    """`later - earlier` measured between instants.

    Subtracting two aware datetimes that share a `tzinfo` object subtracts *wall fields* instead --
    Python ignores the offset when both sides carry the same zone. That is why the old
    `self.end - self.start` assertion could never fail: its June date has no transition, so the two
    readings coincide. Every duration claim in this file goes through here.
    """
    return later.astimezone(UTC) - earlier.astimezone(UTC)


def shift(moment: dt.datetime, minutes: int) -> dt.datetime:
    """Move `moment` by `minutes` of absolute time, keeping its zone for display."""
    return (moment.astimezone(UTC) + dt.timedelta(minutes=minutes)).astimezone(moment.tzinfo)


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


class SlotEndIsAbsoluteTime(unittest.TestCase):
    """§9: `reservation_duration_minutes` is absolute time, not wall clock.

    A 90-minute booking is 90 minutes. On a transition date that is not the same thing as 90 minutes
    on the clock face: spring forward loses an hour to the gap and fall back gains one from the
    repeated hour. `ends_at` is what the guest is told, so a wall-clock answer here is a wrong time
    printed as though it were right.
    """

    # Anchors chosen to straddle the transitions, plus two ordinary dates as controls. The ordinary
    # cases must keep passing: they are the regression check that the fix did not over-correct into
    # shifting every booking.
    CASES = [
        ("Europe/Berlin", "2026-03-29T01:30", "2026-03-29T04:00:00+02:00", "spring forward"),
        ("Europe/Berlin", "2026-10-25T01:30", "2026-10-25T02:00:00+01:00", "fall back"),
        ("America/New_York", "2026-03-08T01:30", "2026-03-08T04:00:00-04:00", "spring forward"),
        ("America/New_York", "2026-11-01T00:30", "2026-11-01T01:00:00-05:00", "fall back"),
        ("Europe/Berlin", "2026-06-01T19:00", "2026-06-01T20:30:00+02:00", "ordinary summer"),
        ("Europe/Berlin", "2026-01-15T19:00", "2026-01-15T20:30:00+01:00", "ordinary winter"),
    ]

    def test_the_booking_lasts_ninety_absolute_minutes(self):
        """The whole invariant, measured where the bug lives: between instants, not wall fields."""
        for zone_name, start_local, _expected, kind in self.CASES:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                self.assertEqual(
                    NINETY, absolute_gap(start, slot_end(start, 90)),
                    f"{kind} in {zone_name}: a 90-minute booking starting {start_local} does not"
                    " last 90 minutes of absolute time, so the duration depends on the date")

    def test_ends_at_reads_the_time_the_guest_was_promised(self):
        """§9 on the fall-back case, verbatim: "its local `ends_at` reads 02:00, not 03:00"."""
        for zone_name, start_local, expected, kind in self.CASES:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                self.assertEqual(
                    expected, slot_end(start, 90).isoformat(),
                    f"{kind} in {zone_name}: `ends_at` is wrong, and it is the string the guest"
                    " reads")

    def test_ends_at_carries_the_offset_in_force_when_the_booking_ends(self):
        """The end offset is not the start offset when the transition falls inside the booking."""
        for zone_name, start_local, expected, kind in self.CASES:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                self.assertEqual(
                    dt.datetime.fromisoformat(expected).utcoffset(), slot_end(start, 90).utcoffset(),
                    f"{kind} in {zone_name}: `ends_at` keeps the start's offset instead of the one"
                    " in force at the end")


class FoldedOverlaps(unittest.TestCase):
    """Two bookings that read the same on the clock and are an hour apart.

    Built by hand because `resolve` always returns `fold=0` (`app/tz.py:97`) and nothing in the API
    addresses the second occurrence of a repeated hour. Both halves of the defect are here: the
    arithmetic (`slot_end` must land on two different instants) and the comparison (`overlaps` must
    order them by instant, not by wall fields).
    """

    def setUp(self):
        # 2026-10-25 02:30 happens twice in Berlin: once at +02:00 (00:30Z) and once at +01:00
        # (01:30Z). Same wall fields, same tzinfo object, different instants.
        self.first = dt.datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=0)
        self.second = dt.datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)

    def test_the_two_occurrences_are_one_hour_apart_in_absolute_time(self):
        """The precondition. If this fails, nothing below is measuring anything."""
        self.assertEqual(dt.timedelta(hours=1),
                         absolute_gap(self.first, self.second))
        self.assertEqual(self.first, self.second,
                         "the two folds compare equal on wall fields -- which is exactly why"
                         " `overlaps` must normalise before comparing")

    def test_each_occurrence_gets_its_own_end(self):
        """Wall-clock arithmetic gives both the same `ends_at`, which is impossible for real time."""
        first_end = slot_end(self.first, 60)
        second_end = slot_end(self.second, 60)
        self.assertNotEqual(
            absolute_gap(first_end, second_end), dt.timedelta(0),
            f"both occurrences end at {first_end.isoformat()}, an hour apart in absolute time")

    def test_the_repeated_hour_is_not_withheld_as_a_conflict(self):
        """Two back-to-back bookings either side of the repeated hour do not overlap.

        This is §1's invariant read through the fold hazard: a guest may book the second 02:30,
        because it is a different instant from the first 02:30. Reporting a conflict withholds a
        genuinely free slot.
        """
        self.assertFalse(
            overlaps(self.first, slot_end(self.first, 60),
                     self.second, slot_end(self.second, 60)),
            "the repeated hour is being reported as a conflict, so the free occurrence is withheld")

    def test_the_spring_forward_pair_does_overlap(self):
        """The spring-forward case must keep reporting a conflict.

        A fix that made everything non-overlapping would satisfy the test above by disabling the
        check entirely. Across the spring gap the two bookings genuinely share instants, so this is
        the assertion that the pair above is not a false green.
        """
        early = dt.datetime(2026, 3, 29, 1, 30, tzinfo=BERLIN, fold=0)
        late = dt.datetime(2026, 3, 29, 3, 30, tzinfo=BERLIN, fold=0)
        self.assertTrue(
            overlaps(early, slot_end(early, 90), late, slot_end(late, 90)),
            "bookings spanning the spring gap genuinely overlap and must be reported as such")


# Anchors `HalfOpenIntervals` is pinned to. The transition dates are the point: a class that only
# ever runs where wall-clock and absolute time coincide cannot detect this class of defect, which is
# why the June-only version of this class sat green for so long. June is kept as a control.
INTERVAL_ANCHORS = [
    ("Europe/Berlin", "2026-06-01T19:00", "ordinary summer"),
    ("Europe/Berlin", "2026-01-15T19:00", "ordinary winter"),
    ("Europe/Berlin", "2026-03-29T01:30", "spring forward"),
    ("Europe/Berlin", "2026-10-25T01:30", "fall back"),
    ("America/New_York", "2026-11-01T00:30", "fall back"),
]


class HalfOpenIntervals(unittest.TestCase):
    """§1 adjacency and containment, re-pinned onto the dates where the two readings disagree."""

    def test_a_booking_ends_after_ninety_minutes(self):
        """Compared in absolute time. The old wall-field subtraction was inert on every date."""
        for zone_name, start_local, kind in INTERVAL_ANCHORS:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                self.assertEqual(NINETY, absolute_gap(start, slot_end(start, 90)))

    def test_a_booking_starting_exactly_at_the_end_does_not_conflict(self):
        """The §1 adjacency case: 19:00-20:30 and a booking starting 20:30 are back to back."""
        for zone_name, start_local, kind in INTERVAL_ANCHORS:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                end = slot_end(start, 90)
                later = shift(end, 0)
                self.assertFalse(overlaps(start, end, later, slot_end(later, 90)))

    def test_a_booking_starting_one_minute_before_the_end_does_conflict(self):
        for zone_name, start_local, kind in INTERVAL_ANCHORS:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                end = slot_end(start, 90)
                earlier = shift(end, -1)
                self.assertTrue(overlaps(start, end, earlier, slot_end(earlier, 90)))

    def test_a_booking_starting_inside_the_interval_conflicts(self):
        for zone_name, start_local, kind in INTERVAL_ANCHORS:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                end = slot_end(start, 90)
                inner = shift(start, 30)
                self.assertTrue(overlaps(start, end, inner, slot_end(inner, 90)))

    def test_a_fully_enclosing_booking_conflicts(self):
        for zone_name, start_local, kind in INTERVAL_ANCHORS:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                end = slot_end(start, 90)
                outer_start = shift(start, -60)
                self.assertTrue(overlaps(start, end, outer_start, slot_end(outer_start, 180)))
                self.assertTrue(overlaps(outer_start, slot_end(outer_start, 180), start, end))

    def test_separate_intervals_do_not_conflict(self):
        for zone_name, start_local, kind in INTERVAL_ANCHORS:
            with self.subTest(zone=zone_name, start=start_local, kind=kind):
                start = resolve(parse_local(start_local), ZoneInfo(zone_name))
                end = slot_end(start, 90)
                later = shift(end, 30)
                self.assertFalse(overlaps(start, end, later, slot_end(later, 90)))

    def test_naive_datetimes_are_refused(self):
        """Ordering two naive values across a clock change is the bug this module prevents."""
        start = resolve(parse_local("2026-06-01T19:00"), BERLIN)
        end = slot_end(start, 90)
        with self.assertRaises(ValueError):
            overlaps(dt.datetime(2026, 6, 1, 19, 0), dt.datetime(2026, 6, 1, 20, 30), end, end)


if __name__ == "__main__":
    unittest.main()