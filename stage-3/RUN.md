# Running Tablekeeper Stage 2

The service is five standard-library-only modules under `app/`, plus a single-page browser
client served from the same process. There is nothing to install.

Stage 2 keeps every stage-1 endpoint and adds combined-table bookings and the browser UI.

## Container

```sh
docker build -t tablekeeper-stage2 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage2
```

Then:

```sh
curl -s localhost:8080/health      # {"status": "ok"}
```

The build context is this directory. The container needs no network at run time; the only
build-time download is the `tzdata` wheel that backs `zoneinfo`.

## Locally, without Docker

Python 3.10 or newer, and the `tzdata` package only if the host has no system zoneinfo
database (Windows needs it):

```sh
pip install tzdata                # Windows only; skip on Linux with /usr/share/zoneinfo
python -m app.main                # listens on 0.0.0.0:$PORT, default 8080
```

```sh
curl -s localhost:8080/health
```

The database is `tablekeeper.sqlite` next to `app/`. `TABLEKEEPER_DB` overrides the path,
which is how the test suite keeps each test on its own database.

## Browser screens

The UI is one document that renders by route; no build step and no external assets. Server-side
and client-side rendering are both permitted, and this is client-side.

| Route | Screen |
| --- | --- |
| `/` | Search and availability grid, combination cells, booking form, confirmation |
| `/signup` | Signup |
| `/login` | Login |
| `/lookup` | Look up a reservation by reference and cancel it |

All API responses remain `application/json`; the four routes above return `text/html`.
The client stores its bearer token in `localStorage` and sends it on every API call.

Competing-client behaviour follows §2: searches are sequence-guarded so a late response never
overwrites a newer one; a `409 table_unavailable` on booking shows `booking-error` and refreshes
availability without discarding the form; a lost response shows `booking-uncertain` and the
unchanged form retries with the same idempotency key and body.

## Tests

```sh
python -m unittest discover -s tests -t . -v
```

No test framework beyond the standard library is needed. A run reporting `Ran 0 tests` is not
a pass; check the count.

`tests/test_spec_stage1.py` and `tests/test_spec_stage2.py` are spec gates. Each failing test is one
*named* defect, and the run ends with a `SPEC GATE` report listing red, green and not-yet-provable
names. That report is the thing to read, not the failure count: a failing test whose name is not in
the named list means the tree and the specification have diverged somewhere nobody wrote down, and the
gate fails the run over it.

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/`, `/signup`, `/login`, `/lookup` | no | the browser client |
| GET | `/health` | no | liveness |
| GET | `/restaurants` | no | fixture restaurants |
| GET | `/restaurants/{id}` | no | one restaurant, with tables and declared `combinable` pairs |
| GET | `/availability?restaurant_id=&date=&party_size=` | no | slots; all three params required |
| POST | `/auth/signup` | no | create an account, returns a token |
| POST | `/auth/login` | no | exchange credentials for a token |
| GET | `/reservations` | bearer | the caller's reservations |
| GET | `/reservations/{reference}` | bearer | one reservation |
| POST | `/reservations` | bearer | book; requires `Idempotency-Key` |
| PATCH | `/reservations/{reference}` | bearer | amend table(s), time or party; no key needed |
| POST | `/reservations/{reference}/cancel` | bearer | cancel; 200 even if already cancelled |
| POST | `/reservation-moves` | bearer | amend 1-8 bookings atomically; requires `Idempotency-Key` |
| POST | `/_test/reset` | no | replace the fixture |
| GET | `/_test/export` | no | the whole state as one importable object |
| POST | `/_test/import` | no | replace everything with an exported object; 204 |

`Idempotency-Key` is required on exactly two paths: `POST /reservations` and `POST /reservation-moves`.
A first use answers 201; a replay of the same user, method, path and body answers 200 with the original
body; the same key with a different body answers 409 `idempotency_key_reuse`.

Errors are always `{"error": {"code": ..., "message": ...}}`. Authentication is
`Authorization: Bearer <token>`.

## Combined tables

A restaurant fixture may declare `combinable` pairs; each entry is an unordered pair of table ids
in that restaurant. Pairs only, at most two tables, and combining is not transitive.

`POST /reservations` and `PATCH /reservations/{reference}` accept `table_ids` (an array of one or
two ids) as an alternative to the single `table_id`. Sending both is 422 `validation_failed`.
Responses always carry `table_ids`, and also carry `table_id` when the set has exactly one member.

`GET /availability` adds `available_options`: every single table and every declared pair whose
summed capacity seats the party and none of whose members is already held. Singles come first in
fixture order, then pairs in `combinable` order. `available_table_ids` still lists single tables only.

| Case | Response |
| --- | --- |
| The pair is not in `combinable` | 422 `combination_not_allowed` |
| More than two tables | 422 `combination_not_allowed` |
| Any table in the set is taken for an overlapping interval | 409 `table_unavailable` |
| `party_size` exceeds the combination's summed capacity | 422 `party_exceeds_capacity` |
| Duplicate table id in the set | 422 `validation_failed` |

## Export and import

```sh
curl -s localhost:8080/_test/export > state.json
curl -s -X POST localhost:8080/_test/import -H 'Content-Type: application/json' -d @state.json
```

The export answers `{"track": "tablekeeper", "format_version": 1, "state": {...}}`. `state` is
implementation-defined and must be handed back to import unchanged; import replaces the destination
wholesale - accounts, tokens, fixture configuration, bookings and idempotency receipts alike - and
answers 204. It carries credentials and session tokens, so it is a test artifact and nothing else.

Import accepts a state produced by this stage *and* an unchanged stage-1 export: a stage-1 state has
no `combinable`, no `table_ids` and no `ends_at_utc`, and each is filled with the value the stage-1
service would have derived (a single-table set, an end from the restaurant duration).

A body that does not parse is `400 malformed_request`. A missing field, a wrong `track` or
`format_version`, and a state this service could not have exported are `422 validation_failed`, and
none of them changes the destination. Repeating an import is 204 and duplicates nothing, and
`POST /_test/reset` after an import still clears everything, imported state included.
