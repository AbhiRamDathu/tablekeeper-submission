#!/usr/bin/env python3
"""Live board and plan resolver for Jam room 48956829-4519-4a35-969e-57797acacd3b.

WHY THIS EXISTS. Four consecutive reviews in this room filed verdicts against a
stale snapshot: a 6-task board, then a 7-task board, both "zero assignments",
and a plan at 85595 bytes / 1162 lines which was revision 3.6 while the live
plan was 3.33 and is now 3.34. The board block in those reviews was headed
`===== board48956829`, which is not the output format of `jam work board` --
it was reconstructed from a document, not read from the board.

So: never summarise the board from prose, and never pick a plan by filename.
The room's Files catalogue holds ~76 snapshots and many are named `plan.md`,
so picking by name returns whichever revision happens to be older.

This script fetches both facts itself, in one shot, and prints the fetch time
so a reader can tell how old the numbers are. It has no cached state.

    python board_verify.py            # board + live plan pointer
    python board_verify.py --gate     # also run the spec gate (~2 min)
    python board_verify.py --selftest # do the plan's own markers still hold?
    python board_verify.py --check FILE

Exit codes: 0 = ok, 1 = the thing you checked is not what you think,
2 = fetch failed (never report a board on this).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

ROOM = "48956829-4519-4a35-969e-57797acacd3b"
PROFILE = "default"
SESSION = "planner"
REPO = r"C:\Users\linga\Jam\tablekeeper-submission"
STAGE1 = os.path.join(REPO, "stage-1")

# Signatures of the stale reads that produced four bad verdicts. If a number
# below matches one of these, you are looking at rev 3.6-era text, not the
# board. Do not reason about it; re-fetch.
STALE_PLAN_BYTES = 85595
STALE_PLAN_LINES = 1162
STALE_PLAN_SHA_PREFIX = "ecf54ce9"
STALE_TASK_COUNTS = {6, 7}

# A one-sided stale check is not enough, and this file records the proof.
#
# MUST-ABSENT: occurs in rev 3.6, absent from the live plan. A hit proves the
# copy in your hand is dead, whichever file it came from.
#
# MUST-PRESENT: absent from rev 3.6, present in the live plan. A miss proves the
# copy in your hand is not the live one. Neither direction can be spoofed by a
# republished header, because it tests content rather than a filename.
#
# THE ONE RULE FOR ADDING A MARKER, and it is not "pick a rare string":
#
#   A marker must not be QUOTED by the revision it is meant to test.
#
# Measured 2026-10-05, --check on the live plan returned DEAD REVISION, exit 1,
# because MUST_ABSENT[0] was quoted verbatim in the live plan's own marker
# table. Grepping the live plan for it therefore returned a hit and the one
# correct file in the room was condemned. That is the same failure class as a
# stale read -- the instrument pushed a reader away from the right answer -- and
# it arrived from the rule meant to prevent it.
#
# So: the plan names its markers by ALIAS (STALE-1, MUST-PRESENT-1..3), never by
# literal. The literals live here and only here. Run --selftest before you
# publish a revision; it fails if a new revision quotes one.
MUST_ABSENT = ("Gap closed: how many violations",)  # STALE-1

MUST_PRESENT = ("Reading this room without reading a ghost",   # MUST-PRESENT-1
                "Retry-After",                                # MUST-PRESENT-2
                "Do not reconstruct the board")               # MUST-PRESENT-3

# NOT DISCRIMINATORS. Both of these occur in rev 3.6 *and* in the live plan, so
# treating either as a stale marker would condemn the current plan. Measured
# 2026-10-05 with Select-String -SimpleMatch, line hits, live = rev 3.36:
#     'never 409, never 500'               rev3.6 = 2   live = 2
#     'internal_error_message_is_redacted' rev3.6 = 2   live = 2
# Both were proposed as rev-3.6-only markers on 2026-10-05. Neither discriminates:
# applying either as a stale test returns "not dead" for the dead copy too, so
# the proposed test would have MISSED the stale read it was offered to catch.
# All three strings proposed in that message fail: STALE-1 was contaminated by
# the self-reference above, these two by appearing in both revisions.
NOT_DISCRIMINATORS = ("never 409, never 500",
                      "internal_error_message_is_redacted")


def check(path: str) -> int:
    """Two-sided content discrimination on any copy of the plan."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2

    size = len(text.encode("utf-8", "replace"))
    lines = text.splitlines()
    first = lines[0] if lines else "(empty)"
    print(f"FILE   {path}")
    print(f"SIZE   {size} B, {len(lines)} lines")
    print(f"LINE 1 {first}")
    print()
    print(f"{'verdict':<8} string")
    stale_hits = [s for s in MUST_ABSENT if s in text]
    live_misses = [s for s in MUST_PRESENT if s not in text]

    for s in MUST_ABSENT:
        print(f"{'STALE' if s in stale_hits else 'ok':<8} {s}")
    for s in MUST_PRESENT:
        print(f"{'MISSING' if s in live_misses else 'ok':<8} {s}")
    for s in NOT_DISCRIMINATORS:
        here = s in text
        print(f"{'-':<8} {s}  (present here: {here}; NOT a discriminator)")

    print()
    if stale_hits:
        print("VERDICT: DEAD REVISION. These strings exist only in rev 3.6:")
        for s in stale_hits:
            print(f"  - {s}")
        print("Do not reason about this file. Re-fetch with plan show.")
        return 1
    if live_misses:
        print("VERDICT: NOT THE LIVE PLAN. These strings are absent here but present")
        print("in the current revision:")
        for s in live_misses:
            print(f"  - {s}")
        print("Re-fetch with plan show, or run without --check for the pointer.")
        return 1
    print("VERDICT: LIVE. Content agrees with the current revision on every marker.")
    return 0



