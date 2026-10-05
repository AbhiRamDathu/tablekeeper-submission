# Running Tablekeeper Stage 1

The service is five standard-library-only modules under `app/`. There is nothing to install.

## Container

```sh
docker build -t tablekeeper-stage1 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage1
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
| GET | `/health` | no | liveness |
| GET | `/restaurants` | no | fixture restaurants |
| GET | `/restaurants/{id}` | no | one restaurant, with tables |
| GET | `/availability?restaurant_id=&date=&party_size=` | no | slots; all three params required |
| POST | `/auth/signup` | no | create an account, returns a token |
| POST | `/auth/login` | no | exchange credentials for a token |
| GET | `/reservations` | bearer | the caller's reservations |
| GET | `/reservations/{reference}` | bearer | one reservation |
| POST | `/reservations` | bearer | book; requires `Idempotency-Key` |
| PATCH | `/reservations/{reference}` | bearer | amend table, time or party; no key needed |
| POST | `/reservations/{reference}/cancel` | bearer | cancel; 200 even if already cancelled |
| POST | `/reservation-moves` | bearer | amend 1–8 bookings atomically; requires `Idempotency-Key` |
| POST | `/_test/reset` | no | replace the fixture |

`Idempotency-Key` is required on exactly two paths: `POST /reservations` and `POST /reservation-moves`.
A first use answers 201; a replay of the same user, method, path and body answers 200 with the original
body; the same key with a different body answers 409 `idempotency_key_reuse`.

Errors are always `{"error": {"code": ..., "message": ...}}`. Authentication is
`Authorization: Bearer <token>`.

## Not implemented

`GET /_test/export` and `POST /_test/import` — the whole of REQUIREMENTS.md §10 — answer
`404 not_found`. No route is registered for either. They are listed here so the gap is visible in
the documentation rather than discovered by a caller.