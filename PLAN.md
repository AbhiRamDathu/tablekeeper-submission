# Plan: Tablekeeper Stage 1 — containerized reservation service (rev. 2)

## Goal

Deliver a submission repository whose `stage-1/` folder builds into a container that passes the
shipped Stage 1 conformance suite in `tablekeeper/test/stage_1/`, verified by the harness itself
rather than by prose review.

Source of truth: `tablekeeper/spec/stage-1.md`. Line-level traceability: `REQUIREMENTS.md` at the
submission repository root.

## What changed from rev. 1, and why

Rev. 1 was written from the spec prose alone and had no runnable acceptance command anywhere. Four
defects were found by reading the harness, and all four would have invalidated work already done.

1. **The submission repository did not exist.** Rev. 1 named repo-relative paths (`app/tz.py`,
   `Dockerfile`) with no root. Every participant in this room gets its own isolated workspace, so
   "the repo" was nowhere. Nothing could be started, which is why Units 0, 1, 2 and independent
   verification all sit `in_progress` with no files on disk.
   **Now:** `C:\Users\linga\Jam\tablekeeper-submission`, git-initialised on `main` by the Planner,
   with `REQUIREMENTS.md` copied to its root so the Reviewer can read it without entering another
   runtime's workspace.
2. **The layout was wrong.** `harness/cli.py:329-336` resolves a submission as
   `<repo>/stage-1` and hands *that folder* to `docker build` as the context
   (`harness/docker_driver.py:59-71`). Rev. 1 put `Dockerfile` at the repository root, which a
   grader running `--repo <clone> --stage 1` rejects outright with
   "A submission repository holds one folder per stage, named stage-1 through stage-4".
   **Now:** everything lives under `stage-1/`, so both `--repo … --stage 1` and
   `--build …\stage-1` work.
3. **Acceptance was unverifiable.** **Now** every unit carries an exact command below.
4. **One owner held all nine units.** That is a single serial queue and it is the second reason
   nothing moved. **Now** the four seats each own a disjoint slice.

A fifth finding is a hard precondition, not a plan defect: see **Toolchain**.

## Shared paths (all agents, same machine)

| What | Path |
|---|---|
| Submission repository (write here) | `C:\Users\linga\Jam\tablekeeper-submission` |
| Stage-1 answer | `…\tablekeeper-submission\stage-1\` |
| Harness + spec + tests (read-only) | `C:\Users\linga\Downloads\dark-factory-wearedevs-main\dark-factory-wearedevs-main` |
| Requirements | `…\tablekeeper-submission\REQUIREMENTS.md` |

Branch per unit (`unit-0-tz`, `unit-1-loop`, …). Never write to another unit's files; the
Integrator lands. `--out` directories must be fresh: the harness calls `mkdir(exist_ok=False)` and
aborts on a collision.

## Acceptance commands

Let `H` = `C:\Users\linga\Downloads\dark-factory-wearedevs-main\dark-factory-wearedevs-main`.
Always run harness commands with `H` as the working directory.

**Tier 1 — runs today, no install.** These are the gate for every unit. Standard library only.

| Purpose | Command (cwd = `C:\Users\linga\Jam\tablekeeper-submission\stage-1`) |
|---|---|
| Unit tests, whole suite | `python -m unittest discover -s tests -t . -v` |
| One test module | `python -m unittest tests.test_tz -v` |
| Start the service | `python -m app.main` (honours `PORT`, default 8080) |
| Smoke it | `python -c "import urllib.request,json;print(urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=5).read())"` |

**Gate command correction, measured on Python 3.10.11 (rev. 4).** The suite form above only works
because `stage-1/tests/__init__.py` exists, which the Planner has now created. Verified:

| Form | Result |
|---|---|
| `discover -s tests -t . -v` **without** `__init__.py` | `ImportError: Start directory is not importable` |
| `discover -s tests -t . -v` **with** `__init__.py` | runs the suite, OK |
| `discover -s . -p "test_*.py" -v` without `__init__.py` | **exits 0 after running 0 tests — a silent false green** |
| `discover -s tests -t tests -v` without `__init__.py` | works |
| `python -m unittest tests.test_tz -v` without `__init__.py` | works |

Two consequences. Never use the `-s . -p` form: it reports success without running anything, which
is precisely the false pass this plan forbids. And treat "Ran 0 tests" as a failure in any report —
a Tier 1 result of zero tests is not a pass, it is an unrun gate.

**Tier 2 — grading gates, blocked on the owner.** Run with `H` as cwd once `httpx`/`pytest` exist.

| Purpose | Command |
|---|---|
| Whole Stage 1, dev loop (service already running on 8080) | `python -m harness run --track tablekeeper --base-url http://127.0.0.1:8080 --stages 1 --out runs/<fresh>` |
| One Stage 1 file | `python -m pytest tablekeeper\test\stage_1\test_reservations.py --base-url http://127.0.0.1:8080 -p harness.plugin --rootdir tablekeeper\test -q` |
| Counts for a run | read `runs\<fresh>\stage-1.counts.json` |