def selftest() -> int:
    """Assert the marker set still agrees with the LIVE plan.

    --check can only prove discrimination if someone runs it on two files. This
    mode removes that dependency: it fetches the live plan itself and asserts
    the invariant directly, so a revision that quotes a marker is caught at
    publication time instead of misleading the next reader.

    It does NOT substitute for --check on a known-dead copy. Run both:

        python board_verify.py --selftest          # markers vs the live plan
        python board_verify.py --check <dead copy> # markers vs rev 3.6
    """
    try:
        out = jam("plan", "show", ROOM)
    except Exception as exc:  # noqa: BLE001
        print(f"FETCH FAILED: {exc}", file=sys.stderr)
        return 2

    live = None
    for token in out.split():
        if token.lower().endswith(".md") and os.path.exists(token):
            live = token
    if live is None:
        print("could not resolve a readable plan path from `plan show`:", file=sys.stderr)
        print(out.strip(), file=sys.stderr)
        return 2

    with open(live, encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read()

    section(f"MARKER SELFTEST against the live plan  {live}")
    print(f"{'alias':<18}{'verdict':<10}literal (defined in this file only)")
    broken = []
    for i, s in enumerate(MUST_ABSENT, 1):
        hit = s in text
        alias = f"STALE-{i}"
        print(f"{alias:<18}{'QUOTED' if hit else 'ok':<10}{s}")
        if hit:
            broken.append(f"{alias} is quoted by the live plan")
    for i, s in enumerate(MUST_PRESENT, 1):
        miss = s not in text
        alias = f"MUST-PRESENT-{i}"
        print(f"{alias:<18}{'MISSING' if miss else 'ok':<10}{s}")
        if miss:
            broken.append(f"{alias} is absent from the live plan")
    print()
    if broken:
        print("VERDICT: MARKERS CONTAMINATED. --check is not trustworthy right now.")
        for b in broken:
            print(f"  - {b}")
        print("Fix before publishing: a marker must not be quoted by the revision")
        print("it tests. Name markers by alias in the plan, never by literal.")
        return 1
    print(f"VERDICT: MARKERS CLEAN. {len(MUST_ABSENT)} stale and "
          f"{len(MUST_PRESENT)} present markers all agree with the live plan.")
    return 0


def jam(*args: str) -> str:
    # Bytes, not text: the board JSON carries the room's UTF-8 punctuation and
    # the Windows default codec (cp1252) cannot decode it.
    cmd = ["jam", "--profile", PROFILE, "--session", SESSION, *args]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"jam {' '.join(args)} failed: {err}")
    return proc.stdout.decode("utf-8", "replace")


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def board() -> int:
    try:
        raw = jam("work", "board", ROOM, "--json")
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        print(f"FETCH FAILED: {exc}", file=sys.stderr)
        print("Report no board at all. A board read from anywhere else is worse "
              "than no board.", file=sys.stderr)
        return 2

    data = json.loads(raw)
    tasks = data.get("tasks", [])
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S local")

    section(f"LIVE BOARD  (fetched {stamp})")
    print(f"{'uid':<5} {'bytes':>6}  {'assignees':<44} subject")
    assigned = 0
    for task in sorted(tasks, key=lambda t: int(t["uid"].lstrip("#"))):
        people = [a["agent"] for a in task.get("assignments", []) if a.get("agent")]
        assigned += 1 if people else 0
        who = ", ".join(p.replace("velishalalingaraju/", "") for p in people) or "-- NONE --"
        subject = task.get("subject", "")[:60]
        print(f"{task['uid']:<5} {len(task.get('detail','')):>6}  {who:<44} {subject}")

    print()
    print(f"TASKS {len(tasks)}   WITH AN ASSIGNEE {assigned}   "
          f"WITHOUT ONE {len(tasks) - assigned}")

    if len(tasks) in STALE_TASK_COUNTS:
        print()
        print(f"!! TASK COUNT {len(tasks)} MATCHES A KNOWN STALE READ. Every review that")
        print("!! reported this number was reading a document, not the board.")
        print("!! Re-fetch. Do not file a verdict from this output.")

    unassigned = [t["uid"] for t in tasks
                  if not [a for a in t.get("assignments", []) if a.get("agent")]]
    if unassigned:
        print(f"UNASSIGNED: {', '.join(unassigned)}  (expected only for a task just "
              f"created and not yet taken)")
    return 0


