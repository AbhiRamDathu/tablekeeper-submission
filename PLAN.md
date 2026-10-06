# Plan: Tablekeeper Stage 1 â€” reconciliation and the remaining two units (rev. 3.41)

## Goal

Close the gap between what the board says and what the tree does, then land the last two units of
product work: multi-restaurant fixture correctness in `store.py`, and the one-door/one-renderer
contract that has to be extracted out of `main.py` before it can be fixed.

Scope of this document: **the measured baseline and the remaining work only.** Rev. 3.33 remains in
Files as `plan-rev3.33.md` and stays authoritative for anything not restated here â€” the seventeen
gate names, the requirement citations, and the accumulated review history. What rev. 3.33 got wrong
about unit status and about line numbers is corrected below and superseded.

---

## Measured baseline â€” 2026-10-05, commit `c816cee`, **clean tree**. Every gate number below is a commit measurement, not a dirty-tree one.

Reproduce with `python -m unittest discover -s tests -t .` from `stage-1/`. Count and exit status
are the signal; wall clock is not.

```
Ran 250 tests - FAILED (failures=9, skipped=1). 250 is the whole suite.
  every one of the 9 failures is a named gate defect; nothing else fails.

SPEC GATE (Unit 0) - subset assertion - GATE PASSED
  named defects ............ 17
    red (named) ............ 9
    not yet provable ....... 0
    green .................. 8   auth_returns_display_name
                                 dockerignore_excludes_copied_source
                                 idempotency_key_scoped_by_path
                                 occupancy_scoped_by_restaurant
                                 reset_spec_shaped_seeded_reservation
                                 reset_two_restaurants_sharing_table_ids
                                 slot_end_is_absolute_across_transitions
                                 unknown_table_is_404
    declined ............... 1   starts_at_rendered_in_restaurant_zone

The 9 red: internal_error_message_is_redacted, list_reservations_envelope_and_desc_order,
opening_hours_in_fixture_order, party_size_wrong_type_is_422,
reservation_body_has_ends_at_and_created_at, reset_rejects_invalid_fixture,
restaurants_list_envelope, skipped_local_time_is_invalid_local_time,
slot_grid_and_opening_hours_codes.

The 1 skip is declared, not silent:
  test_a_batch_receipt_survives_an_export_import_round_trip
    skipped 'GET /_test/export is 404; 205 is unreachable until 10 lands'
```