**Tier 3 — the grading architecture, blocked on Docker.**

| Purpose | Command |
|---|---|
| Whole Stage 1, container path — **this is grading** | `python -m harness run --track tablekeeper --repo C:\Users\linga\Jam\tablekeeper-submission --stage 1 --mode isolated --out runs/<fresh>` |

A unit is not done because Tier 1 passes. Tier 1 is the gate that lets work proceed; Tier 2 and
Tier 3 are what decide whether it is submitted. Say which tier you reached, and never report a
tier you did not run.

## Toolchain — measured, re-checked 2026-10-02

| Tool | State on this machine | Consequence |
|---|---|---|
| Docker | **absent** — `docker` is not on PATH | The grading command `--mode isolated` cannot run. |
| `httpx` | **absent** — `ModuleNotFoundError: No module named 'httpx'` | The shipped harness suite cannot run. |
| `pytest` | **absent** — `ModuleNotFoundError: No module named 'pytest'` | Rev. 2's per-unit pytest commands cannot run. |
| Python | 3.10.11, `zoneinfo` + tzdata working | Fine. Container pins 3.12. |
| git | 2.53.0 | Fine. |

Nothing has been installed since rev. 2. The single ask to the owner stands:
`python -m pip install -r <harness>\harness\requirements.txt`, plus a Docker install.

## Decision (rev. 3): the service depends on nothing

Waiting on that install would idle four seats. Instead the service is built on the standard library
only — `http.server.ThreadingHTTPServer`, `sqlite3`, `zoneinfo`, `hashlib`, `secrets`, `json`,
`base64` — with **no pip requirement in the image at all**, and unit tests written as `unittest`
cases driven by `urllib` against a locally started service.

This is measured, not assumed. The Planner ran a probe on this machine with zero third-party
packages installed. Observed output:

```
test_fifty_concurrent_requests_produce_no_5xx (__main__.Probe) ... ok
test_health_and_reset_against_a_live_server (__main__.Probe) ... ok
----------------------------------------------------------------------
Ran 2 tests in 1.084s

OK
```

That probe served `GET /health` as 200 `{"status":"ok"}`, `POST /_test/reset` as 204 writing a
fixture inside one `BEGIN IMMEDIATE` transaction, and survived 50 concurrent requests with zero
5xx on `ThreadingHTTPServer`.

Three reasons this is the right call rather than a workaround:

1. **It removes the blocker instead of waiting on it.** Every unit except the final image proof is
   now verifiable today, with no install and no permission.
2. **It fits the spec better.** "No outbound network at run time" becomes trivially true — there is
   nothing to fetch. A dependency-free `python:3.12-slim` image starts inside the 60 s health
   deadline with room to spare.
3. **It costs nothing later.** `unittest`-style tests are collected by `pytest` unchanged, so the
   moment `httpx` is installed the shipped harness suite runs against the same code and the same
   tests. No rework.

What it does **not** replace: `--mode isolated` is still the grading path and still needs Docker,
and the shipped suite still needs `httpx`. Unit 8 stays blocked on the owner. Nothing else is.

