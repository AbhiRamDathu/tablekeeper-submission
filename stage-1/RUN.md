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

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | no | liveness |
| GET | `/restaurants` | no | fixture restaurants |
| GET | `/restaurants/{id}` | no | one restaurant, with tables |
| GET | `/restaurants/{id}/availability` | no | slots for `date` and `party_size` |
| POST | `/auth/signup` | no | create an account, returns a token |
| POST | `/auth/login` | no | exchange credentials for a token |
| GET | `/reservations` | bearer | the caller's reservations |
| GET | `/reservations/{reference}` | bearer | one reservation |
| POST | `/reservations` | bearer | book; requires `Idempotency-Key` |
| POST | `/_test/reset` | no | replace the fixture |

Errors are always `{"error": {"code": ..., "message": ...}}`. Authentication is
`Authorization: Bearer <token>`.