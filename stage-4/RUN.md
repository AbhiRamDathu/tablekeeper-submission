# Running Tablekeeper Stage 4

The service is standard-library-only modules under `app/`, plus a single-page
browser client served from the same process. There is nothing to install.

Stage 4 keeps every stage-1, stage-2 and stage-3 endpoint and adds seating
changes after a table closure (deterministic, previewable, atomic re-planning)
and amendments to recurring series.

## Container

```sh
docker build -t tablekeeper-stage4 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage4
```

Then:

```sh
curl -s localhost:8080/health      # {"status": "ok"}
```

The build context is this directory. The container needs no network at run time;
the only build-time download is the `tzdata` wheel that backs `zoneinfo`.

## Locally, without Docker

Python 3.10 or newer, and the `tzdata` package only if the host has no system
zoneinfo database (Windows needs it):

```sh
pip install tzdata                # Windows only; skip on Linux with /usr/share/zoneinfo
python -m app.main                # listens on 0.0.0.0:$PORT, default 8080
```

```sh
curl -s localhost:8080/health
```

The database is `tablekeeper.sqlite` next to `app/`. `TABLEKEEPER_DB` overrides
the path, which is how the test suite keeps each test on its own database.

## Browser screens

The UI is one document that renders by route; no build step and no external
assets. Server-side and client-side rendering are both permitted, and this is
client-side.

| Route | Screen |
| --- | --- |
| `/` | Search and availability grid, combination cells, booking form, confirmation |
| `/signup` | Signup |
| `/login` | Login |
| `/lookup` | Look up a reservation by reference and cancel it |

All API responses remain `application/json`; the four routes above return
`text/html`. The client stores its bearer token in `localStorage` and sends it on
every API call. Existing availability, confirmation and lookup screens reflect an
applied re-plan.

Competing-client behaviour follows stage 2: searches are sequence-guarded so a
late response never overwrites a newer one; a `409 table_unavailable` on booking
shows `booking-error` and refreshes availability without discarding the form; a
lost response shows `booking-uncertain` and the unchanged form retries with the
same idempotency key and body.

## Tests

```sh
python -m unittest discover -s tests -t . -v
```

No test framework other than the standard library is needed. A run reporting
`Ran 0 tests` is not a pass; check the count.

