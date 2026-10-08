# Stage 3 work items — dispatched by the Architect seat

Scope: `stage-3/` only. `stage-2/` is copied to `stage-3/` first; any `.git`
inside that copy is deleted; `Dockerfile` and `RUN.md` are kept; no earlier stage
folder (`stage-1/`, `stage-2/`) may change. Every item cites the stage-3
specification passage it implements; when a shipped check and the specification
disagree, the specification wins and the disagreement is reported, not patched
silently.

Proof commands, from `stage-3/`:

```sh
python -m unittest discover -s tests -t .        # suites that live in the folder
python -m harness run --track tablekeeper --base-url http://127.0.0.1:<PORT> --stages 1 2 3 \
  --previous-base-url http://127.0.0.1:<PORT-OF-stage-2> --out <new-dir>
```

Use a private port. `8099` is contended by the QA and implementer seats and has
already produced one false failure report.

---

## WI-301 — Carry the folder forward
Copy `stage-2/` to `stage-3/`. Delete any `.git` inside the copy. Keep
`Dockerfile`, `RUN.md`, `.dockerignore`. `stage-1/` and `stage-2/` do not change
and nothing else in the repository changes (§Stage 3 preamble, lines 3–4).
*Proof:* the stage-2 suite is green from inside `stage-3/`, and `git status`
reports no change outside `stage-3/`.

## WI-302 — Model: managers, policies and revisions
Restaurant fixture gains `manager_user_ids`, default `[]` (§Policies and
accepted terms, lines 96–99). Store gains an immutable per-restaurant policy list
with a `policy_version` counter that is an integer starting at 1 and increasing
by one per restaurant; policy 0 is the original fixture's rules and applies
before any published policy (lines 115–122). Every reservation gains `revision`,
1 at creation, and `accepted_terms`, the snapshot of the policy it accepted
(lines 136–147).
*Proof:* a store-schema check for the new fixture field, for the policy records
and their version counter, and for both reservation fields.

## WI-303 — `explain=true` on `GET /availability`
`explain` is optional and its only accepted value is `true`; `false`, `1` and the
empty string are 422 `validation_failed`. Without it the response keeps stage 1's
shape, with no explanation fields at all. With it, every slot carries one further
field, an `explain` array with the shape shown (§Availability explanations, lines
20–42).
*Proof:* one availability check per accepted and rejected `explain` value,
asserting the stage-1 shape byte-for-byte when the parameter is absent.

## WI-304 — Explain invariants
Every table of the restaurant appears exactly once in `explain`, available or
not, in fixture order, the same order `available_table_ids` uses. Both rules are
reported for every table in the order `capacity` then `no_overlap`; a rule that
holds is reported holding, a table excluded by both reports both false, no rule
may be omitted. `available` is true exactly when both rules hold, and the
`table_id`s whose `available` is true are exactly `available_table_ids` in the
same order. A closed day still returns `"slots": []`, and a slot with no
available table still appears with a full `explain` for every table (lines
44–51).
*Proof:* an availability check comparing `explain` with `available_table_ids`
set-for-set and order-for-order on a partly booked day and on a closed day.

## WI-305 — Reservation history endpoint
`GET /reservations/{reference}/history` returns the reservation's own record,
oldest first: a `reference` and its `entries` (§Reservation history, lines 55–61,
66–77). Only its owner may read it; anyone else, signed in or not, gets the same
404 `not_found` that §8 gives for a reservation that is not theirs, and history
and decision 404 even without authentication (lines 59–61, 162–166). A cancelled
reservation still has its history (lines 59–61).
*Proof:* the owner reads 200 with entries; a stranger with a token, a stranger
with no token and a wrong reference all get 404; a cancelled reference still
returns its entries.

## WI-306 — History entry rules
`seq` starts at 1 and increases by exactly 1, so the order is total even when two
writes land in the same second; entries are returned in `seq` order, which is
also `at` order. `created` names all three fields with `"from": null`. `changed`
names only the fields that actually changed, in the order `table_id`,
`starts_at_local`, `party_size`; a `PATCH` that sets a field to the value it
already has still succeeds and records no entry at all. `cancelled` carries an
empty `changes` and nothing follows it. Replaying an idempotent
`POST /reservations` records nothing (§Reservation history, lines 79–87).
*Proof:* create, no-op patch, real patch, cancel and a replayed create, asserting
`seq` runs 1..n with no gaps and that each entry's `changes` match exactly.

## WI-307 — Publishing a policy
`POST /restaurants/{id}/policies` requires an idempotency key with stage 1's
replay rules and accepts a complete policy, not a patch (§Policies and accepted
terms, lines 101–113). Unknown restaurant is 404, an authenticated non-manager is
403 `forbidden`, no token is 401 (lines 96–99). Returns 201 with the supplied
policy plus `policy_version` (lines 115–116).
*Proof:* one check per row — manager 201, non-manager 403, no token 401, unknown
restaurant 404, missing key rejected as stage 1 rejects it, and a replay under
the same key returns the original 201 while allocating no version.