**Concurrency note that follows from this:** `ThreadingHTTPServer` is the default choice precisely
because the spec requires 50 concurrent in-flight requests with no 5xx, and a single-threaded
server would fail that immediately. Pair it with SQLite in WAL mode and `BEGIN IMMEDIATE` for
write transactions; that is what makes §7 and §11 atomicity achievable without extra dependencies.

**Language pin:** the container is `python:3.12-slim`; the host runs 3.10.11. Do not use 3.11+ only
syntax (e.g. `typing.Self`) or the host cannot run the tests that prove the image works.

## Unit 1: Build-to-test loop, schema, reset — *reordered to first*

- Owner: Integrator
- Files: `stage-1/Dockerfile`, `stage-1/app/main.py`, `stage-1/app/store.py`,
  `stage-1/app/schema.sql`, `stage-1/requirements.txt`
- Depends on: nothing
- Deliverable: a service that starts, answers `/health`, and accepts `/_test/reset`, plus the
  `Dockerfile` that will carry it. Stdlib only — `ThreadingHTTPServer` + `sqlite3`, no pip
  requirement in the image.
- Acceptance, split by tier, and say which tier you reached:
  - **Tier 1, runs today:** `python -m unittest tests.test_loop -v` passes, covering
    `GET /health` → 200 `{"status":"ok"}`, `POST /_test/reset` → 204 unauthenticated writing the
    whole fixture in one `BEGIN IMMEDIATE` transaction, repeated resets supported, and 50 concurrent
    requests producing zero 5xx. `python -m app.main` starts and honours `PORT`.
  - **Tier 2, blocked:** the harness suite runs against it and `runs\<fresh>\stage-1.counts.json`
    exists.
  - **Tier 3, blocked on Docker:** `docker build -t tablekeeper-s1 C:\Users\linga\Jam\tablekeeper-submission\stage-1`
    exits 0, and `--mode isolated` reaches the suite instead of failing at "docker is not installed".
  - Dockerfile pins `python:3.12-slim`, honours `-e PORT` (default 8080), binds `0.0.0.0`, copies
    `app/`, installs nothing from the network at run time.

Why first: if the build-to-test loop does not exist, no later unit can be proven and the defect
would surface only at the end of the stage, after all nine units were written. Everything else in
this plan is worthless without this loop.

## Unit 0: DST and absolute-interval core

- Owner: Implementer
- Files: `stage-1/app/tz.py`, `stage-1/app/intervals.py`, `stage-1/tests/test_tz.py`
- Depends on: nothing (parallel to Unit 1)
- Deliverable: pure functions for local-time resolution and half-open occupancy intervals. No HTTP,
  no DB, no import of anything outside the standard library.
- Acceptance (Tier 1, runs today): `python -m unittest tests.test_tz -v` passes for Europe/Berlin
  2026-03-29 and 2026-10-25, America/New_York 2026-03-08 and 2026-11-01, plus the §1 adjacency
  case where a 90-minute booking at 19:00 does not conflict with one starting 20:30.
  Write the tests as `unittest.TestCase`; pytest collects them unchanged later.

Why second-in-line but genuinely parallel: the highest *domain* risk, and it is pure. If local-time
resolution is wrong, availability, booking, amendment and batch moves are wrong together.

## Conformance suite and one verify command

- Owner: Test author
- Files: `stage-1/tests/verify.ps1`, `stage-1/tests/README.md`
- Depends on: nothing
- Deliverable: a script that starts nothing, assumes a service on 8080, runs the whole Stage 1
  suite, and prints pass/fail counts plus the failing node IDs. One command, so every later unit
  reports the same evidence instead of an assertion about its own code.
- Acceptance: `powershell -File stage-1\tests\verify.ps1` run from the harness root exits 0 and its
  output contains the Stage 1 counts; with the service stopped it exits non-zero and says the
  service was unreachable.

Why this seat: Rev. 1 left the Test author idle while the Implementer queued nine units. The suite
is what converts "looks right" into "verified", and it is reusable by every subsequent unit.

