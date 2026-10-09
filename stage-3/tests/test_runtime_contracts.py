"""Unit 1d: the image-dependency guard. Docker-free, and it passes on the current tree by design.

The 17 named defects each have a test that is red now and green after a fix, because
count-monotonicity depends on it. This one is different in kind. It guards a **build-time
dependency**, not a defect:

    Dockerfile:7   RUN pip install --no-cache-dir "tzdata>=2024.1"

`python:3.12-slim` ships no `/usr/share/zoneinfo`, so that `pip install` is the *sole* source of the
IANA database in the image. `app/tz.py` imports only `zoneinfo` and `main.py:197` / `main.py:380`
call `ZoneInfo(restaurant["timezone"])` with no `try`, and there is no `except ZoneInfoNotFoundError`
anywhere in `app/`. One missing package and every availability and booking call 500s at grading time.

Why this file is mostly static assertions, and not just a runtime probe
---------------------------------------------------------------------
A runtime probe is the obvious instrument and it is a guard that passes for the wrong reason. This
host has its own IANA database from a host-level `tzdata` install that has nothing to do with
`Dockerfile:7`, so `GET /availability` returning 200 proves only that `resolve()` and the `ZoneInfo`
call work *here*. It cannot fail for any image-related reason, cannot distinguish a correct image from
one whose tzdata was never installed, and cannot tell a correct image from a correct host. A green
result here would be read as "the tzdata dependency is guarded" while no test asserted
`Dockerfile:7` at all -- a test whose name promises one thing and whose body asserts another.

So the load is carried by five static assertions about the `Dockerfile`, which need no image, and the
runtime probe is kept alongside them because it is what proves the dependency is *load-bearing* rather
than incidentally installed. Together they foreclose the named regression: someone hits a
network-isolated build, "fixes" `Dockerfile:7` with `|| true`, or deletes it, or drops the floor. The
build goes green, `GET /health` stays green because it returns `status: ok` without resolving a zone,
and every availability and booking call 500s at grading time. Nothing in the shipped tree could see it.

Assertion 0 is the precondition that makes 1-4 mean anything: if a source file ever imported `tzdata`
or set `TZPATH` as a fallback, `Dockerfile:7` would no longer be the sole source and the premise would
have changed. Then assertion 0 fails and says so. That is intended -- fail loudly and re-derive, do
not silently widen the guard.

A guard never seen red is the same defect shape as Unit 1b's original `fnmatch` spec, which is the
finding that put this unit on the board. Every assertion here was therefore also checked against a
mutated scratch copy: `|| true`, a dropped version floor, a deleted install, and an install moved
below the first `COPY`. Each one goes red.

`GET /health` is not modified or asserted on its own anywhere else in this stage, and none of these
names belongs to Unit 0's named-defect list: a guard that is *supposed* to pass would muddy the exact
signal the monotonicity probe reads. No Docker, no network, no third-party import. Run from `stage-1`:

    python -m unittest tests.test_runtime_contracts -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests import support

STAGE1 = Path(__file__).resolve().parent.parent
DOCKERFILE = STAGE1 / "Dockerfile"
APP = STAGE1 / "app"

TZDATA = "tzdata"
VERSION_FLOOR = "tzdata>="

# Suffixes that turn a failing `RUN` into a successful build. Each one is a way to lose the IANA
# database while every visible signal stays green.
NON_FATAL_SUFFIXES = {"true", ":", "-", "exit 0", "/bin/true"}


def instructions(keyword: str) -> list[tuple[int, str]]:
    """`(line_number, command)` for every `RUN`/`COPY` instruction, comments and blanks dropped."""
    found = []
    for number, raw in enumerate(DOCKERFILE.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        if head.upper() == keyword:
            found.append((number, rest.strip()))
    return found


def tzdata_install() -> tuple[int, str] | None:
    """The `RUN` line that installs tzdata, or None if there is not one."""
    for number, command in instructions("RUN"):
        if TZDATA in command:
            return number, command
    return None


def non_fatal_suffix(command: str) -> str | None:
    """The part of `command` that swallows a failure, or None if it fails loudly.

    Only the text after the final `||` decides, because that is what Docker appends to the exit
    status. A trailing bare `-` is treated the same way.
    """
    if "||" in command:
        tail = command.rsplit("||", 1)[1].strip().strip("\"'")
        if tail in NON_FATAL_SUFFIXES:
            return f"|| {tail}"
    if command.rstrip().endswith(" -"):
        return "trailing `-`"
    return None


def sources_naming_the_dependency() -> list[str]:
    """`file:line` for every mention of the dependency or a zoneinfo fallback in `app/`."""
    pattern = re.compile(r"tzdata|TZPATH|reset_tzpath")
    hits = []
    for path in sorted(APP.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.name}:{number}: {line.strip()}")
    return hits


class ImageDependency(unittest.TestCase):
    """`Dockerfile:7` is the sole source of the IANA database in the image, and it fails loudly.

    These assertions pass on the current tree. That is the design: they guard a build-time
    dependency, and no red-to-green transition exists for them.
    """

    # -- assertion 0: the precondition that gives 1-4 their meaning ------------------------

    def test_no_source_file_supplies_the_iana_database(self):
        """`Dockerfile:7` is the *sole* source of the IANA database in the image.

        No `app/` module may import `tzdata` or point `zoneinfo` at a fallback `TZPATH`. If one ever
        does, `Dockerfile:7` stops being load-bearing and this guard's premise has changed -- fail
        loudly and re-derive rather than silently widening it.
        """
        self.assertEqual(
            sources_naming_the_dependency(), [],
            "an app source now names tzdata/TZPATH/reset_tzpath, so Dockerfile:7 is no longer the"
            " sole source of the IANA database and these assertions no longer prove what they"
            " claim. Re-derive the guard before touching Dockerfile.")

    # -- assertions 1-4: the Dockerfile line itself ----------------------------------------

    def test_tzdata_install_exists(self):
        found = tzdata_install()
        self.assertIsNotNone(
            found,
            f"no RUN line installs {TZDATA!r}. python:3.12-slim ships no /usr/share/zoneinfo, so"
            " without this the image has no IANA database and every availability and booking call"
            " 500s at grading time while GET /health stays 200.")
        self.assertIn("install", found[1].lower(),
                      f"Dockerfile:{found[0]} mentions tzdata but does not install it: {found[1]!r}")

    def test_tzdata_install_is_not_made_non_fatal(self):
        found = tzdata_install()
        self.assertIsNotNone(found, "covered by test_tzdata_install_exists")
        swallowed = non_fatal_suffix(found[1])
        self.assertIsNone(
            swallowed,
            f"Dockerfile:{found[0]} makes the tzdata install non-fatal with {swallowed!r}:"
            f" {found[1]!r}\n"
            "  A network-isolated build would then go green without the IANA database. GET /health"
            " still returns 200 because it never resolves a zone, so this is invisible until"
            " grading. Fix the build; do not silence the failure.")

    def test_tzdata_install_precedes_first_copy(self):
        found = tzdata_install()
        self.assertIsNotNone(found, "covered by test_tzdata_install_exists")
        copies = instructions("COPY")
        self.assertTrue(copies, "the Dockerfile has no COPY instruction, so there is nothing to"
                                 " order the install against")
        first_copy = copies[0][0]
        self.assertLess(
            found[0], first_copy,
            f"the tzdata install is Dockerfile:{found[0]} but the first COPY is Dockerfile:"
            f"{first_copy}. A layer that installs a package must not be cached behind the source"
            " it is independent of; move the install above the COPY.")

    def test_tzdata_version_floor_survives(self):
        found = tzdata_install()
        self.assertIsNotNone(found, "covered by test_tzdata_install_exists")
        self.assertIn(
            VERSION_FLOOR, found[1],
            f"Dockerfile:{found[0]} no longer carries the {VERSION_FLOOR!r} floor: {found[1]!r}\n"
            "  Pinning nothing means a resolver upgrade -- or a yanked release -- can silently"
            " change which IANA data ships. Restore the floor rather than relaxing the pin.")

    # -- the behavioural half: the dependency is load-bearing, not incidentally installed ----

    def test_health_green_and_availability_green(self):
        """Both, deliberately. `GET /health` returns `status: ok` without resolving a zone.

        `app/main.py:66-67` is a static literal, so an image whose tzdata was never installed is
        fully green on health and 500s on every real call. Asserting health alone would be exactly
        the false green this stage has already produced once.
        """
        with support.service() as base_url:
            anon = support.Client(base_url)
            reset = anon.post("/_test/reset", json_body=berlin_fixture("2026-06-01"))
            self.assertEqual(204, reset.status, f"reset failed: {reset!r}")
            health = anon.get("/health")
            availability = anon.get("/availability", params={
                "restaurant_id": "r_anker", "date": "2026-06-01", "party_size": 4})
            self.assertEqual(200, health.status, f"health failed: {health!r}")
            self.assertEqual(
                200, availability.status,
                f"health was {health.status} but availability was {availability.status}:"
                f" {availability!r}\n"
                "  This is the exact split the assertion exists to catch -- the IANA database is"
                " missing, and /health cannot see it because it never resolves a zone.")

    def test_availability_carries_a_real_iana_offset(self):
        """`starts_at` must carry the zone's real offset: `+02:00` in June, `+01:00` in winter.

        An offset is the only evidence that `zoneinfo` resolved a key rather than falling back to
        something. Both seasons are asserted because a hard-coded `+01:00` would satisfy a winter
        fixture while silently mis-rendering every summer booking.
        """
        cases = (("2026-06-01", "+02:00", "CEST"), ("2026-01-15", "+01:00", "CET"))
        with support.service() as base_url:
            anon = support.Client(base_url)
            for date, expected_offset, season in cases:
                with self.subTest(date=date, season=season):
                    reset = anon.post("/_test/reset", json_body=berlin_fixture(date))
                    self.assertEqual(204, reset.status, f"reset failed: {reset!r}")
                    availability = anon.get("/availability", params={
                        "restaurant_id": "r_anker", "date": date, "party_size": 4})
                    self.assertEqual(200, availability.status, f"{availability!r}")
                    slots = (availability.json or {}).get("slots") or []
                    self.assertTrue(slots, f"no slots returned for {date}: {availability!r}")
                    first = slots[0]
                    self.assertEqual(
                        expected_offset, offset_of(first["starts_at"]),
                        f"{season}: first slot starts_at is {first['starts_at']!r}, expected offset"
                        f" {expected_offset}. An offset this far off means the zone was not"
                        " resolved from the IANA database.")


def berlin_fixture(date: str) -> dict:
    """A Europe/Berlin fixture open on `date`, so the first slot is a real one.

    The date is pinned rather than relative so the June/winter offsets are the ones asserted;
    `support.booking_date()` is deliberately relative and would drift across a DST boundary.
    """
    return support.fixture(restaurants=[support.restaurant(
        timezone="Europe/Berlin",
        opening_hours=support.opening_hours_on(date, "09:00", "23:00"))])


def offset_of(stamp: str) -> str:
    """The `+HH:MM` offset of an RFC 3339 timestamp, or the whole string if it carries none."""
    text = stamp.strip()
    for sign in ("+", "-"):
        cut = text.rfind(sign)
        if cut > 10:  # past the date's own hyphens
            return text[cut:]
    return text


if __name__ == "__main__":
    unittest.main()