## WI-308 — Policy validation, versioning and selection
All fields are required. `effective_from` is an actual `YYYY-MM-DD` date;
`slot_minutes` and `reservation_duration_minutes` are integers 1..1440;
`cancellation_cutoff_minutes` is an integer 0..10080; `opening_hours` follow
stage 1 and contain no duplicate weekdays; `capacities` names exactly the
restaurant's table ids with integer capacities 1..100. Booleans are not integers.
Invalid policy is 422 `validation_failed` with no version or state change;
unknown fields are ignored; table ids, labels, timezone and declared combinations
cannot be changed by a policy (lines 124–129). Failed writes and replays allocate
no version; policies are immutable; publication order may differ from
effective-date order. For a booking's local start date choose the greatest
`effective_from` not later than that date, ties choosing the greatest
`policy_version`; a new same-date policy supersedes the old one for future
decisions without changing any accepted reservation; effective dates may be in
the past and publication never retroactively edits a booking (lines 115–122).
*Proof:* one 422 check per validation row asserting no version was consumed, plus
a two-policy fixture where the selected `policy_version` flips exactly at the
effective date.

## WI-309 — Listing policies and `policy_version` in explanations
`GET /restaurants/{id}/policies` is public and returns `{"policies": [...]}` in
publication order, omitting policy 0. The ordinary restaurant detail still
returns its original fixture configuration, while availability and booking
decisions use the selected policy, not that detail. With `explain=true` each
table explanation additionally identifies its `policy_version` (§Policies and
accepted terms, lines 131–134).
*Proof:* an unauthenticated list returns exactly the published policies in
publication order, the restaurant detail payload is unchanged, and every
`explain` entry names the selected `policy_version`.

## WI-310 — `revision` and `accepted_terms` on every response
Every reservation response gains `revision`, 1 at creation, and
`accepted_terms`, a snapshot of the entire selected policy excluding
`effective_from`. Seeded bookings start at revision 1 under policy 0. Responses
to old idempotency keys remain the original response, including the original
revision and terms (§Policies and accepted terms, lines 136–147).
*Proof:* create, read and replay one booking and assert the same `revision` and
identical `accepted_terms` in all three responses.

## WI-311 — Amendments and cancellations under a policy
A policy publication changes no existing booking, its end time or its history.
Cancel checks the accepted cutoff against the current start and increments
revision once; repeated cancel does not. A real amendment of time, tables or
party size checks the old accepted cutoff first, then validates all resulting
fields against the policy applicable to the resulting start date, atomically
replacing accepted terms and end time and incrementing revision once. A no-op
amendment retains terms, end time and revision and records no history; it still
requires a confirmed, editable booking. Failed amendments change nothing.
`PATCH` optionally accepts `expected_revision`: a positive integer differing from
the current revision gives 409 `stale_revision` before cutoff or validation, an
invalid type or range gives 422, omission retains stage 1 semantics, and of two
concurrent amendments using one revision at most one real change succeeds;
unrelated unknown fields remain ignored (§Policies and accepted terms, lines
149–160).
*Proof:* one check per bullet — publication leaves an existing booking's response
untouched, cutoff refusal on cancel, terms, end time and revision replaced in one
step, a no-op patch with no history entry, and a stale `expected_revision`
returning 409.

## WI-312 — Decision endpoint and per-entry terms
Each history entry additionally carries the reservation's resulting `revision`
and complete `accepted_terms`, and old entries never acquire newer terms.
`GET /reservations/{reference}/decision` returns
`{"reference": "...", "revision": 1, "accepted_terms": {...}}` for the current
booking, including after cancellation, with history's owner-only 404 rule,
404 even without authentication (§Policies and accepted terms, lines 63–64,
162–166).
*Proof:* amend a booking, then assert the first entry keeps the old terms while
the later entry and `/decision` carry the new ones, and that a stranger gets 404
from both routes with and without a token.

## WI-313 — `POST /series` adoption
`POST /series` adopts an existing reservation as occurrence zero of a recurring
agreement and requires an idempotency key; the body is `anchor_reference`,
`count`, `interval_weeks` (§Recurring reservations, lines 170–175). The anchor
must belong to the caller, be confirmed and satisfy its accepted cancellation
cutoff: unknown or another owner's anchor gives 404 `not_found`, cancelled gives
409 `reservation_cancelled`, already adopted gives 409 `already_in_series`.
`count` is an integer 2..12 including the anchor, `interval_weeks` an integer
1..4; invalid values including booleans give 422 `validation_failed`; no token
gives 401 (lines 177–181).
*Proof:* one check per error row, plus a successful adoption that replays under
the same key to the original 201.