def plan() -> int:
    section("LIVE PLAN")
    try:
        out = jam("plan", "show", ROOM)
    except Exception as exc:  # noqa: BLE001
        print(f"FETCH FAILED: {exc}", file=sys.stderr)
        return 2
    print(out.strip())
    print()
    print("Use the path and byte size above. Do NOT open a file named plan.md from")
    print("the room Files: ~76 snapshots live there, 33 of them share that exact name,")
    print("so a filename choice returns an older revision regardless of intent.")
    print("The stored filename is derived from the label and can lag the revision by")
    print("one; the rev is in the LABEL above and in the plan's own first line. Read")
    print("the first line, never the filename.")
    for bad, label in ((f"{STALE_PLAN_BYTES} B", "bytes"),
                       (f"{STALE_PLAN_LINES} lines", "lines"),
                       (STALE_PLAN_SHA_PREFIX, "sha prefix")):
        if bad in out:
            print(f"!! {label} {bad} MATCHES THE STALE rev 3.6 READ. Stop and re-fetch.")
    return 0


def gate() -> int:
    section("SPEC GATE  (this runs the suite; ~2 min)")
    proc = subprocess.run([sys.executable, "-m", "unittest", "tests.test_spec_stage1"],
                          cwd=STAGE1, capture_output=True, timeout=900)
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    for line in text.splitlines():
        if any(k in line for k in ("named defects", "red (named)", "not yet provable",
                                   "green", "declined", "GATE", "blocked_on",
                                   "Ran ", "OK", "FAILED")):
            print(line.rstrip())
    return 0 if proc.returncode in (0, 1) else 2


def main() -> int:
    # The plan and the board both carry UTF-8 punctuation (em dashes, section
    # marks). The Windows default stdout codec is cp1252 and raises on them, so
    # force UTF-8 with replacement rather than crashing halfway through a report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true",
                        help="also run the Unit 0 spec gate against the working tree")
    parser.add_argument("--check", metavar="FILE",
                        help="two-sided content discrimination on any plan copy; "
                             "exit 1 if it is dead or not the live revision")
    parser.add_argument("--selftest", action="store_true",
                        help="assert the marker set still agrees with the live "
                             "plan; exit 1 if a marker has been contaminated")
    args = parser.parse_args()
    if args.check:
        return check(args.check)
    if args.selftest:
        return selftest()
    code = board() or plan()
    if args.gate:
        code = gate() or code
    return code


if __name__ == "__main__":
    raise SystemExit(main())
