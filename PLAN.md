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

| Purpose | Command |
|---|---|
| Whole Stage 1, dev loop (service already running on 8080) | `python -m harness run --track tablekeeper --base-url http://127.0.0.1:8080 --stages 1 --out runs/<fresh>` |
| Whole Stage 1, container path — **this is grading** | `python -m harness run --track tablekeeper --repo C:\Users\linga\Jam\tablekeeper-submission --stage 1 --mode isolated --out runs/<fresh>` |
| One Stage 1 file | `python -m pytest tablekeeper\test\stage_1\test_reservations.py --base-url http://127.0.0.1:8080 -p harness.plugin --rootdir tablekeeper\test -q` |
| Counts for a run | read `<fresh>\stage-1.counts.json` |

## Toolchain — precondition, measured not assumed

| Tool | State on this machine | Consequence |
|---|---|---|
| Docker | **absent** (`docker` not on PATH) | The grading command cannot run. Only the `--base-url` dev loop is available. |
| `httpx`, `pytest` | **absent** (`ModuleNotFoundError: httpx`) | No harness command can run at all. |
| Python | 3.10.11, `zoneinfo` + tzdata working | Fine. Container pins 3.12; host version does not gate the build. |
| git | 2.53.0 | Fine. |

**Unit 0 is the only unit provable without this.** It is pure Python. Everything else is blocked on
one `pip install -r harness/requirements.txt`. This is the single ask of the owner.

## Unit 1: Build-to-test loop, schema, reset — *reordered to first*

- Owner: Integrator
- Files: `stage-1/Dockerfile`, `stage-1/app/main.py`, `stage-1/app/store.py`,
  `stage-1/app/schema.sql`, `stage-1/requirements.txt`
- Depends on: nothing
- Deliverable: a container that builds, starts, answers `/health`, and accepts `/_test/reset`.
  This is the smallest slice that touches Docker, HTTP, SQLite and the harness at once.
- Acceptance:
  - `docker build -t tablekeeper-s1 C:\Users\linga\Jam\tablekeeper-submission\stage-1` exits 0.
  - `python -m harness run --track tablekeeper --repo C:\Users\linga\Jam\tablekeeper-submission --stage 1 --mode isolated --out runs/<fresh>` reaches the suite instead of failing at
    "docker is not installed", and `stage-1.counts.json` exists.
  - `GET /health` returns 200 `{"status":"ok"}` inside 60 s; `POST /_test/reset` returns 204 and
    is unauthenticated.
  - Dockerfile pins `python:3.12-slim`, honours `-e PORT` (default 8080), binds `0.0.0.0`, and
    installs everything at build time — no outbound request at run time.

Why first: if the build-to-test loop does not exist, no later unit can be proven and the defect
would surface only at the end of the stage, after all nine units were written. Everything else in
this plan is worthless without this loop. Docker being absent makes it the riskiest unknown in the
build, not the DST arithmetic.

## Unit 0: DST and absolute-interval core

- Owner: Implementer
- Files: `stage-1/app/tz.py`, `stage-1/app/intervals.py`, `stage-1/tests/test_tz.py`
- Depends on: nothing (parallel to Unit 1)
- Deliverable: pure functions for local-time resolution and half-open occupancy intervals. No HTTP,
  no DB, no import of anything outside the standard library.
- Acceptance: `cd C:\Users\linga\Jam\tablekeeper-submission\stage-1; python -m pytest tests/test_tz.py -q`
  passes for Europe/Berlin 2026-03-29 and 2026-10-25, America/New_York 2026-03-08 and 2026-11-01,
  plus the §1 adjacency case where a 90-minute booking at 19:00 does not conflict with one starting
  20:30.

Why second-in-line but genuinely parallel: the highest *domain* risk, and the only unit whose
proof needs no toolchain beyond stdlib. If it is wrong, availability, booking, amendment and batch
moves are wrong together.

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