## WI-314 — Generated occurrences and all-or-nothing failure
Occurrence zero is the anchor itself: its reference, identity, revision, terms,
history, timestamps and original idempotent response remain unchanged. Occurrence
i starts on the anchor's local calendar date plus i × interval_weeks × 7 days, at
the same local clock time. Each generated occurrence independently selects its
date's policy, including duration and capacity, and obeys ordinary opening, DST
and occupancy rules. A nonexistent local time rejects the entire adoption with
`invalid_local_time`; repeated times use stage 1's first occurrence rule.
Generated occurrences use the anchor's party size and table selection. No partial
series, reservations, histories, counters or idempotency claim survive failure,
and the first failing occurrence in index order determines the ordinary booking
error (§Recurring reservations, lines 183–191).
*Proof:* an 8-week series spanning a policy boundary and a DST change, and a
series whose index 3 fails, leaving no reservation, no history, no counter bump
and a still-retryable key.

## WI-315 — Series response and owner-only read
Return 201 with `series_id`, `revision`, `interval_weeks` and `occurrences`; the
array includes all `count` occurrences in index order, each with its `index`, a
distinct ordinary reservation `reference`, `exception` and an ordinary
reservation response (§Recurring reservations, lines 193–199). References and
indices never change when dates or tables change; occurrences appear in ordinary
reservation lists, occupy tables and have ordinary histories.
`GET /series/{series_id}` returns this shape with current reservation states;
only the owner may read it, another user or no token gives 404 `not_found`
(lines 201–205).
*Proof:* adopt a series, find an occurrence in `GET /reservations`, then read the
series as the owner (200), as another user (404) and with no token (404).

## WI-316 — Series exceptions, revisions and replays
A real individual PATCH permanently marks that occurrence `exception: true` and
increments the series revision once; a no-op or failure changes neither.
Cancellation increments the series revision once, retains the cancelled
occurrence and does not mark it an exception; repeated cancel does nothing.
Cancelling the anchor does not cancel its siblings. Ordinary cutoff and revision
checks still apply. Adoption increments the restaurant revision once for the
whole operation. Replays return the original series response even after later
changes, and change no counter. Series creation adds one idempotent write path
and unknown fields are ignored (§Recurring reservations, lines 207–214).
*Proof:* patch one occurrence and cancel another, then assert both series
revision counts, the exception flags, and that a replayed adoption returns the
first response without moving any counter.

## WI-317 — Combined-table history
Stage-3 accepted terms apply to combinations, capacity being the sum of the
selected policy's capacities. In history, single-to-single operations keep
stage-3 fields; for a creation of a pair the `table_id` change is replaced by
`table_ids`, from null to the pair; for a change involving a pair, `table_ids`
with complete before/after lists is used instead of `table_id`. Table-set order
is the declared combination order. A reversed input pair names the same set and
is not an amendment on its own. Policy selection, revision and replay rules are
unchanged (§Combined-table history, lines 220–228).
*Proof:* create a pair, amend it, and send a reversed-pair `PATCH` that succeeds
while recording no history entry and no revision.

## WI-318 — Collective moves under policies and agreements
Each real change in `POST /reservation-moves` uses individual PATCH semantics:
check the old accepted cutoff, then adopt the resulting date's policy. Per-move
`expected_revision` is optional and follows PATCH validation and stale-revision
rules. A no-op retains its terms and history. All resulting bookings must satisfy
amendment and occupancy rules; failure leaves every booking unchanged. Every
changed booking gains one revision and one changed history entry, and the
restaurant revision increases once for the whole batch. Each affected series
revision increases once and each changed series occurrence becomes a permanent
diner exception. A failed batch or replay changes no revisions, histories or
exception flags (§Collective moves under policies and agreements, lines 230–240).
*Proof:* a two-move batch whose second move fails, asserting nothing changed,
then a successful batch asserting one revision per changed booking, one
restaurant revision, one series revision and the exception flag set.

## WI-319 — Existing screens unchanged
No new screens are required for explanations or history; the availability grid
continues to follow the stage-2 rules (§Existing screens, lines 89–92).
*Proof:* the stage-2 screen suite passes unchanged from inside `stage-3/`.

## WI-320 — Stage-1 and stage-2 exports stay valid
A stage-3 service accepts exports produced by the same team's stage-1 or stage-2
service; adoption must work on reservations imported this way; existing
confirmation links, sessions and original booking retries remain valid
(§Recurring reservations, lines 216–218).
*Proof:* import a stage-2 export, adopt one of its reservations as a series
anchor, and retry a pre-export lost response with its original body and
idempotency key.

## WI-321 — Verification and the overshoot check
Run suites 1, 2 and 3 against `stage-3/` with `--previous-base-url` pointed at
the running `stage-2/`, and record the counts. Then run suite 4 against
`stage-3/` and require that it does NOT pass, because `stage-3/` is not a
stage-4 answer (§Stage 3 preamble, lines 3–4).
*Proof:* the four recorded harness outputs, showing green suites 1–3 with their
counts and a failing suite 4.

---

## Rules for every item

- No shipped check is edited to make the implementation pass; editing a check is
  its own item with its own justification.
- One item in flight at a time, committed at the item boundary, message carrying
  the item id.
- Hand back the pinned revision and the verbatim output of the proof command.
- Docker is not installed on this host: any container result is reported as
  **UNVERIFIED BECAUSE DOCKER IS UNAVAILABLE**, never as passing.
