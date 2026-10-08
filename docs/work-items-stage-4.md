# Stage 4 work items — dispatched by the Architect seat

Scope: `stage-4/` only. `stage-3/` is copied to `stage-4/` first; any `.git`
inside that copy is deleted; `Dockerfile` and `RUN.md` are kept; no earlier stage
folder (`stage-1/`, `stage-2/`, `stage-3/`) may change. Every item cites the
stage-4 specification passage it implements; when a shipped check and the
specification disagree, the specification wins and the disagreement is reported,
not patched silently.

Proof commands, from `stage-4/`:

```sh
python -m unittest discover -s tests -t .        # suites that live in the folder
python -m harness run --track tablekeeper --base-url http://127.0.0.1:<PORT> --stages 1 2 3 4 \
  --previous-base-url http://127.0.0.1:<PORT-OF-stage-3> --out <new-dir>
```

Use a private port. `8099` is contended by the QA and implementer seats and has
already produced one false failure report.

---

## WI-401 — Carry the folder forward
Copy `stage-3/` to `stage-4/`. Delete any `.git` inside the copy. Keep
`Dockerfile`, `RUN.md`, `.dockerignore`. `stage-1/`, `stage-2/` and `stage-3/`
do not change and nothing else in the repository changes (§Stage 4 preamble,
lines 3–4).
*Proof:* the stage-3 suite is green from inside `stage-4/`, and `git status`
reports no change outside `stage-4/`.

## WI-402 — Model: closures, plans and restaurant revision
Store gains the applied closures (table id with a half-open `[from,to)` interval
of explicit-offset instants), the stored plans with their assignments, and a
restaurant revision counter that starts at 0 after reset (§Seating changes after
a table closure, lines 20–23, 47–51). Each considered booking keeps its
reference, owner, party size, start, end and accepted terms unchanged by planning
and application (lines 26–29).
*Proof:* a store-schema check for closures, plans and the revision counter, plus
a reset that returns the restaurant revision to 0.

## WI-403 — `POST /restaurants/{id}/replans` preview
Requires a manager and an idempotency key. Body is `table_id`, `from`, `to` with
explicit offsets and `from < to`; an invalid interval is 422
`validation_failed`, an unknown table is 404. The proposed closure is the
half-open interval `[from,to)`. Consider every confirmed booking at this
restaurant overlapping that interval; other bookings retain their assignments
(§Seating changes after a table closure, lines 13–23). Planning supports up to 6
tables, 4 declared pairs and 6 considered bookings; larger inputs may return 422
`planning_limit`. Each considered booking must retain its identity fields and be
assigned a single or a declared pair with enough capacity under its own accepted
terms, without conflicts with fixed bookings, other assignments, previously
applied closures or the proposed closure; diners' cancellation cutoffs do not
prevent an operator repair; no booking may disappear or be cancelled (lines
24–29).
*Proof:* one check per row — manager 201, non-manager 403, no token 401, missing
key rejected, unknown table 404, reversed interval 422, a 7-table input allowed
to return 422 `planning_limit`, and a preview whose every considered booking
retains its original fields.

## WI-404 — Plan minimization order
Among feasible plans minimize, in order: (1) the number of bookings whose table
set changes; (2) total unused seats across all considered bookings, capacity
minus party size; (3) the vector of option ranks in ascending reservation-
reference order, singles ranked first in fixture order then pairs in declared
order, starting at 0 (§Seating changes after a table closure, lines 31–36).
*Proof:* a fixture with two feasible plans where the larger one wins only on
criterion 2, asserting the chosen assignment set and the reported
`moved_count`/`unused_seats`.

## WI-405 — Preview response and restaurant revision accounting
Returns 201 with `plan_id`, `restaurant_revision`, `closure`, `assignments`,
`moved_count` and `unused_seats`; assignments include every considered booking in
reference order (§Seating changes after a table closure, lines 38–47). The
restaurant revision increments once for each successful new booking, real
amendment, cancellation, policy publication or plan application, and no-op
writes, failures, previews and replays do not increment it (lines 47–50). A
preview stores only a plan: no closure, occupancy, reservation revision or
history changes. No feasible plan gives 409 `no_feasible_plan`, changing nothing
(lines 50–51).
*Proof:* take a preview and assert the restaurant revision, occupancy and every
reservation revision are untouched, then an impossible closure returning 409
`no_feasible_plan` with the same assertions.

## WI-406 — Applying a plan
`POST /restaurants/{id}/replans/{plan_id}/apply` with body `{}` requires a
manager and an idempotency key. Returns 201 with `plan_id`,
`restaurant_revision` and `reservations` including every considered booking in
reference order. An unknown plan or one from another restaurant is 404. Any
intervening restaurant revision invalidates the plan: 409 `stale_plan`, changing
nothing. A plan already applied under a different key gives 409
`plan_already_applied`; a replay of the successful key returns the original
response with 200 even after later changes. Application is atomic (§Seating
changes after a table closure, lines 53–59).
*Proof:* apply, then replay the same key (200, original body), apply again under
a new key (409 `plan_already_applied`), and apply a plan after an unrelated
booking (409 `stale_plan` with no state change).