## Unit 2: Authentication

- Owner: Implementer
- Files: `stage-1/app/auth.py`
- Depends on: nothing (parallel to Units 0 and 1)
- Deliverable: signup, login, bearer token issuance and lookup
- Acceptance: `python -m pytest tablekeeper\test\stage_1\test_health_reset_auth.py --base-url http://127.0.0.1:8080 -p harness.plugin --rootdir tablekeeper\test -q`
  passes; signup 201, login 200, 409 `email_taken`, 422 for a password under 8 characters and for
  an email not of the form `local@domain`, 401 `unauthenticated` on wrong password or unknown
  email, no plaintext password at rest.

## Unit 3: Public read endpoints

- Owner: Implementer
- Files: `stage-1/app/api_public.py`
- Depends on: Units 0, 1
- Deliverable: `GET /restaurants`, `GET /restaurants/{id}`, `GET /availability`
- Acceptance: `python -m pytest tablekeeper\test\stage_1\test_restaurants_availability.py --base-url http://127.0.0.1:8080 -p harness.plugin --rootdir tablekeeper\test -q`
  passes; all three need no bearer token; a missing required query parameter is 422
  `validation_failed`; a closed weekday returns `"slots": []`; a slot with no free table still
  appears with an empty `available_table_ids`; table order follows the fixture.

## Unit 4: Idempotency engine and reservation write path

- Owner: Implementer (single owner — see coupling note)
- Files: `stage-1/app/idempotency.py`, `stage-1/app/api_reservations.py`, `stage-1/app/service.py`
- Depends on: Units 0, 1, 2, 3
- Deliverable: `POST /reservations`, `GET /reservations`, `GET /reservations/{reference}`
- Acceptance: the full §7 table — absent or empty key 400 `missing_idempotency_key`; first use
  201; replay 200 with an identical JSON value; different body 409 `idempotency_key_reuse`; a key
  whose original request failed 4xx is reusable; key length outside 1–255 is 422; a burst of
  concurrent identical requests on one unused key yields exactly one 201 and the rest 200 with the
  same body, applying the operation once.

Coupling note: the idempotency table, the reservation write path and the service layer are one
owner. Split across two seats they produce two individually correct changes that are jointly broken,
because a key is only reusable if the failure that stored it was rolled back with the reservation.

## Unit 5: Cancel and amend

- Owner: Implementer
- Files: `stage-1/app/api_reservations.py`, `stage-1/app/service.py`
- Depends on: Unit 4
- Deliverable: `POST /reservations/{reference}/cancel`, `PATCH /reservations/{reference}`
- Acceptance: covered by `stage-1\test_reservations.py`; cancelling twice returns 200 with current
  state, not an error; within `cancellation_cutoff_minutes` of start returns 409 `cutoff_passed`; a
  cancelled reservation returns 409 `reservation_cancelled`; a failed amendment leaves the original
  booking and its occupancy unchanged; `reference` and `reservation_id` survive an amendment; the
  table is offered again in `GET /availability` immediately after a cancel.

## Unit 6: Export and import

- Owner: Implementer
- Files: `stage-1/app/api_testcontrol.py`, `stage-1/app/portability.py`
- Depends on: Unit 4
- Deliverable: `GET /_test/export`, `POST /_test/import`
- Acceptance: export 200 with `track: "tablekeeper"`, `format_version: 1` and opaque `state`; import
  204 and atomic; a round trip against a *changed* destination preserves accounts, hashed-password
  login, existing bearer tokens, references, completed idempotent request bodies and their original
  responses, and leaves previously failed keys reusable; identities, statuses and timestamps are not
  regenerated; missing fields, wrong track or version, or invalid state give 422 with the
  destination unchanged.

## Unit 7: Atomic reservation moves