**(rev. 3.41 supersedes this paragraph - the number is now a subset property, see "The invariant,
restated as a property.")**

Seventeen, not eighteen. `starts_at_rendered_in_restaurant_zone` is declined on the evidence: the
spec never constrains the offset of `starts_at`, and `17:00:00+00:00` is the same instant as
`19:00+02:00`. The gate reports the count; it does not assert it.

`not yet provable ....... 0` is itself a fix, not a rounding. The reporter used to derive that set
from a static dict, so it kept printing the name after the name passed - see rev. 3.40.

### Rev. 3.40 - the armed defect fired, and the gate was reporting it backwards

Measured on the clean tree at `c816cee`. Four commits, in the order they had to land.

**1. `occupancy_scoped_by_restaurant` was armed by `8acf056`, and the gate could not say so.**
`8acf056` keyed `tables` by `(restaurant_id, id)`, which removed the reason the name had been
blocked: two restaurants can now own a `t_2` each. That is exactly the world the unscoped
occupancy query gets wrong, so the latent defect became a live one in the same commit that made it
reachable. Count-monotonicity correctly reported no movement - red before, red after - and
correctly told nobody that the fix had armed it.

The arming probe, measured, before the fix:

```
r_one books t_2 .................................. 201
r_two still offers its own t_2 at the same slot .. true
r_two may book the same table id at the same time . expected 201, got 409
```

The middle leg passing beside a failing third leg is the whole diagnosis. `GET /availability`
filtered by `restaurant_id` and so *offered* `t_2`, while the booking path read occupancy by
`table_id` alone and refused it. An offered table that cannot be booked, and the two paths disagreed
about whose table `t_2` was. Fixed at `c55da5d`.

**2. The gate reported a passing name as unprovable.** `_report` built the not-yet-provable set
from `BLOCKED`, a static dict, rather than from the run - so the name stayed excluded from the
green list after it went green, and a real fix was invisible in the instrument meant to record it.
`_report` now derives the set from `result.skipped`, which is the only evidence that a cause went
unchecked, and reports any named defect that skipped even without a declared blocker so an
unexpected skip cannot be counted as green. Same commit as the product fix.

**3. A shipped test outlived the behaviour it described.** `main.py` answered an unknown
`table_id` with 404 `not_found` while `test_service.py` still asserted 422 - the product half of
Unit 5's rule had landed without the test half. Fixed at `442ac48`, both halves in one commit,
because either alone leaves a failure and the failing test was the only thing reporting the split.

**4. The idempotency migration decided under a lock it did not hold.** Fixed at `c816cee`; see
that commit's message. Both its tests were run against the pre-fix `store.py` and observed to fail,
so the pair is an instrument rather than an assertion of intent.

### What has actually landed

| Unit | Task | State | Evidence |
|---|---|---|---|
| 1a | #1 | **done** | `stage-1/.dockerignore` no longer excludes `app/`; `Dockerfile:10 COPY app ./app` now resolves |
| 1b | #6 | **done** | `stage-1/tests/test_packaging.py` exists; gate name green |
| 1d | #8 | **done** | `stage-1/tests/test_runtime_contracts.py` exists; commit `4405a80` |
| 0 | #5 | **done** | `stage-1/tests/test_spec_stage1.py` exists; gate PASSES; 17 names, subset assertion live |
| 10 | #3 | **done** | `app/intervals.py` exists; commit `ef97421`; gate name green |
| 4 | #4 | **done** | commit `9bf2220`; gate name green |
| 2 | #7 | **partial** | defect 18 and the `send_error` override landed (`3adc2d8`, `f3e654d`); **the extraction has not started** |
| 5 | #10 | **partial** | moves contract landed (`d4fe50e`, `750743c`, `ea6c446`); input order and the `table_id` 404 landed at `dacc02c`/`442ac48`; the door/renderer has not started |
| 3 | #2 | **done** | commit `8acf056`; composite key plus whole-fixture validation. Armed-defect follow-on landed at `c55da5d` - occupancy now scoped, `occupancy_scoped_by_restaurant` red -> green |
| 1c | #9 | **blocked** | `docker` is not on `PATH` on this host |

The board showed ten tasks `in_progress` at rev 3.36. It shows **eleven** at rev 3.37: ten
with an assignee, one without. Six of them are finished. **The Planner cannot correct this**:
`work room-status` updates only *your own* assignment. Each owner marks their own task, and `#7` is
the one that has to close, because its landed half is green and `#11` now carries the extraction.
That is the durable version of "authorship is not ownership", pointed the other way.

### Every `main.py` line anchor in the plan is stale. Use content anchors.

`main.py` has been edited four times since those anchors were written. Verified anchors, current tree:

| Plan said | Actually | Content to search for |
|---|---|---|
| `main.py:369` unknown table | `main.py:425` | `raise _invalid("no such table at this restaurant")`, directly after `SELECT * FROM tables WHERE id = ? AND restaurant_id = ?` at `:421` |
| `main.py:388` occupancy query | `main.py:450-451` | `SELECT starts_at_utc FROM reservations` + `WHERE table_id = ? AND status != 'cancelled'` â€” still no `restaurant_id` |
| `main.py:508-509` catch-all | `main.py:903` | `except Exception` in `_dispatch` â€” already redacted, gate name green |
| `main.py:66-67` `/health` | unchanged | `return 200 with status ok` |

Anchors that still hold: `app/store.py:47` (`id TEXT PRIMARY KEY`), `app/store.py:103-116`
(`BEGIN IMMEDIATE` / `ROLLBACK`), `app/tz.py:62` (`raise InvalidLocalTime(str(exc) from exc)`),
`app/auth.py:99` (inspects exception text, never interpolates â€” **do not fix**), and every
`test_service.py` line the plan cites: `:390/:393`, `:395/:405`, `:407/:413`, `:415/:418`.

**A rev 3.35 must not introduce a new `main.py:NNN`.** Cite the string.

---

## Unit 3 â€” multi-restaurant fixture correctness

- Owner: Implementer (already assigned, task #2)
- Files: `app/store.py` only. Nothing else.
- Depends on: nothing. Start now.
- Deliverable: `tables` keyed `(restaurant_id, id)`; the whole fixture validated against the model
  before the transaction opens; lock failure handled per path kind.
- Turns green: `opening_hours_in_fixture_order`, `reset_rejects_invalid_fixture`,
  `reset_spec_shaped_seeded_reservation`, `reset_two_restaurants_sharing_table_ids`.
- Unblocks: `occupancy_scoped_by_restaurant`, which is `blocked_on` the last of those.
- Acceptance:
  - `python -m unittest discover -s tests -t .` from `stage-1/` reports **10 red**, and
    `occupancy_scoped_by_restaurant` moves from *not yet provable* into the red list or green â€” one
    or the other, and either is a result. Report which.
  - Reset with two restaurants sharing `t_1..t_3` returns **204**.
  - Lock rule, by path kind. **Request paths:** a lock failure is 409 `table_unavailable` or a
    retryable 4xx, never 500. **Reset:** retries inside its own 10 s budget (`REQUIREMENTS.md:16`) and
    answers 204 or 422, never 409, never 500. State in your completion notes what reset does when the
    budget is genuinely exhausted â€” see Open question 1.
  - **Arming probe, mandatory, manual, in your completion run.** This unit converts a latent defect
    into a live one and the gate cannot see it. `occupancy_scoped_by_restaurant` is red before you
    land and red after, so count-monotonicity correctly reports *no movement* and correctly tells
    nobody that your commit just armed it. So: land the composite key, then in an **immediate
    follow-on commit** add `AND restaurant_id = ?` to the unscoped query â€” content anchor
    `WHERE table_id = ? AND status != 'cancelled'`, with a `restaurant["id"]` parameter. Book
    `r_anker/t_2`, read `r_ny` availability, confirm `n_2` is offered. Report either way. The fix has
    a working example twenty lines above the defect in the same function
    (`SELECT * FROM tables WHERE id = ? AND restaurant_id = ?`), and it is a query change, not a
    schema change, which is why it cannot live in your Files line and why it cannot wait behind
    Unit 5.
  - No `error.violations` key, ever. A 422 is exactly two keys
    (`REQUIREMENTS.md:47`): `{"error":{"code":...,"message":...}}`, first-error-wins, message names
    the failing path (`restaurants[0].tables[2].capacity must be a positive integer`). Validate the
    **whole** fixture before opening the transaction â€” that ordering is the only thing making
    first-error-wins safe here.

## Unit 2 â€” extract `main.py` (this is what gates Unit 5)

- Owner: Integrator (task #9's seat is blocked on an absent Docker, and this is the work that
  actually moves the submission)
- Files: `app/main.py`, and it creates `app/routes.py`, `app/service.py`, `app/errors.py`,
  `app/validation.py`, `app/render.py`, `app/idempotency.py`
- Depends on: nothing. **Disjoint from Unit 3** â€” it reads `store.py`, it does not edit it.
- Deliverable: `main.py` under 200 lines containing **no SQL**. Today it is **874 lines with 33 SQL
  statements**, so this is a real move, not a tidy-up.
- Turns green: nothing. This unit is behaviour-preserving and its only output is that Unit 5 becomes
  writable.
- Acceptance:
  - `python -m unittest discover -s tests -t .` from `stage-1/` reports **the same 11 red and the
    same 5 green** as the baseline above. Same names, not the same count â€” an unnamed failure means
    the refactor changed behaviour, and it is the signal that stops the run being believed.
  - `main.py` under 200 lines; zero SQL statements in it.
  - `app/main.py` reads `store.py`. It does not edit it.
  - Already-landed and must survive the move byte-for-byte in behaviour: the defect-18 redaction, the
    `send_error` override so a 501 is a JSON envelope, `do_PATCH`, `do_DELETE`.
- **Do not cut an instrument for this.** The gate *is* the characterisation suite: 17 names, subset
  assertion, 158 tests. A refactor this size with no behavioural net is the one thing in this plan
  that could pass a green build and lose a requirement.

## Unit 5 â€” one door, one renderer (gated on Unit 2)

- Owner: unassigned until Unit 2's files exist. **Do not start it against `main.py`** â€” its Files
  line names six modules that are not on disk, and that is exactly the coupling file overlap hides.
- Files: `app/validation.py`, `app/render.py`, `app/idempotency.py`, `app/routes.py`, `app/service.py`,
  and `app/tz.py` for line 62 only.
- Depends on: Unit 2.
- Turns green: `restaurants_list_envelope`, `list_reservations_envelope_and_desc_order`,
  `reservation_body_has_ends_at_and_created_at`, `slot_grid_and_opening_hours_codes`,
  `unknown_table_is_404`, `skipped_local_time_is_invalid_local_time`, and
  `party_size_wrong_type_is_422` subject to Open question 2.
- Acceptance:
  - One validation function, one renderer. `create`, `PATCH` and moves all go through both â€” three
    endpoint-local copies would each be individually correct and jointly wrong.
  - `unknown_table_is_404` is a **one-line** change at `raise _invalid("no such table at this
    restaurant")` to `HttpError(404, "not_found", ...)`, in the **same commit** as the correction of
    `test_service.py:415/418`. Neither before nor after the other.
  - `test_service.py:390/393` â†’ 422 `party_exceeds_capacity`; `:395/405` â†’ 422 `invalid_local_time`.
    Same rule: same commit as the product fix, same owner.
  - `app/tz.py:62` routes `str(exc)` through the same shared helper as the other sites.
  - `app/auth.py:99` is **not** touched. It inspects exception text for control flow and never
    interpolates; rewriting it deletes duplicate-email detection and returns 500 where the spec wants
    409.
  - Report: **10 red** if Unit 3 has landed, **9 red** if it has not, and the names either way.

## Unit 1c â€” blocked, and it stays blocked

- Owner: Integrator, task #9. `docker` is not on `PATH` on this host; it has been absent since the
  start of this room.
- Everything reachable without Docker has already been re-homed: the tzdata build-dependency guard
  is task #8 and is green; the packaging guard is task #6 and is green; the 50-concurrent no-5xx
  bound and the `{201:1, 200:19}` idempotency tally are asserted by two shipped tests
  (`test_concurrent_identical_requests_book_twice`,
  `test_fifty_concurrent_requests_produce_no_5xx`) â€” **reference them, do not clone them**, because a
  clone inflates the gate's own count and makes seventeen harder to defend.
- Mark the task `blocked` with that reason. Do not report a build result from any stdlib stand-in.

---

## Risks

- **The two units are disjoint by file and coupled by import.** Unit 2 creates the modules Unit 5
  edits. If Unit 2 lands and Unit 5 has already been started against `main.py`, one Implementer has
  two sets of edits in the same logic and the merge is done by hand. Earliest observation: Unit 5's
  task being `in_progress` while `app/routes.py` does not exist.
- **Unit 3 arms a live defect and the gate is blind to it by construction.** Handled by the mandatory
  follow-on commit and the arming probe, both in Unit 3's acceptance. Earliest observation: composite
  key merged with the unscoped query still unfixed.
- **`main.py` anchors rot again.** Handled by banning `main.py:NNN` from rev 3.35 onward. Earliest
  observation: any new task or plan text citing a `main.py` line number.
- **Docker-gated evidence does not exist and will be graded.** Unit 1c's acceptance is unrunnable
  here. Report it as UNRUN, never as passing. A2 (60 s to healthy) and the harness run
  (`python -m harness run --mode isolated`) are unverified on this host and must be labelled so.
- **The seventeen are not a coverage claim.** They are a floor: every failure is a *named* failure.
  Five uncovered areas found at rev 3.5 have gained names; the half of the spec surface those names
  do not touch is still only covered by the shipped 70.

## Open questions

1. **What does `POST /_test/reset` answer when its 10 s lock budget is genuinely exhausted?**
   Escalated to the owner at rev 3.17 and still unanswered. `REQUIREMENTS.md:23` pins reset to 204;
   `REQUIREMENTS.md:17` forbids 5xx outright; if the budget is exhausted the fixture was not
   committed, so no honest 204 exists. 422 is unavailable too â€” exhaustion is not "a stated rule
   violated" per `:54`. **Recommendation: 503 with `Retry-After`, as a recorded deviation from `:17`,
   because the only way to keep `:17` is a 204 that lies.** `transaction()` at `store.py:103-116` is
   `BEGIN IMMEDIATE` with `ROLLBACK`, so a lock failure leaves the *previous* fixture intact and a
   204 would hand the harness a stale world that every later assertion then runs against. 500 is
   acceptable if preferred. **204 is not, under any reading.** Blocks: one line in Unit 3.
2. **Is a wrong-*type* `party_size` a 400 or a 422?** Put to the owner this revision; no answer yet.
   `REQUIREMENTS.md:48` says 400 for a wrong JSON type; `:57` says 422 for invalid `party_size`
   *including strings and booleans*; `:129` says 422 for not-an-integer.
   `test_service.py:407/413` asserts 400 and is shipped; gate defect `party_size_wrong_type_is_422`
   asserts 422 and is red. **Recommendation: 422.** `:57` names strings explicitly, so it covers
   `"four"` directly and a specific rule beats a general one; read `:48` as governing fields other
   than `party_size`. **This withdraws the pending KEEP-400 recommendation at rev 3.15**, which read
   `:57`/`:129` as applying only after coercion to `int` â€” that reading makes the words "including
   strings" meaningless. Whatever is ruled, the shipped test and the product defect move in **one
   commit by one owner**, never before. Blocks: one named defect of eleven.

## Reading this room without reading a ghost â€” rev. 3.35 addition

### Do not reconstruct the board

**Run the instrument, do not reconstruct the board.**

```
python C:\Users\linga\Jam\tablekeeper-submission\docs\board_verify.py        # board + live plan pointer
python C:\Users\linga\Jam\tablekeeper-submission\docs\board_verify.py --gate # also run the Unit 0 spec gate
```

In Files as `board-verify-rev3.37.py`. It fetches the board and the live plan path itself,
prints the fetch timestamp, and **refuses to report** a board whose task count is 6 or 7, or a plan
of 85595 bytes - it prints the known-stale warning instead. Exit code 2 means the fetch failed:
report no board at all. Three check modes, each with its own exit code:

```
python docs\board_verify.py --check FILE   # is this copy live?   0 = live, 1 = no
python docs\board_verify.py --selftest     # do the markers hold? 1 = contaminated
python docs\board_verify.py --gate         # also run the Unit 0 spec gate
```

**Why this rule exists, measured.** The room Files catalogue held 76 snapshots, **33 of them named
exactly `plan.md`, carrying 33 different byte sizes** from 8624 to 251929. Four consecutive reviews
in this room filed verdicts against one of them. The one the Reviewer opened was
`art-ff4771280b09e2f7f4f90b8d`, sha256 `ecf54ce9â€¦`, 85595 bytes, first line
`# Plan: â€¦ (rev. 3.6)` â€” verified against the store, and matching the sha and byte count the
Reviewer independently reported. It was reading rev. 3.6 while the live plan was 3.33, and its
board block was headed `===== board48956829`, which is not the output format of `jam work board` at
all. A board reconstructed from a document cannot be corrected by re-reading the document.

Standing rules, all of them earned:

1. The live plan is whichever snapshot `plan show` names. Never pick one from Files by name.
2. A board is `work board <room-id> --json`, read per-task, never summarised from prose.
3. `started_at` reads 0 on every task and is not a signal. Occupancy is `assignments[].status`.
4. Never cite a line number in `app/main.py`. It moved four times in three hours. Cite the string.
5. Publish snapshots as `plan-rev<N>.md`, never as `plan.md`.
6. **Validate any plan copy by content, in both directions, before reasoning about it.**

   ```
   python C:\Users\linga\Jam\tablekeeper-submission\docs\board_verify.py --check <file>
   ```

   Exit 0 = live. Exit 1 = dead, or not the live revision, and the script says which. It works on any
   copy, and it cannot be fooled by a republished header, because it never looks at a filename.
   `--selftest` is the other half: it fetches the live plan and asserts the markers against it, so a
   contaminated marker is caught at publication time instead of misleading the next reader.

   A one-sided check is not sufficient, and this is measured rather than asserted. Counts are
   `Select-String -SimpleMatch` line hits, measured 2026-10-05. Dead column = the rev 3.6 artifact
   `art-ff4771280b09e2f7f4f90b8d` (sha `ecf54ce9...`). Live column = this revision.

   | alias | rev 3.6 | live | use |
   |---|---|---|---|
   | STALE-1 | 1 | 0 | **must be absent** - a hit proves dead |
   | MUST-PRESENT-1 | 0 | 2 | **must be present** - a miss proves not live |
   | MUST-PRESENT-2 | 0 | 6 | **must be present** |
   | MUST-PRESENT-3 | 0 | 2 | **must be present** |
   | `never 409, never 500` | 2 | 2 | **not a discriminator** |
   | `internal_error_message_is_redacted` | 2 | 2 | **not a discriminator** |

   **Markers are named by alias in this plan, never by literal. The literals live only in
   `docs/board_verify.py`.** That is not a style rule. Rev. 3.35 published STALE-1 verbatim in the
   table above, so the live plan quoted the one string that identifies a dead revision - and
   `--check` on the live plan answered `DEAD REVISION`, exit 1, on 2026-10-05:

```
> python docs\board_verify.py --check <live rev 3.36>
STALE    <the STALE-1 literal>
ok       Reading this room without reading a ghost
ok       Retry-After
ok       Do not reconstruct the board
VERDICT: DEAD REVISION. ... Do not reason about this file. Re-fetch with plan show.
exit=1
```

   The STALE-1 line above is elided in this plan on purpose. The first draft of this revision pasted
   it verbatim as evidence and the count went straight back to 1 - the same defect, reintroduced by
   the fix that documents it. If you re-paste real output here, redact the stale marker.

   The instrument built to stop a reader trusting a dead revision pushed them off the only correct
   file in the room. The generalisable rule: **a marker must not be quoted by the revision it tests**,
   because the newest revision has to explain the old markers in order to be useful at all. Fixed at
   this revision: `--check` on the live plan exits 0, `--check` on the rev 3.6 artifact exits 1.

   **All three strings offered as stale markers on 2026-10-05 fail, for two different reasons.** The
   two `not a discriminator` rows read 2 in rev 3.6 *and* 2 here, so neither discriminates: applied as
   a stale test each returns "not dead" for the dead copy too, and would have **missed** the stale
   read they were offered to catch. STALE-1 was contaminated by the self-reference above. A marker is
   valid only when the two columns differ, and a stale marker only when the live column is 0.
   The asymmetry that makes this survivable: a MUST-PRESENT marker may gain hits every time a
   revision quotes it, which is harmless, so the live column for those rows drifts upward and is
   only ever a floor. A MUST-ABSENT marker has no such slack - one hit condemns the file.

   And the inversion worth noticing: `error.violations` reads **4 hits in this plan and 0 in rev 3.6**.
   The string reported absent is present four times in the current plan, each time recording that it
   was withdrawn at rev 3.17 and must not be reintroduced.

7. **No `error.violations` key, ever â€” and that is a closed decision, not an open one.** Rev. 3.17
   withdrew the violations list outright because `REQUIREMENTS.md:47` pins every 4xx and 5xx body to
   exactly two keys. Do not re-raise it.
8. Reset lock exhaustion is **described** â€” 503 with `Retry-After`, `REQUIREMENTS.md:17` not
   extending to an unreachable storage layer â€” and **awaiting only the owner's answer**. It is not an
   undescribed branch. Do not re-raise it as one.
9. **A verdict with no command output in it is an assertion, not a verdict, and gets no action.**
   This is not a stylistic rule. Four consecutive reviews in this room filed verdicts whose board
   blocks read `===== board48956829` â€” not the output format of `jam work board` â€” and whose task
   counts were 6, then 7, matching no point in this board's history. A count that was never measured
   cannot be corrected by re-reading the document it came from.

## What this revision changed

- Board reconciled against the tree: six units recorded landed with evidence, three partial or not
  started, one blocked. The board's `in_progress` on all ten is stale and only each owner can correct
  it.
- `main.py` line anchors retired in favour of content anchors, after four edits moved them
  (369â†’425, 388â†’450, 508â†’903).
- Count settled at seventeen on a gate run, with `starts_at_rendered_in_restaurant_zone` declined and
  the reason recorded.
- Unit 2 and Unit 5 separated, because Unit 2 is extraction and Unit 5 cannot be written until
  extraction lands. They were one unit in effect and that was the coupling.
- KEEP-400 on wrong-type `party_size` withdrawn, with the reason, in favour of 422.

### Rev. 3.35

- Added the standing read-the-room rules and the verification instrument, after the root cause of
  four consecutive mis-verdicts was identified in the artifact store rather than argued about: 33
  snapshots sharing the name `plan.md`.
- Recorded that `error.violations` (withdrawn at rev. 3.17) and the reset lock-exhaustion branch
  (described, awaiting only the owner) are **closed as questions**, so they stop being re-raised as
  gaps in the next review.

### Rev. 3.37 - the board is 11 tasks with 10 named, and the read-the-room instrument was broken

Measured 2026-10-05. Everything below is reproducible with the commands quoted.

**1. The board is 11 tasks, 10 assigned, 1 not. It has never been 7 and unassigned.**

```
> python docs\board_verify.py
TASKS 11   WITH AN ASSIGNEE 10   WITHOUT ONE 1
UNASSIGNED: #11
```

A review dated 2026-10-05 reported `tasks: 7  assigned: 0  in_progress: 0` and asked for names on
each task. That reading has no point in this board's history, and the offer to route the seats is
declined: nine of the ten names are already on. `#11` is the only unassigned task in the room, and
it is assigned at this revision. Every `in_progress` on this board is stale - six units are
finished - and only the owner of each task can correct its own.

**2. Unit 2 had two seats and one file. One owner now.**

Task `#7` (Implementer) and task `#11` (Integrator) both claimed `app/main.py`. One piece of state,
two seats, and neither could see the other from its own Files line. Resolved:

- `#7` **closes**. Its landed half - defect 18 redaction, the `send_error` override, `do_PATCH`,
  `do_DELETE` - is green and evidenced at `3adc2d8`, `f3e654d`. The Implementer marks it, because
  `work room-status` writes only your own assignment.
- `#11` **carries the extraction alone**: `app/main.py` under 200 lines with no SQL, creating
  `routes.py`, `service.py`, `errors.py`, `validation.py`, `render.py`, `idempotency.py`. Owner
  Integrator. Unchanged acceptance: the same 11 red and the same 5 green *names*, main.py under 200
  lines, zero SQL, and the characterisation suite is not cut.
- One seat now holds an in-flight `main.py` edit, and it is not two.

**3. The stale-marker instrument condemned the live plan. Fixed, and guarded.**

See the marker table above. `docs/board_verify.py --check` returned `DEAD REVISION`, exit 1, on the
live plan, because the plan quoted its own stale marker. `--selftest` is new and fails when that
happens again. Verified in both directions at this revision:

```
> python docs\board_verify.py --check PLAN.md             -> exit 0, VERDICT: LIVE
> python docs\board_verify.py --check <rev 3.6 artifact> -> exit 1, VERDICT: DEAD REVISION
> python docs\board_verify.py --selftest                 -> exit 0, MARKERS CLEAN
```

**4. Two closed questions re-raised against rev 3.6, answered here so they stop again.**

- "`error.violations` reads zero times, so the 422 list has no key and no per-entry shape." Against
  this revision `error.violations` reads **6**. It was withdrawn at rev 3.17 and is forbidden by
  rule 7: `REQUIREMENTS.md:47` pins every 4xx and 5xx body to exactly two keys, first-error-wins,
  message names the failing path. A per-entry `rule` key would be a third key.
- "`503` and `Retry-After` read zero times, so the exhaustion branch is undescribed." Against this
  revision `Retry-After` reads **6**. The branch is written at Open question 1 with a recommendation
  of 503 plus `Retry-After`, and rule 8 records it as described and awaiting only the owner's answer.

Both read zero in rev 3.6 because rev 3.6 predates both decisions. A zero hit is evidence about the
copy in your hand, and about nothing else.

### Rev. 3.38 - a shared task's detail is capped at 10000 characters, measured

`work edit` on `#7` refused with `HTTP 422: Request validation failed` at 10042 characters of detail
and accepted 9992. The cap is 10000. Nothing in the CLI says so; the only symptom is a 422 that
reads like a permissions problem and is not one. `#7` already sits at 9961, so the next append to it
has about 35 characters of room and the text must be trimmed in place, not appended to.

This is why long task texts have to be moved out of the board and into the plan. Nine of the ten
task details on this board are 4-10 KB of prose that mostly restates plan text; the plan is the
durable copy and the board entry is a pointer. A 422 on a task detail costs two failed calls and
then a trim pass, and nothing records the number anywhere else.

### Rev. 3.39 - the disclosure gate lost its trigger and now passes vacuously

Measured 2026-10-05 on a dirty tree at commit `8acf056`. Unit 3 landed between the rev 3.36 baseline
and now, and it moved four gate names. One of them is a gate failure wearing a defect's name.

**Unit 3 is no longer "not started".** `8acf056` keys `tables` by `(restaurant_id, id)` and validates
the whole fixture before the transaction. That is the Unit 3 deliverable. The tree is also dirty, so
treat every number here as provisional until someone commits.

**`internal_error_message_is_redacted` went green -> red, and it is not a disclosure.** The failure
output is the whole finding: `expected 500, got 204`. Its only trigger is the reset at
`tests/test_spec_stage1.py:585-586`, the same fixture as `reset_two_restaurants_sharing_table_ids`,
which existed solely because that reset raised `UNIQUE constraint failed: tables.id`. Unit 3 removed
the cause, the reset returns 204, and there is no 500 body left to inspect.

The redaction itself is intact and must not be touched: `app/main.py:58` still defines
`GENERIC_500_MESSAGE`, and the catch-all at `app/main.py:936-941` still logs the real text
server-side and returns the fixed string plus a correlation id.

**The real defect is quieter than the red.** Of the five legs in that test, four failed and one
passed:

```
message leaks none of  ->  PASSED
```

It passed because the message is empty. `forbidden` is
`[w for w in ("UNIQUE", "tables.id", "sqlite3", "Traceback") if w in message]`, and no word occurs
in `""`. **The one leg that carries this gate's entire purpose now passes vacuously**, so the name
can no longer fail for a disclosure, and a real `str(exc)` restored to the catch-all would walk
through it. That is the shape this gate exists to prevent - a signal that looks armed and is not -
and it arrived through a fix rather than through a miss. Rev. 3.13 already required this assertion to
be whole-app; nothing ever addressed that it also needs a trigger that still raises.

New task, **`#12`, owner Test author, runs today**: restore the trigger by breaking the schema
underneath a valid fixture so the handler's own SELECT raises, keep the assertion unchanged, and prove
the guard by putting a real disclosure back and showing the name go red. Fixing this by relaxing the
assertion to expect 204, or by dropping the name, is forbidden - 17 stays 17.
*(the "17 stays 17" phrasing is superseded by rev. 3.41; the prohibition stands, the number does not)*

**`occupancy_scoped_by_restaurant` has a stale `blocked_on`.** The gate still reports it as not yet
provable, blocked on `reset_two_restaurants_sharing_table_ids`, and that blocker has been green since
`8acf056`. The gate therefore prints the same name as both blocked and red. Clear the block. The name
is a live defect now rather than a latent one, and only Unit 3's arming probe can show that: it was
red before Unit 3 and red after, so count-monotonicity correctly reports no movement and correctly
tells nobody that the composite key armed it. Also in `#12`, since it is the same instrument.

**Correction to the instruction sent at rev 3.37.** The Implementer was told to close `#7` on the
ground that the defect-18 redaction is "green and evidenced". The code is landed and still correct;
the *evidence* moved, because that evidence was a gate name that is red again for a reason that has
nothing to do with the code. `#7` should close on the commits, not on the gate name. Closing it
before `#12` lands is fine only if nobody reads its green gate name as current.

**Two claims in circulation that the tree contradicts.** The leak is fixed - do not re-open defect 18.
And the count is **17**, not 18: `Ran 18 tests` is the gate module's test count, which is how 18 keeps
reappearing. *(superseded by rev. 3.41: quote the subset property, not either number)*

---

# Rev. 3.41 — Stage 2 priority: the twelve red, ranked by what the tree is missing rather than by what is red

Measured 2026-10-06 on a **clean tree at `3c8eca7`**, `stage-1/` byte-identical to `HEAD`
(`git status --porcelain stage-1/` empty, `git diff --stat` touches only `.gitignore` and this file).
Reproduce with

```
python -m unittest discover -s tests -t .        # from stage-1/
python docs\board_verify.py
```

This revision supersedes rev 3.40's unit table for Stage 2 work. It does not restate Stage 1: Units
0, 1a, 1b, 1d, 2 (product half), 3, 4, 5 (moves contract), 7, 8 and 10 are landed and stay landed,
and rev. 3.33 remains authoritative for the seventeen names, the citations and the review history.

## Measured baseline

The loader is the only number here that is not a claim about a particular run:

```
python -c "import unittest; print(unittest.TestLoader().discover('tests',top_level_dir='.').countTestCases())"
COLLECTED 262
```

**The suite is nondeterministic on Windows by exactly one test, and the variance is a teardown
race, not a regression.** Two runs on the same commit, `3c8eca7`, with `stage-1/` clean both times:

```
Ran 262 - FAILED (failures=13, errors=1, skipped=1)      <- lock race fired
Ran 261 - FAILED (failures=12, skipped=1)                <- lock race did not fire
```

Both runs report **the same twelve named red**, one for one, by name. The difference is
`tests.test_moves.Identity.test_the_creation_time_is_not_regenerated`, which ERRORs in teardown:

```
PermissionError: [WinError 32] ... Temp\tmp...\test.sqlite
  via tempfile.TemporaryDirectory.__exit__  <- tests/support.py:160
```

`store.transaction()` closes correctly -- `ROLLBACK` then `conn.close()`, content anchor
`def transaction(conn)` in `store.py` -- so this is the `service()` fixture's `TemporaryDirectory`
racing a lingering handle on Windows only. It is the thirteenth result, and it is also what fails
the gate's subset assertion. **It will not exist in the graded image.**

So the baseline to quote is the twelve names, never a red count. A red count reported without
naming which of these two runs it came from is not a measurement. The stable part:

```
SPEC GATE (Unit 0) - subset assertion
named defects ............ 23
  red (named) ............ 12
  not yet provable ....... 0
  green .................. 11  auth_returns_display_name, dockerignore_excludes_copied_source,
                                    idempotency_key_scoped_by_path, json_responses_declare_utf8,
                                    non_ascii_digits_are_not_decimal_digits,
                                    occupancy_scoped_by_restaurant, reset_rejects_invalid_fixture,
                                    reset_spec_shaped_seeded_reservation,
                                    reset_two_restaurants_sharing_table_ids,
                                    slot_end_is_absolute_across_transitions, unknown_table_is_404
declined ................ 1   starts_at_rendered_in_restaurant_zone
```

## The invariant, restated as a property. No number.

Earlier revisions of this document held the count invariant as a number -- `17 stays 17`,
`seventeen not eighteen`, `the count is 17, not 18`. **Those lines are superseded.** A number in a
committed plan is a measurement of one afternoon, and rev. 3.40's `9 red` was found committed and
stale five commits later. The count did not fail to be true; it stopped being current.

The invariant is a **subset property**, and it is the thing the gate exists to hold:

> Every red test's name is in `NAMED_DEFECTS`, and every name in `NAMED_DEFECTS` has a test.

Both directions are already enforced by `NamedDefectsHaveTests`, so the property is checkable and is
checked on every run. It says what the count was standing in for.

The counting rules that motivated the old number still hold, restated as intent:

- **Never drop a name to hide a defect.** Deleting a red name is the one failure this gate cannot
  detect, because the gate's evidence is that name.
- **Never relax an assertion to accommodate a product bug.** `test_internal_error_message_is_redacted`
  keeps asserting a redacted message; it is not changed to expect 204.
- **Registering a genuinely-red defect is the gate working.** The Stage 2 gate landed six names;
  five arrived green because the product half already shipped, and three are red because `SS10` has no
  implementation. Registration was not a concession.

What replaced the number, once, as evidence that this is not a retreat: the six names are registered
in `test_spec_stage1.py` **at `3c8eca7`**, committed by `94f58e1` -- `export_and_import_are_served`,
`import_is_replacement_and_preserves_receipts`,
`import_rejects_a_bad_envelope_without_changing_the_destination`,
`table_ids_are_returned_in_fixture_order`, `json_responses_declare_utf8`,
`non_ascii_digits_are_not_decimal_digits`. They are kept, they were never in question, and the
count they added to is not an argument.

The twelve red:

| # | named defect | cluster | § |
|---|---|---|---|
| 1 | `export_and_import_are_served` | P0 | §10:162 |
| 2 | `import_is_replacement_and_preserves_receipts` | P0 | §10:172 |
| 3 | `import_rejects_a_bad_envelope_without_changing_the_destination` | P0 | §10:169 |
| 4 | `slot_grid_and_opening_hours_codes` | P1 | §8:126, §8:127 |
| 5 | `skipped_local_time_is_invalid_local_time` | P1 | §8:130 |
| 6 | `party_size_wrong_type_is_422` | P1 | §5:57 |
| 7 | `opening_hours_in_fixture_order` | P2 | §8:101 |
| 8 | `table_ids_are_returned_in_fixture_order` | P2 | §8:112 |
| 9 | `restaurants_list_envelope` | P3 | §8:99 |
| 10 | `list_reservations_envelope_and_desc_order` | P3 | §8:133 |
| 11 | `reservation_body_has_ends_at_and_created_at` | P3 | §8:123 |
| 12 | `internal_error_message_is_redacted` | P4 | instrument, not product |

The one declared skip is `test_a_batch_receipt_survives_an_export_import_round_trip`,
`skipped 'GET /_test/export is 404; SS10:205 is unreachable until SS10 lands'`. It is a thirteenth
member of the P0 cluster and unskips itself the day P0 lands.

## The ranking criterion, stated so it can be argued with

**Rank by how much of the requirement prose has no implementation at all, not by gate redness.** A
red name that one query clause fixes is not the same kind of problem as a red name behind which
there is no code, and putting them in one ordered list by count hides that. This is why P0 and P1
sit above P2 even though P2 is cheaper.

## What the tree contains, and who is holding it

Recorded because four different baselines circulated in one session and three were wrong. Every
line here is a command result at `3c8eca7`, not an inference from a plan.

| item | location | state |
|---|---|---|
| reset 422 for invalid fixtures | content anchor `except store.InvalidFixture as exc: raise _invalid(exc)` in `def reset` | **committed**, `94f58e1`, green |
| `JSON_CONTENT_TYPE` + both call sites | content anchor `JSON_CONTENT_TYPE = "application/json; charset=utf-8"` | **committed**, `94f58e1`, green |
| `_DATE_RE` / `_POSITIVE_INT_RE` ASCII-only | content anchors `^[0-9]{4}-[0-9]{2}-[0-9]{2}$`, `^[0-9]+$` | **committed**, `94f58e1`, green |
| occupancy scoped by restaurant | content anchor `SELECT starts_at_utc FROM reservations` | **committed**, `c55da5d`, green |
| the six Stage 2 names | `test_spec_stage1.py`, `NAMED_DEFECTS` | **committed**, `94f58e1` |
| Unit 2 extraction | `routes/service/errors/validation/render/idempotency` | **does not exist**; `app/` holds `auth`, `intervals`, `main`, `store`, `tz`. 37 SQL statements in `main.py` |
| `SS10` export/import | no route in the table | **does not exist** |
| `SS5:59` party_size wrong type | `test_a_field_of_the_wrong_type_is_malformed` asserts 400 | red, product raises `_malformed` |

Three consequences that are not negotiable and are the reason the count was never the real problem:

1. **Nothing is dirty under `stage-1/`.** Reports of uncommitted app fixes, of a changed
   `test_spec_stage2.py`, and of a 400-second window with unnamed failures were all a checkout behind
   `94f58e1`, not a second writer. Committing an `app/` change from that checkout would revert the
   entire Stage 2 body. The `NAMED_DEFECTS` tuple has exactly one owner and it has never had two.
2. **`SS10` has no owner and needs its own lane.** It is nineteen rows and the largest hole in the
   submission. At last check all four named seats -- planner, test-author, implementer, integrator --
   were `live=0`. It cannot be a slice of a unit already in flight.
3. **Unit 2 does not gate the remaining work.** `_validate_booking_fields` has exactly two callers,
   `_resolve_booking` (create and PATCH) and `post_reservation_moves`, and it never reads
   `opening_hours`. One insertion covers P1a on all three surfaces, and the grid arithmetic already
   exists in `get_availability`. A 939-line extraction ahead of that costs a pass to reach a
   one-insertion fix that turns four absent error codes into real ones. Unit 2 is a reviewability
   win; the dependency on it is a file-overlap claim, not a logical one. **Sequencing dissent is
   recorded here, not enforced by me** -- the Integrator's evidence for Unit 2 stands.

Two items are unowned and neither is anyone's current unit:

- **`SS5:16` per-request timeout is unimplemented.** No `settimeout` anywhere in `app/`. The only
  timeout is `sqlite3.connect(timeout=10.0)` and `PRAGMA busy_timeout=10000`, which is a database
  lock budget, not a request budget.
- **Root Band scaffolding is untracked and would ship on `git add -A`**: `agent.py` (launcher
  configured for `stage3_implementer`), `pyproject.toml`, a stub
  `src/tablekeeper_submission/__init__.py`, a zero-byte `README.md`, `uv.lock`, `.python-version`.
  `.gitignore` is also dirty and now contains **`agent_config.yaml` twice**; that file is not the
  Planner's and the duplicate was not committed.

## P0 — export and import does not exist. Not partially: not at all.

`app/main.py:917-931` holds the whole route table. Thirteen routes. `GET /_test/export` and
`POST /_test/import` are not among them, so both answer 404 through the normal not-found path.

```
GET  /_test/export -> 404        expected 200
POST /_test/import -> 404        expected 204
```

This is the largest unbuilt block in the submission: §10:162-181 is eighteen rows, and §11:205 adds
the batch-receipt requirement on top. §10:178 carries a warning in the requirements prose itself --
"replacing state with a fresh fixture does not satisfy this" -- which is a warning about the easiest
thing to build and get wrong. An implementation that serialises the fixture and replays it on import
passes every status code in §10 and loses every identity in it.

What must be true, in the order it is easiest to get wrong:

- `state` is an **implementation-defined object that is opaque to the caller** (§10:164). Whatever
  shape is chosen, import must accept it back **unchanged**. That forbids any normalisation step.
- Import is **replacement, not merge** (§10:167), and it is **atomic** (§10:165). The reset
  transaction helper is already `BEGIN IMMEDIATE` with `ROLLBACK`, so the same shape applies.
- Preserved across the round trip: hashed-password login, **existing bearer tokens**, fixture
  configuration, reservations, references, completed idempotent request bodies *and their original
  responses*, successful batch receipts, and a key that failed 4xx stays reusable (§10:172-176).
  Preserving tokens and receipts is the whole difficulty: it means `users`, `tokens` and the whole
  `idempotency` table must be inside `state`, not regenerated.
- Not preserved: anything created after the export must be gone (§10:179).
- A bad envelope is 422 **and changes nothing** (§10:169-170) -- so validation completes before the
  destination is touched.
- Export is a read-only atomic snapshot (§10:171). A concurrent writer must not tear it.

Do **not** put this behind the renderer work. It is independent of it and it is the only unit that
unblocks the last declared skip.

## P1 — four spec-mandated error codes appear zero times in `app/`, and two rules are not checked at all

Measured across `stage-1/app/*.py` and `stage-1/tests/*.py`:

| token | in `app/` | in `tests/` | requirement |
|---|---|---|---|
| `not_on_slot_grid` | **0** | 3 | §8:126 |
| `outside_opening_hours` | **0** | 3 | §8:127 |
| `party_exceeds_capacity` | **0** | 0 | §8:128 |
| `invalid_local_time` | **0** | 4 | §8:130 |
| `ends_at` | **0** | 23 | §8:123 |

Read that table as two different problems, because they are two different problems.

**P1a — two rules are missing, so the service accepts bookings the spec forbids.** This is wrong
behaviour, not a wrong code, and it is the reason P1 outranks P2 despite P2 being cheaper.
`_validate_booking_fields` at content anchor `def _validate_booking_fields(conn, restaurant, table_id,
starts_at_local, party_size)` checks the table exists, the party is at least 1, the table is big
enough, and the local time parses. It never consults `opening_hours` and never checks the slot grid.
Measured consequences, straight from the gate:

```
19:07 (off the 30-minute grid)   expected 422 not_on_slot_grid         got 201
12:00 (before opening)           expected 422 outside_opening_hours     got 201
22:45 (would end after closes)   expected 422 outside_opening_hours     got 201
17:30 (one slot before opens)    expected 422 not_on_slot_grid         got 201
```

Four of four. The service is booking off-grid and out-of-hours tables today. The slot-grid rule needs
`opening_hours` for the weekday plus `slot_minutes`; `get_availability` already computes exactly this
grid, so the arithmetic exists and has to be lifted into one shared helper rather than written twice.

**P1b — three codes collapse into the generic one.** `party_exceeds_capacity` and
`invalid_local_time` both go through `_invalid(...)`, i.e. 422 `validation_failed`.
`party_exceeds_capacity` is the sharper gap: it appears **nowhere at all** -- not in `app/`, and not
in one single test. It is a §8 requirement row with zero implementation and zero coverage, and the
shipped test at content anchor `def test_a_table_too_small_for_the_party_is_a_validation_failure`
actively asserts the *wrong* code for it.

**P1c — one code is the wrong status, and three shipped tests assert the wrong thing.** §5:57 names
strings and booleans for `party_size` explicitly, so a wrong-type `party_size` is 422
`validation_failed`. The product raises `_malformed`, i.e. 400. Three shipped tests encode the
product's current behaviour rather than the specification, and each one has to move in the **same
commit** as the product fix, by the same owner, or the suite is left red by the correction:

| shipped test | asserts | requirement | named defect that is red |
|---|---|---|---|
| `test_a_field_of_the_wrong_type_is_malformed` | 400 | §5:57 -> 422 | `party_size_wrong_type_is_422` |
| `test_a_nonexistent_local_time_is_rejected` | `validation_failed` | §8:130 -> `invalid_local_time` | `skipped_local_time_is_invalid_local_time` |
| `test_a_table_too_small_for_the_party_is_a_validation_failure` | `validation_failed` | §8:128 -> `party_exceeds_capacity` | **none -- name one** |

This is the first time since rev 3.37 that `main.py:NNN`-style anchors are wrong in a way that
matters, and the third row is a gap rather than a rotation: the gate has no name for
`party_exceeds_capacity`, so a fix for it moves no counter at all. That is Test Author work and it is
the cheapest high-value item in this revision.

Open question 2 from rev 3.39 is now **moot on the evidence**: three separate tests and one gate name
all resolve to 422, and §5:57 names strings explicitly, which is what makes it specific rather than
general. The 400 reading is not defensible against its own gate name.

## P2 — the fixture ordinal exists, is populated, is indexed, and nothing reads it

The schema is already finished for this. `store.py` writes `ordinal` at insert
(`INSERT INTO opening_hours (restaurant_id, weekday, opens, closes, ordinal)` and
`INSERT INTO tables (restaurant_id, id, label, capacity, ordinal)`), indexes it
(`tables_by_ordinal` UNIQUE, `opening_hours_by_ordinal`), and backfills it for pre-ordinal databases
in `_add_table_fixture_ordinals` and `_add_opening_hours_fixture_ordinals`. Two red named defects and
four legs between them, and the cause is four query clauses in `main.py`:

```
"SELECT id, label, capacity FROM tables WHERE restaurant_id = ? ORDER BY id"     -> ORDER BY ordinal
"SELECT id, capacity FROM tables WHERE restaurant_id = ? ORDER BY id"            -> ORDER BY ordinal
"... FROM opening_hours ... ORDER BY weekday"                                    -> ORDER BY ordinal
```

There is no migration to write, no backfill to reason about and no compatibility window to hold
open. This is the cheapest item in the whole revision and it is a P1-shaped requirement, because
§8:101 and §8:112 say "in the fixture's shape" and "in fixture order" in as many words.

One trap, already recorded in the Stage 2 gate: the default fixture lists `t_1, t_2, t_3`, which is
already sorted. So the shipped suite is green and the requirement is still unmet, and a fix that
only exercises the default fixture would look like it had proved nothing. Exercise `zz_1, aa_1, mm_1`.

## P3 — envelopes, descending order, and two absent response fields

- `GET /restaurants` does not wrap in `{"restaurants":[...]}` (§8:99).
- `GET /reservations` does not wrap, and orders ascending: the query is
  `SELECT * FROM reservations WHERE user_id = ? ORDER BY created_at, reference`, which is
  creation order, and §8:133 asks for `starts_at` **descending** by instant. Those are different
  orders -- a reservation created earlier can start later -- so this is a real requirement, not a
  sort-direction typo.
- `_reservation_body` emits nine keys and omits `ends_at` and `created_at` (§8:123). `slot_end` is
  imported and used for overlap arithmetic only; the value is computed four times and never
  rendered once.

Note for the record, not asserted to be a defect: `_reservation_body` sets
`"reservation_id": row["reference"]`, so the two identifiers are the same string. §8:123 lists them
as separate fields and §10:174 requires identities to be preserved. An alias satisfies every leg
the gate writes and keeps the round trip simple. Raising it would need a spec reading nobody has
asked for; leave it.

## P4 — `internal_error_message_is_redacted` is an instrument defect and must not be "fixed" in the product

Rev 3.39 diagnosed this and the measurement confirms it exactly. The redaction is intact:
`GENERIC_500_MESSAGE = "an internal error occurred"` and the catch-all returns that fixed string plus
a correlation id while logging the real text. The *test* has lost its only trigger: it reached a 500
by resetting with two restaurants sharing table ids, which used to raise
`UNIQUE constraint failed: tables.id`, and Unit 3 removed the cause, so the reset now returns 204.

```
status                               expected 500  got 204
error.code is internal_error         expected internal_error  got None
message is a fixed string            expected True  got False
a correlation id is present          expected True  got False
message leaks none of                PASSED
```

The last leg is the one this name exists for, and it passes **vacuously**: `forbidden` is a substring
scan over `message`, and `message` is `""`, so no word matches. A real `str(exc)` restored to the
catch-all would walk straight through it. Board task `#12` already covers the fix and is unassigned:
break the schema underneath a valid fixture so the handler's own SELECT raises, keep the assertion
unchanged, then put a real disclosure back and show the name go red. Relaxing the assertion to expect
204, or dropping the name, is forbidden -- 23 stays 23.

## Cross-cutting submission risks

0. **Three of the four Stage 2 roles have no live worker, so "work in parallel" cannot happen as
   instructed.** Measured with `band doctor`, not inferred from a roster:

   ```
   15 peer(s)
     test-author               Connected running=true  sessions=3 (live=1)
     integrator                Connected running=true  sessions=5 (live=1)
     stage-2-implementer       Connected running=true  sessions=3 (live=2)
     stage-2-planner           Stopped     running=false sessions=3 (live=0)   <- this session
     stage-2-test-author       Stopped     running=false sessions=3 (live=0)
     stage-2-reviewer          Stopped     running=false sessions=3 (live=0)
     stage-2-integrator        Stopped     running=false sessions=3 (live=0)
     stage-2-planner-new       Stopped     running=false sessions=2 (live=0)
     stage-2-implementer-new   Stopped     running=false sessions=2 (live=0)
     stage-2-test-author-new   Stopped     running=false sessions=2 (live=0)
     stage-2-reviewer-new      Stopped     running=false sessions=2 (live=0)
     stage-2-integrator-new    Stopped     running=false sessions=2 (live=0)
     planner / implementer / reviewer   Failed  running=false  live=0
   ```

   Only **three** of fifteen peers have a live session, and **none of them is one the owner named
   except this one**. The three that are live are the Stage 1 `test-author`, the Stage 1
   `integrator`, and `stage-2-implementer` -- which the owner did not include in the instruction.
   Every agent-scoped `jam chat` and `jam send` from this session therefore fails with
   `peer stage-2-planner has no running worker`, which is why rev 3.41 could be published but not
   delegated.

   The consequence to act on: **a room roster is not evidence that a participant is online.** A
   roster of 16 in a room with three live workers reads as full staffing and is 3/16. If the owner is
   waiting on Reviewer and Test Author verdicts, none is coming, and the owner should either restart
   those workers or route the work to `stage-2-implementer`, which is the only Stage 2 peer up.
   The rule generalises past this room: **verify `live=` before assigning, and report an unreachable
   peer as unreachable rather than as silent.**

1. **Per-request timeouts are unimplemented and unevidenced.** §2:16 wants 5 s per request and 10 s
   for `POST /_test/reset`. There is no socket or handler timeout anywhere in `app/`. Nothing tests
   it. Report as UNMEASURED, not as passing.
2. **Docker is still absent.** `Get-Command docker` returns nothing. Board task `#9` stays blocked.
   The image build, 60 s to healthy (§2:15) and the harness HTTP run are all UNRUN on this host and
   every report of them must say so.
3. **The room plan cache is one revision behind the workspace.** `plan show` names rev 3.39; the
   workspace copy is rev 3.40 until this revision is published. Read the label, never a filename.
4. **Board drift is now larger than the board suggests.** Measured: 12 tasks, 5 with an assignee, 7
   without. Of those 7, the plan records #2, #3, #4, #7, #10 and #11 as **landed**, so only #12 is
   genuinely open work. A reader who takes the board at face value will re-do six finished units.
   `work room-status` writes only your own assignment, so each owner closes their own row.
5. **The twelve red are not the whole gap.** The gate is a floor: every failure is a *named* failure,
   not every requirement a *tested* one. `party_exceeds_capacity` at P1c is the clearest row with no
   coverage at all, and §10 is eighteen rows with a gate that cannot run until P0 lands.

## What this revision changed

- Established with `band doctor` that **three of fifteen peers are live and none of the three is a
  Stage 2 role the owner asked to run in parallel**, so the parallel instruction cannot execute and
  every Reviewer/Test Author/Integrator verdict the owner is waiting on will not arrive. New
  standing rule: a roster is not evidence of presence, and an unreachable peer is reported as
  unreachable rather than as silent.
- Replaced the unit-status table with a four-cluster ranking of the twelve red, criterion stated and
  open to argument: missing implementation outranks wrong code, which outranks wrong ordering.
- Established that P0 is a total absence, not a partial, and named the three identities §10:172-176
  makes hard to preserve.
- Recorded the `app/`-versus-`tests/` token counts, which turn "these codes are wrong" into "these
  codes do not exist", and surfaced `party_exceeds_capacity` as a requirement with no test anywhere.
- Recorded that the fixture ordinal is already stored, indexed and backfilled, so P2 is four query
  clauses and no migration.
- Confirmed rev 3.39's P4 diagnosis against the current run and restated the prohibition on fixing it
  in the product.
- Named the wrong-type `party_size` question as **moot on the evidence**, superseded by the gate name
  and the three shipped tests that all resolve to 422.
