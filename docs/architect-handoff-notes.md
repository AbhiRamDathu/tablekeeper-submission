# Architect session notes — Tablekeeper Stages 1–4 run

Written by the Architect seat (@velishalalingaraju/architect) on 2026-10-08 (local 16:0x)
before attempting a peer-worker restart. If this file is being read by a fresh Architect
turn, everything below is already verified by command output and does not need re-deriving.

## 1. Room / runtime state

- Room: `f8e78f35-4137-491b-979b-bae86f1b6a17` (Tablekeeper — Hackathon).
- Peers at session start: owner `velishalalingaraju`, plus `velishalalingaraju/implementer`,
  `velishalalingaraju/reviewer`, `velishalalingaraju/quality-assurance`, `velishalalingaraju/architect`.
- `jam --profile default list` (measured): implementer / reviewer / quality-assurance are
  `Connected running=true`; **architect is `Stopped running=false`**.
- Consequence: every agent-scoped outbound command from this seat
  (`jam ... send`, `jam ... chat participants`, `jam ... inbox`) fails with
  `peer peer architect has no running worker`. This is an environment defect on this seat,
  not a room-membership problem.
- Workarounds that DO work from this seat: `jam work assign|edit|board|status`,
  `jam plan show|set|diagram`, `jam activity list` (cross-peer live activity),
  `jam logs`, `jam status`, `jam doctor`.
- `jam activity report --provider opencode --kind turn-started ...` restored
  `presence=live` on the bound session but did **not** start the worker.

## 2. Environment facts (measured)

- Python 3.12.10 system; `pytest 9.1.1` available on the system interpreter.
- The repo venv `tablekeeper-submission\.venv` has **no pytest**; it is only for the
  service (uvicorn/httpx). Use system `python` for tests.
- **Docker is NOT installed** (`Get-Command docker` → nothing). Isolated/`--build`
  harness modes and container builds are therefore UNVERIFIED on this host.
  Never report `stage-1\tests\test_packaging.py` as a container build result.
- Harness bundle (specs + official suites): `C:\Users\linga\Downloads\dark-factory-wearedevs-main\dark-factory-wearedevs-main\`
  - specs: `tablekeeper\spec\stage-{1..4}.md`
  - suites: `tablekeeper\test\stage_{1..4}\`
  - CLI: `python -m harness run --track tablekeeper --base-url http://127.0.0.1:PORT --stages N`
    (host mode needs no Docker; `--build`/`--repo`/`--mode isolated` DO need Docker).
- Local time zone is UTC+05:30; `jam` log timestamps are UTC.

## 3. Baseline evidence already captured by this seat

- Full Stage-1 shipped suite, from `stage-1/`:
  `python -m unittest discover -s tests -t .` →
  `Ran 272 tests in 275.904s` / `OK` (0 failures, 0 errors, 0 skipped) and
  `SPEC GATE PASSED`, `named defects 28 / red 0 / declined 0`.
- Git at that moment: branch `unit-7-moves`, HEAD `225aa85`
  ("Store passwords with scrypt, the KDF spec 6 names (AUTH defect)"),
  8 commits ahead of `origin/unit-7-moves`; only `PLAN.md` dirty.
  Remote: `https://github.com/AbhiRamDathu/tablekeeper-submission`.
  Branches: local `main`, `stage-2-repair`, `unit-4-auth`, `unit-7-moves`;
  remote `origin/main` (default/HEAD), `origin/unit-7-moves`.
  Protected stash `stash@{0}` exists — do not drop or apply wholesale.
- The Implementer is actively committing; HEAD moves. Always re-read `git log` before quoting a SHA.

## 4. Official specs — what still has to be built

Stage-1 spec is at the bundle path above and is the contract; shipped tests are only a
partial sample. The repo currently contains **only `stage-1/`**. Still required:

- `stage-2/` — browser UI (`/`, `/signup`, `/login`, `/lookup`) with the exact
  `data-testid` set, out-of-order search handling, lost-response recovery with the same
  idempotency key, `table_ids` / `combinable` pair bookings, `available_options`,
  `combination_not_allowed`, combined-table cells `slot-{t_a}+{t_b}-{HH:MM}`,
  `confirmation-tables`, `reservation-tables`, plus every Stage-1 guarantee.
- `stage-3/` — `explain=true` availability explanations, reservation history with
  `seq`, manager policies with `policy_version` / effective dating, `accepted_terms`
  and `revision` on every reservation, `expected_revision` → 409 `stale_revision`,
  `/decision`, recurring `POST /series` / `GET /series/{id}` with exceptions,
  combined-table history rules, collective moves under policies.
- `stage-4/` — `POST /restaurants/{id}/replans` preview (deterministic minimal-cost
  plan, `planning_limit`, `no_feasible_plan`, `restaurant_revision`),
  `/replans/{plan_id}/apply` (atomic, `stale_plan`, `plan_already_applied`,
  `reassigned` history, closure enforcement), `POST /series/{series_id}/amend`
  (`stale_revision`, cutoff→policy adoption, no-op semantics, atomicity).
- Repo root: `README.md`, `FACTORY.md`, `mandates/` (one per seat, each with
  `Harness` and `Model` fields, generic — no Tablekeeper-specific content),
  `Dockerfile` + `RUN.md` in every completed stage, `room.json` only if obtained
  legitimately from Band (never fabricate).

## 5. Harness/`check` obligations for the judge-facing repo

`harness/check.py` (no Docker needed) requires:
- `README.md` and `FACTORY.md` at the root,
- `stage-1` … `stage-4` folders,
- `mandates/` with a mandate per seat, each containing the literal fields `Harness` and `Model`,
- `room.json` at the root (download instructions: Band console → room `…` menu →
  Download full session → save unchanged as `room.json`),
- no credentials matching its `SECRETS` patterns in any tracked text file.

## 6. Standing process rules for this run

- Specification is the contract; shipped tests are evidence, never authority.
- No seat accepts its own work. Implementer produces → Reviewer reviews → QA verifies →
  Architect accepts, on evidence only (command output, diff, commit SHA, test counts).
- Never weaken a test to get green; fix the implementation.
- Never claim a pass without the command output that produced it.
- Docker results must be reported as UNVERIFIED, never as passing.
