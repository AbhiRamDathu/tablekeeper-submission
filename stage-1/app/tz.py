"""Local-time resolution for Tablekeeper Stage 1 (spec §9).

Pure functions. No HTTP, no database, no third-party imports.

The API takes `starts_at_local` as a naive local timestamp (`YYYY-MM-DDTHH:MM`) because the
restaurant's wall clock is what a guest means. Turning that into an instant is the whole job of
this module, and it has exactly one hard case: a local time that occurs twice, or not at all, when
the clock changes.

`zoneinfo` already encodes both facts, so we read them off it rather than maintaining an offset
table that would silently rot the next time a jurisdiction changes its rules:

  * **Ambiguous** (autumn, clock repeats an hour): `fold=0` and `fold=1` carry different offsets.
    §9 resolves to the FIRST occurrence, which is `fold=0`.
  * **Non-existent** (spring, clock skips an hour): round-tripping through UTC lands on a different
    wall time than we started with. That local time never happened and must not be silently
    shifted, so it is rejected rather than invented.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

__all__ = [
    "InvalidLocalTime",
    "NonExistentLocalTime",
    "parse_local",
    "is_ambiguous",
    "is_nonexistent",
    "resolve",
    "format_instant",
    "minutes_of",
]

_LOCAL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$")


class InvalidLocalTime(ValueError):
    """The value is not a `YYYY-MM-DDTHH:MM` local timestamp."""


class NonExistentLocalTime(ValueError):
    """The local time falls inside a spring-forward gap and never occurred."""


def parse_local(value: object) -> dt.datetime:
    """Parse `YYYY-MM-DDTHH:MM` into a naive datetime.

    Rejects non-strings, wrong shapes and impossible calendar values alike: to a caller all three
    are the same mistake, so they raise one error and the endpoint maps it to one status.
    """
    if not isinstance(value, str):
        raise InvalidLocalTime("starts_at_local must be a string")
    match = _LOCAL_RE.match(value)
    if match is None:
        raise InvalidLocalTime("starts_at_local must look like YYYY-MM-DDTHH:MM")
    year, month, day, hour, minute = (int(group) for group in match.groups())
    try:
        return dt.datetime(year, month, day, hour, minute)
    except ValueError as exc:
        raise InvalidLocalTime(str(exc)) from exc


def _round_trips(naive: dt.datetime, tz: ZoneInfo) -> bool:
    """True when `naive` survives a UTC round trip unchanged.

    This is the discriminator between the two hard cases, and it is the *only* reliable one.
    Comparing `fold=0` against `fold=1` offsets does not work: PEP 495 gives a spring-forward gap
    two different offsets too, so an offset comparison reports every gap as ambiguous. A gap is
    pushed forward by the round trip; an ambiguous time is not.
    """
    there = naive.replace(tzinfo=tz, fold=0).astimezone(dt.timezone.utc).astimezone(tz)
    return there.replace(tzinfo=None) == naive


def is_nonexistent(naive: dt.datetime, tz: ZoneInfo) -> bool:
    """True when this wall time never happens, i.e. the clock skips over it."""
    return not _round_trips(naive, tz)


def is_ambiguous(naive: dt.datetime, tz: ZoneInfo) -> bool:
    """True when this wall time happens twice, i.e. the clock repeats an hour."""
    if not _round_trips(naive, tz):
        return False
    return naive.replace(tzinfo=tz, fold=0).utcoffset() != naive.replace(tzinfo=tz, fold=1).utcoffset()


def resolve(naive: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """Attach `tz` to a naive local time, choosing the first occurrence (§9).

    Raises `NonExistentLocalTime` rather than letting `zoneinfo` quietly shift the value forward,
    which would book a guest into an hour that does not exist.
    """
    if is_nonexistent(naive, tz):
        raise NonExistentLocalTime(f"{naive.isoformat()} does not exist in {tz.key}")
    return naive.replace(tzinfo=tz, fold=0)


def format_instant(moment: dt.datetime) -> str:
    """Render an aware datetime as `YYYY-MM-DDTHH:MM:SS±HH:MM`.

    `isoformat()` produces exactly this, including the offset, which is what makes two different
    instants at the same wall time distinguishable in a response body.
    """
    return moment.isoformat()


def minutes_of(hhmm: str) -> int:
    """`"18:30"` -> `1110`. Used for opening hours and slot arithmetic."""
    hours, _, minutes = hhmm.partition(":")
    return int(hours) * 60 + int(minutes)