- Owner: Implementer
- Files: `stage-1/app/api_moves.py`
- Depends on: Units 4, 5
- Deliverable: `POST /reservation-moves`
- Acceptance: 1–8 items with distinct references; 404 for unknown or another owner's reference; 422
  for different restaurants or duplicate references; 409 `reservation_cancelled`; non-occupancy
  errors use ordinary amendment codes and take precedence in input order with cutoff errors ahead of
  other changes for that booking; overlap yields 409 `table_unavailable`; either every move commits
  or nothing changes including retry keys; success 201 with reservations in input order including
  unchanged items; replay 200 with that original response even after later amendment or
  cancellation.

## Unit 8: Container hardening and evidence

- Owner: Integrator
- Files: `stage-1/Dockerfile`, `stage-1/RUN.md`
- Depends on: Units 3–7
- Deliverable: an image that survives the grading architecture, not just a local build
- Acceptance: the container-path harness command above reports PASS for Stage 1; 50 concurrent
  in-flight requests produce no 5xx and each completes within 5 s (`/_test/reset` within 10 s); the
  service makes no outbound request at run time, proven by running the grading command with
  `--mode isolated` on an `--internal` network where a fetch would simply fail.

## Independent verification

- Owner: Reviewer
- Files: writes `stage-1/docs/verification.md`; reads everything else, writes no implementation
- Depends on: Unit 0 first, then each subsequent unit
- Deliverable: one finding per spec section, each with the command run and its observed output
- Acceptance: `stage-1/docs/verification.md` confirms or refutes §7 ordering, §11 atomicity, §9
  DST resolution, §5 status/code mapping and §10 portability, citing
  `<fresh>\stage-1.counts.json` from a run the Reviewer started. Read
  `C:\Users\linga\Jam\tablekeeper-submission\REQUIREMENTS.md` — it is in the shared repository
  precisely so this task does not require entering another runtime's workspace.

Why Rev. 1's version of this task stalled: it was scoped "read-only against the implementation and
`REQUIREMENTS.md`" when no implementation existed and `REQUIREMENTS.md` lived only in the Planner's
private workspace. There was literally nothing the Reviewer could open.

## Risks

- **No toolchain.** Docker and `httpx`/`pytest` are absent, so no harness command runs. Earliest
  observation: Unit 1's acceptance failing at "docker is not installed" or `ModuleNotFoundError`.
  Owner action required; see the ask.
- **Layout drift back to repo root.** Earliest observation: `--repo … --stage 1` rejecting with
  "holds no stage folder". Any file added outside `stage-1/` is not part of the submission.
- **DST resolution implemented twice or inconsistently.** Earliest observation: Unit 0's four
  transition tests disagreeing on the ambiguous hour.
- **Idempotency bound at the wrong layer.** §7 resolves the key after the body parses as an object
  and after authentication, but before endpoint field validation. Late binding passes
  single-threaded tests and fails the 409-on-invalid-body case.
- **Export/import regenerating something it must preserve.** Earliest observation: a round-trip that
  passes against a fresh reset and fails against a changed destination. §10 says explicitly that a
  fresh fixture does not satisfy the requirement.
- **Races invisible locally.** 50 concurrent in flight with no 5xx is a stated limit;
  single-threaded testing will not surface it. Only the container path exercises it.
- **Host mode flatters the build.** `harness/docker_driver.py:6-15` warns that host mode does not
  block outbound traffic, so a service that fetches a CDN passes locally and fails grading. Never
  score from host mode.
- **Scope.** Four days against a Stage 1 larger than Stages 2–4 combined.

## Open questions

- **Are stage gates cumulative, and what does the rubric weight?** `kickoff-manifest.json` and
  `docs/participant-guide.md` are unverified — a read of the manifest was declined, so the plan does
  not assume its contents. Not blocking: Stage 1 is required under any answer. Blocks planning past
  Stage 1. **This is the second thing I need from the owner, and it is not urgent.**
- **Spec ambiguities.** The README routes these to the BAND Discord, where answers are public.
  Anything the Reviewer cannot resolve from the spec text should be raised there rather than decided
  locally.
- **Docker.** No `Dockerfile` can be proven buildable on this machine until Docker is installed. If
  it cannot be installed, say so and I will re-plan Unit 8 and Unit 1's acceptance around host mode
  only, accepting the grading risk explicitly rather than silently.