## Endpoints added in stage 4

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/restaurants/{id}/replans` | manager | preview a re-seating plan for a table closure; requires `Idempotency-Key` |
| POST | `/restaurants/{id}/replans/{plan_id}/apply` | manager | apply a previewed plan atomically; requires `Idempotency-Key` |
| POST | `/series/{series_id}/amend` | owner | change the clock time of eligible occurrences; requires `Idempotency-Key` |

All stage-3 endpoints remain: `GET /availability?...&explain=true`,
`GET /reservations/{reference}/history`, `GET /reservations/{reference}/decision`,
`POST`/`GET /restaurants/{id}/policies`, `POST /series`, `GET /series/{series_id}`,
plus every stage-1/2 endpoint.

## Seating changes after a table closure

`POST /restaurants/{id}/replans` body
`{"table_id": ..., "from": "<RFC3339 with offset>", "to": "<RFC3339 with offset>"}`
considers every confirmed booking overlapping the half-open closure `[from, to)`.
`from < to` is required (422 `validation_failed` otherwise) and an unknown table
is 404. Planning supports up to 6 tables, 4 declared pairs and 6 considered
bookings; larger inputs return 422 `planning_limit`.

Each considered booking keeps its reference, owner, party size, start, end and
accepted terms, and is assigned a single or declared pair with enough capacity
under **its own accepted terms**, free of conflicts with fixed bookings, other
assignments, previously applied closures and the proposed closure. Diners'
cancellation cutoffs do not prevent an operator repair, and no booking disappears
or is cancelled.

Among feasible plans the service minimises, in order: (1) the number of bookings
whose table set changes, (2) total unused seats, (3) the vector of option ranks
in ascending reference order. Returns 201 with `plan_id`, `restaurant_revision`,
`closure`, `assignments` (every considered booking in reference order) and
`moved_count` / `unused_seats`. No feasible plan is 409 `no_feasible_plan`, and a
preview changes no closure, occupancy, revision or history.

`POST /restaurants/{id}/replans/{plan_id}/apply`, body `{}`, returns 201 with
`plan_id`, `restaurant_revision` and every considered reservation in reference
order. An unknown or foreign plan is 404; any intervening restaurant revision is
409 `stale_plan`; a plan already applied under another key is 409
`plan_already_applied`; a replay of the successful key returns the original
response with 200. Application is atomic: each moved booking gains one revision
and one `reassigned` history entry (a `table_ids` change plus `plan_id`), times
and accepted terms unchanged; the restaurant revision increments once for the
whole plan. Applied closures then exclude singles and pairs from availability and
reject creates/amendments with 409 `table_unavailable`; in explanations
`no_overlap` is false for a closure as for a conflicting booking.

A restaurant revision starts at 0 after reset and increments once for each
successful new booking, real amendment, cancellation, policy publication or plan
application. No-op writes, failures, previews and replays do not increment it.

## Amending recurring reservations

`POST /series/{series_id}/amend` body
`{"expected_revision": <int>, "from_index": <int>, "local_time": "HH:MM"}`.
Revision must be a positive integer, `from_index` an integer in `0..count-1`, and
`local_time` exactly `HH:MM` in `00:00..23:59`; booleans are invalid. Invalid
input is 422 `validation_failed`; a mismatched series revision is 409
`stale_revision` before any occurrence's cutoff or booking validation.

Indices at or after `from_index`, excluding cancelled and exception-marked
occurrences, change their clock time on their original scheduled local dates,
retaining reference, owner, party size and current table selection. An identical
result is a no-op retaining its terms; a real change checks its old accepted
cutoff, then adopts the policy for its resulting start date, like an individual
PATCH. The result must not conflict with unchanged occurrences, other bookings or
applied closures; on failure all histories, idempotency records and revisions are
unchanged. Non-occupancy errors take precedence in occurrence-index order,
otherwise an occupancy conflict is `table_unavailable`.

On success the current series response is returned with 201; each changed
occurrence gains one ordinary `changed` history entry and one reservation
revision, and the series and restaurant revisions each increase once if anything
changed. Series amendments do not mark exceptions. Seating repairs may move series
occurrences, preserving exception flags, scheduled dates, identities and accepted
terms.

## Earlier-stage surface

The service still provides, unchanged in meaning: `GET /restaurants`,
`GET /restaurants/{id}`, `GET /availability`, signup/login, `GET /reservations`,
`GET`/`POST`/`PATCH /reservations...`, `POST /reservations/{reference}/cancel`,
`POST /reservation-moves` (atomic, 1..8 bookings), `POST /_test/reset`,
`GET /_test/export`, `POST /_test/import`, combined-table bookings and
`available_options`, manager policies and `accepted_terms`/`revision`,
`/history`, `/decision`, and recurring series.

`Idempotency-Key` is required on every write path named in the specification
(`POST /reservations`, `POST /reservation-moves`, `POST /restaurants/{id}/policies`,
`POST /series`, the two replan calls, and `POST /series/{id}/amend`). A first use
answers 201; a replay of the same user, method, path and body answers 200 with the
original body; the same key with a different body answers 409
`idempotency_key_reuse`.

Errors are always `{"error": {"code": ..., "message": ...}}`. Authentication is
`Authorization: Bearer <token>`.

## Export and import

```sh
curl -s localhost:8080/_test/export > state.json
curl -s -X POST localhost:8080/_test/import -H 'Content-Type: application/json' -d @state.json
```

The export answers `{"track": "tablekeeper", "format_version": 1, "state": {...}}`.
`state` is implementation-defined and must be handed back to import unchanged;
import replaces the destination wholesale — accounts, tokens, fixture
configuration, policies, series, bookings, applied closures and idempotency
receipts alike — and answers 204.

A stage-4 service accepts an unchanged export produced by this team's stages 1–3,
including imported series with moved and cancelled occurrences, and earlier
booking and series receipts, histories and retries remain valid. A body that does
not parse is `400 malformed_request`. A missing field, a wrong `track` or
`format_version`, and a state this service could not have exported are 422
`validation_failed`, and none of them changes the destination. Repeating an
import is 204 and duplicates nothing, and `POST /_test/reset` after an import
still clears everything, imported state included.
