# Stage 2 work items — dispatched by the Architect seat

Scope: `stage-2/` only. `stage-1/` must not change. Every item cites the stage-2
specification passage it implements; when a shipped check and the specification
disagree, the specification wins and the disagreement is reported, not patched
silently.

Proof commands, from `stage-2/`:

```sh
python -m unittest discover -s tests -t .        # suites that live in the folder
python -m harness run --track tablekeeper --base-url http://127.0.0.1:<PORT> --stages 1 2 \
  --previous-base-url http://127.0.0.1:<PORT-OF-stage-1> --out <new-dir>
```

Use a private port. `8099` is contended by the QA and implementer seats and has
already produced one false failure report.

---

## WI-201 — Carry the folder forward
Copy `stage-1/` to `stage-2/`. Delete any `.git` inside the copy. Keep
`Dockerfile`, `RUN.md`, `.dockerignore`. Nothing else in the repository changes.
*Proof:* the stage-1 suite is green from inside `stage-2/`.

## WI-202 — Model: combinable pairs
Fixture field `combinable` on a restaurant: a list of unordered pairs of table
ids in that restaurant (§Combined tables, lines 145–164). Pairs only; not
transitive. Seeded `reservations` are `confirmed` unless they carry
`"status": "cancelled"`, and may carry `table_id` **or** `table_ids`.
*Proof:* a store-schema check for the new column/field and for both seed shapes.

## WI-203 — `available_options` on `GET /availability`
Every single table and every declared pair with `capacity >= party_size` and no
overlapping confirmed reservation on any member. Singles first in fixture order,
then pairs in `combinable` order; `table_ids` inside a pair in `combinable`
order. `available_table_ids` is unchanged — singles only (lines 168–192).
*Proof:* an availability check asserting both fields together.

## WI-204 — `table_ids` on `POST /reservations`
Body takes `table_ids`; `table_id` still accepted as a set of one; both sent →
422 `validation_failed`. Responses always carry `table_ids`, and carry `table_id`
only when the set has exactly one member. Errors (lines 208–214): pair not in
`combinable` → 422 `combination_not_allowed`; more than two tables → 422
`combination_not_allowed`; any member taken → 409 `table_unavailable`;
`party_size` over summed capacity → 422 `party_exceeds_capacity`; duplicate id in
the set → 422 `validation_failed`.
*Proof:* one check per row of the case table.

## WI-205 — `table_ids` on PATCH, cancel, and moves
`PATCH /reservations/{reference}` accepts `table_ids` under the same rules.
Cancelling frees every table in the set. Stage-1 atomic moves accept `table_ids`
per move, and no table may belong to overlapping resulting bookings
(lines 216–217, 233–235).
*Proof:* a cancel that frees both members; a move batch whose resulting state is
judged, not its live state.

## WI-206 — The four screens are HTML routes
`/`, `/signup`, `/login`, `/lookup` must be reachable by URL and return HTML —
the JSON convention does not govern them (lines 9–20).
*Proof:* each route returns `text/html` with a 200, directly navigable with no
client-side routing.

## WI-207 — Signup, login, and who is signed in
`signup-email`, `signup-password`, `signup-display-name`, `signup-submit`;
`login-email`, `login-password`, `login-submit`; `auth-error` present only when
there is one; `current-user` visible on every screen when signed in and
containing the display name; `logout-button` (lines 65–74).

## WI-208 — Search and the availability grid
`restaurant-select` (option values are restaurant ids), `date-input`
(`YYYY-MM-DD`), `party-size-input`, `search-button`, `availability-grid`,
`slot-{table_id}-{HH:MM}` one cell per table per slot, `no-slots` shown instead
of the grid when the day has none (lines 76–93). Each cell carries
`data-available="true"` exactly when that table id is in that slot's
`available_table_ids` for the searched party size. An available cell opens the
booking form; an unavailable cell does nothing. Signed out, an available cell
shows `auth-error` or navigates to `/login`.

## WI-209 — Booking form and re-submission identity
`booking-form`, `booking-summary` (table label and local start time),
`booking-party-size` prefilled from the search, `booking-submit`, `booking-error`
(lines 95–107). After success the form stays on screen. Submitting again without
changing a field returns the same `confirmation-reference`, with no
`booking-error` and no second booking. Changing a field makes the next
submission a new request. Retries follow §7.

## WI-210 — Confirmation
`confirmation` container; `confirmation-reference` text is **exactly** the
reference with no surrounding words; `confirmation-details` contains the
restaurant name, table label and local start time (lines 109–117).

## WI-211 — Lookup
`lookup-reference-input`, `lookup-submit`, `reservation-detail`,
`reservation-status` text exactly `confirmed` or `cancelled`,
`reservation-cancel-button` (absent once cancelled), `reservation-error` when not
found or when a cancel is refused (lines 119–127).

## WI-212 — Competing clients and uncertain outcomes
Three behaviours (lines 22–39):
1. Out-of-order searches — if A starts before B but finishes after it, the grid,
   labels and form describe B. A late response must not restore A.
2. `409 table_unavailable` → show `booking-error` and refresh availability,
   preserve the selected form and its inputs, show no confirmation.
3. Lost response (including after the commit) → nonempty `booking-uncertain`,
   no `booking-error`, no new confirmation; the unchanged form retries with the
   **same idempotency key and body**; success removes the uncertainty/error
   elements and shows the original reference; a confirmed rejection uses
   `booking-error`.
No background polling, no cross-tab sync, no recovery across reload; the server
stays authoritative.

## WI-213 — Combination cells in the grid
`slot-{t_a}+{t_b}-{HH:MM}` with ids in `combinable` order, carrying
`data-available` like a single cell, shown when the pair is available for the
searched party size; `confirmation-tables` and `reservation-tables` contain every
table label; `booking-summary` names every table in the selection (lines 219–231).

## WI-214 — Existing clients after an upgrade
A stage-2 service accepts a stage-1 export; a browser signed in before the
upgrade stays signed in; a retained reference still resolves in lookup; a booking
whose response was lost before export stays retryable after import with the same
body and key and recovers the original confirmation; the form and the pending
retry identity survive the upgrade (lines 129–137).

## WI-215 — Product quality and responsiveness
Warm, coherent, presentation-ready restaurant product; consistent visual system;
primary actions obvious; available / unavailable / selected / loading / success /
refused / uncertain states visually distinct; human-readable labels prominent
(lines 45–63). Usable at a 375 px viewport and at desktop widths with no
horizontal page scrolling, visible input labels, apparent keyboard focus,
sufficient contrast, considered empty/loading/error states, consistent navigation.

## WI-216 — Verification and the overshoot check
Run suites 1 and 2 against `stage-2/` with `--previous-base-url` pointed at the
running `stage-1/`. Record the counts. Then run suite 3 against `stage-2/`: it
must **not** pass, because `stage-2/` is not a stage-3 answer.

---

## Rules for every item

- No shipped check is edited to make the implementation pass; editing a check is
  its own item with its own justification.
- One item in flight at a time, committed at the item boundary, message carrying
  the item id.
- Hand back the pinned revision and the verbatim output of the proof command.
- Docker is not installed on this host: any container result is reported as
  **UNVERIFIED BECAUSE DOCKER IS UNAVAILABLE**, never as passing.
