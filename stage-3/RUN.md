# Running Tablekeeper Stage 3

The service is standard-library-only modules under `app/`, plus a single-page
browser client served from the same process. There is nothing to install.

Stage 3 keeps every stage-1 and stage-2 endpoint and adds availability
explanations, reservation history and decision records, effective-dated manager
policies with accepted terms and revisions, and recurring reservation series.

## Container

```sh
docker build -t tablekeeper-stage3 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage3
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
every API call.

Competing-client behaviour follows stage 2: searches are sequence-guarded so a
late response never overwrites a newer one; a `409 table_unavailable` on booking
shows `booking-error` and refreshes availability without discarding the form; a
lost response shows `booking-uncertain` and the unchanged form retries with the
same idempotency key and body.

## Tests

```sh
python -m unittest discover -s tests -t . -v
```

No test framework beyond the standard library is needed. A run reporting
`Ran 0 tests` is not a pass; check the count.

`tests/test_spec_stage1.py` and `tests/test_spec_stage2.py` are spec gates. Each
failing test is one *named* defect, and the run ends with a `SPEC GATE` report
listing red, green and not-yet-provable names.

## Endpoints added in stage 3

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/availability?...&explain=true` | no | per-table `capacity` / `no_overlap` explanation with `policy_version`; only `true` is accepted |
| GET | `/reservations/{reference}/history` | owner | the reservation's own record, oldest first, with `seq`, `revision`, `accepted_terms` |
| GET | `/reservations/{reference}/decision` | owner | current `revision` + `accepted_terms` (also after cancellation) |
| POST | `/restaurants/{id}/policies` | manager | publish an immutable, effective-dated policy; requires `Idempotency-Key` |
| GET | `/restaurants/{id}/policies` | no | published policies in publication order (policy 0 omitted) |
| POST | `/series` | owner | adopt a confirmed reservation as occurrence zero of a recurring agreement; requires `Idempotency-Key` |
| GET | `/series/{series_id}` | owner | the series with current reservation states |

Every reservation response also carries `revision` (1 at creation) and
`accepted_terms`, a snapshot of the policy selected for the booking's local start
date. `PATCH /reservations/{reference}` optionally takes `expected_revision`; a
positive integer that differs from the current revision gives 409
`stale_revision` before cutoff or validation, and an invalid value is 422.

A `manager_user_ids` list in a restaurant fixture (default `[]`) grants policy
publication to those users only; unknown restaurant is 404, an authenticated
non-manager is 403 `forbidden`, and no token is 401. Managers gain no access to
other diners' private lookup or history.

## Policy selection and history

For a booking's local start date the service picks the greatest `effective_from`
not later than that date, ties broken by the greatest `policy_version`. Policy 0
is the original fixture rules and applies before any published policy. Policies
are immutable and publication never retroactively edits an accepted booking.

History records `created` (naming all three fields, each `"from": null`),
`changed` (only the fields that actually changed, in the order `table_id`,
`starts_at_local`, `party_size`), and `cancelled` (empty `changes`, last). A
no-op `PATCH` records no entry. A pair creation/change uses `table_ids` in place
of `table_id`.

## Recurring reservations

`POST /series` takes `{"anchor_reference": ..., "count": 2..12, "interval_weeks":
1..4}`. Occurrence `i` starts on the anchor's local date plus
`i × interval_weeks × 7` days at the same local time, each generated occurrence
selecting its own date's policy and obeying ordinary opening, DST and occupancy
rules. Occurrence zero — the anchor — keeps its reference, revision, terms,
history and original idempotent response. A failure leaves no partial series. A
real individual `PATCH` marks the occurrence `exception: true` and increments the
series revision once; cancellation retains the cancelled occurrence without
marking an exception.

## Combined tables

A restaurant fixture may declare `combinable` pairs; each entry is an unordered
pair of table ids in that restaurant. Pairs only, at most two tables, and
combining is not transitive.

`POST /reservations` and `PATCH /reservations/{reference}` accept `table_ids` (an
array of one or two ids) as an alternative to the single `table_id`. Sending both
is 422 `validation_failed`. Responses always carry `table_ids`, and also carry
`table_id` when the set has exactly one member.

`GET /availability` adds `available_options`: every single table and every
declared pair whose summed capacity seats the party and none of whose members is
already held. Singles come first in fixture order, then pairs in `combinable`
order. `available_table_ids` still lists single tables only.

| Case | Response |
| --- | --- |
| The pair is not in `combinable` | 422 `combination_not_allowed` |
| More than two tables | 422 `combination_not_allowed` |
| Any table in the set is taken for an overlapping interval | 409 `table_unavailable` |
| `party_size` exceeds the combination's summed capacity | 422 `party_exceeds_capacity` |
| Duplicate table id in the set | 422 `validation_failed` |

Under stage 3 a combination's capacity is the sum of the **selected policy's**
capacities, and combined-table history uses `table_ids` in place of `table_id`.

## Export and import

```sh
curl -s localhost:8080/_test/export > state.json
curl -s -X POST localhost:8080/_test/import -H 'Content-Type: application/json' -d @state.json
```

The export answers `{"track": "tablekeeper", "format_version": 1, "state": {...}}`.
`state` is implementation-defined and must be handed back to import unchanged;
import replaces the destination wholesale — accounts, tokens, fixture
configuration, policies, series, bookings and idempotency receipts alike — and
answers 204.

A stage-3 service accepts an unchanged export produced by this team's stage-1 or
stage-2 service; adoption (`POST /series`) works on reservations imported that
way, and existing confirmation links, sessions and original booking retries
remain valid. A body that does not parse is `400 malformed_request`. A missing
field, a wrong `track` or `format_version`, and a state this service could not
have exported are `422 validation_failed`, and none of them changes the
destination. Repeating an import is 204 and duplicates nothing, and
`POST /_test/reset` after an import still clears everything, imported state
included.
