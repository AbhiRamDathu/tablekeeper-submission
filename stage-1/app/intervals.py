"""Absolute-time occupancy intervals for Tablekeeper Stage 1 (spec §1).

A reservation occupies the half-open interval `[start, end)`: it covers the instant it begins and
stops covering the instant it ends. Half-open is the only choice that makes back-to-back bookings
work, and §1 depends on it directly — a 90-minute booking at 19:00 occupies 19:00 up to but not
including 20:30, so a 20:30 booking does not conflict with it.

Every comparison here takes aware datetimes. Naive local times are deliberately not accepted: two
naive values cannot be ordered correctly across a clock change, which is the exact bug this module
exists to prevent.
"""
from __future__ import annotations

import datetime as dt

__all__ = ["overlaps", "slot_end", "format_minutes"]


def overlaps(
    a_start: dt.datetime,
    a_end: dt.datetime,
    b_start: dt.datetime,
    b_end: dt.datetime,
) -> bool:
    """True when `[a_start, a_end)` and `[b_start, b_end)` share any instant.

    Touching endpoints do not overlap: `a_end == b_start` is back-to-back, not a conflict.

    All four arguments are normalised to UTC before comparing, and that is load-bearing rather than
    tidiness. Python compares two aware datetimes that share one `tzinfo` object on their *wall
    fields*: `datetime(2026,10,25,2,30, tzinfo=Berlin, fold=0) == datetime(2026,10,25,2,30,
    tzinfo=Berlin, fold=1)` is `True`, and `x < y` is `False` even though `y` is the later instant.
    Comparing those directly reports a one-hour overlap as no overlap, which withholds the second
    occurrence of a repeated hour -- a free slot, lost. Converting first makes that state
    unrepresentable rather than merely unreached.
    """
    for moment in (a_start, a_end, b_start, b_end):
        if moment.tzinfo is None:
            raise ValueError("intervals compare absolute times; a naive datetime was given")
    a_start, a_end, b_start, b_end = (moment.astimezone(dt.timezone.utc)
                                      for moment in (a_start, a_end, b_start, b_end))
    return a_start < b_end and b_start < a_end


def slot_end(start: dt.datetime, duration_minutes: int) -> dt.datetime:
    """The exclusive end of a booking of `duration_minutes` starting at `start`.

    Added in absolute time, not by adding minutes to the wall clock, so a booking that spans a
    clock change keeps its real duration instead of gaining or losing the repeated hour. Adding to
    an aware datetime adds to its naive wall fields and keeps the original offset, so
    `2026-03-29T01:30+01:00` plus 90 minutes lands on `03:00` -- 30 minutes later, not 90 -- and
    `2026-10-25T01:30+02:00` lands on `03:00+01:00`, two and a half hours later.

    The duration is applied in UTC and the result is converted back to `start.tzinfo`, because that
    is the zone the guest reads their confirmation in. The offset it carries is then the one actually
    in force at the end, which is what makes §9's fall-back case read `02:00` rather than `03:00`.
    """
    return (start.astimezone(dt.timezone.utc)
            + dt.timedelta(minutes=duration_minutes)).astimezone(start.tzinfo)


def format_minutes(total: int) -> str:
    """`1110` -> `"18:30"`. The inverse of `tz.minutes_of`, for slot labels."""
    return f"{total // 60:02d}:{total % 60:02d}"