## WI-407 — Effects of an applied plan
Application records the closure and all assignments together. Each moved booking
increments its revision once and gains one `reassigned` history entry with a
`table_ids` change and the `plan_id`; accepted terms and times remain identical;
unmoved bookings gain nothing. The restaurant revision increments once for the
whole plan. Closures thereafter exclude singles and pairs from availability and
reject creates and amendments with 409 `table_unavailable`. In explanations,
`no_overlap` is false for a closure as for a conflicting booking (§Seating
changes after a table closure, lines 61–66).
*Proof:* apply a plan, then assert one `reassigned` entry per moved booking with
its terms and times unchanged, one restaurant revision increment, a create and an
amendment refused with 409 `table_unavailable`, and `no_overlap: false` in
`explain=true` for a table under the closure.

## WI-408 — Concurrency and isolation of plans
Concurrent applications must not leave partially moved bookings, and a closure at
another restaurant does not invalidate this plan (§Seating changes after a table
closure, lines 68–69).
*Proof:* two simultaneous applies of the same plan yield one winner and one
409, with every booking either fully moved or untouched; a plan at restaurant B
stays applicable while restaurant A closes a table.

## WI-409 — `POST /series/{series_id}/amend` input handling
An owner-only idempotent write. Unknown or another owner's series is 404, no
token is 401. Body is `expected_revision`, `from_index`, `local_time`.
`expected_revision` must be a positive integer, `from_index` an integer in
0..count-1, `local_time` exactly HH:MM in 00:00..23:59, and booleans are invalid
integers. Invalid input gives 422 `validation_failed`; a mismatched series
revision gives 409 `stale_revision` before any occurrence's cutoff or booking
validation; unknown fields are ignored (§Amend recurring reservations, lines
73–83).
*Proof:* one check per validation row, the 404/401 rows, and a stale
`expected_revision` returning 409 with no occurrence touched.

## WI-410 — Amend eligibility and per-occurrence semantics
Consider indices at or after `from_index`, excluding cancelled occurrences and
those marked exception. Change their clock time on their original scheduled
local dates, retaining each reference, owner, party size and current table
selection. A change with identical resulting fields is a no-op and retains its
terms. Each real change checks its old accepted cutoff, then adopts the policy
for its resulting start date, just like an individual `PATCH` (§Amend recurring
reservations, lines 85–89).
*Proof:* amend from index 2 over a series containing a cancelled and an excepted
occurrence, asserting neither is touched, the scheduled dates are unchanged, and
a same-time amend records no history and no revision.

## WI-411 — Amend conflicts, precedence and atomicity
The resulting occurrences must not conflict with unchanged occurrences, other
bookings or applied closures. On failure, histories, idempotency records and all
revisions remain unchanged. Non-occupancy errors take precedence in
occurrence-index order; otherwise an occupancy conflict returns
`table_unavailable` (§Amend recurring reservations, lines 91–94).
*Proof:* a batch whose index 4 fails while index 7 would also fail, asserting
index 4's error is reported and no history, revision or idempotency record
changed.

## WI-412 — Amend success response and revision accounting
On success return 201 with the current series response. Each changed occurrence
gains one ordinary changed history entry and one reservation revision. The series
and restaurant revisions each increase once for the entire operation if anything
changed. Series amendments do not mark exceptions. All-no-op or empty eligible
sets succeed without changing revisions. Replay returns the original response
with 200 even after further edits or cancellations (§Amend recurring
reservations, lines 96–100).
*Proof:* a real amend, an all-no-op amend and a replay, asserting exactly one
series revision, one restaurant revision and no `exception: true` flags, with the
replay returning the first body as 200.

## WI-413 — Repairs preserve series state, and amend concurrency
Seating repairs may move series occurrences and preserve their exception flags,
scheduled dates, identities and accepted terms; each affected series revision
increases once per plan application if at least one member moved. Concurrent
amendments from the same expected revision may not both make a real change
(§Amend recurring reservations, lines 102–105).
*Proof:* a plan that moves two members of one series, asserting one series
revision increment, intact exception flags and terms, plus two concurrent amends
from one revision where at most one changes anything.

## WI-414 — Existing screens reflect an applied plan
Customers must keep their booking times, party sizes and accepted terms. No new
screens are required, and existing availability, confirmation and lookup screens
must reflect an applied plan (§Seating changes after a table closure, lines
8–11).
*Proof:* the stage-3 screen suite still passes, and after an apply the grid, the
confirmation and the lookup screen all show the new table assignment.

## WI-415 — Stage-1 to stage-3 exports stay valid
A stage-4 service accepts exports produced by the same team's stages 1–3. These
operations must support imported series, including moved and cancelled
occurrences, and earlier booking and series receipts, histories and retries
remain valid (§Amend recurring reservations, lines 107–109).
*Proof:* import a stage-3 export containing a series with a moved and a
cancelled occurrence, amend it, and replay an earlier receipt with its original
key.

## WI-416 — Verification and the overshoot check
Run suites 1, 2, 3 and 4 against `stage-4/` with `--previous-base-url` pointed at
the running `stage-3/`, and record the counts (§Stage 4 preamble, lines 3–4).
*Proof:* the four recorded harness outputs, showing green suites 1–4 with their
counts.

---

## Rules for every item

- No shipped check is edited to make the implementation pass; editing a check is
  its own item with its own justification.
- One item in flight at a time, committed at the item boundary, message carrying
  the item id.
- Hand back the pinned revision and the verbatim output of the proof command.
- Docker is not installed on this host: any container result is reported as
  **UNVERIFIED BECAUSE DOCKER IS UNAVAILABLE**, never as passing.
