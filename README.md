# Tablekeeper submission — the **tablekeeper** track

A Band Desktop software factory (four coding-agent seats) and the service that
factory built across the track's four stages. The build is complete through
Stage 4; each stage folder is a self-contained, buildable service that solves its
own stage and carries every earlier stage forward.

| | |
|---|---|
| **Track** | `tablekeeper` |
| **Team** | Tablekeeper Band — room owner `@velishalalingaraju` |
| **Seats** | `@architect` (lead/coordinator), `@implementer`, `@reviewer`, `@quality-assurance` |
| **Harness** | OpenCode, model `opencode/big-pickle` on every seat |
| **Implementation** | Python 3.12, standard library only (no third-party runtime dependency) |
| **Public remote** | `https://github.com/AbhiRamDathu/tablekeeper-submission` |

## How to read this repository

```text
README.md          this file: team, track, how to read and reproduce the result
FACTORY.md         the factory: seats, design choices, costs, failure handling
mandates/          one mandate per seat, named after the seat as the room shows it
room.json          the room, downloaded from Band as a full session (human-supplied)
stage-1/           complete, buildable service — the stage-1 specification
stage-2/           stage-1 carried forward and extended to the stage-2 specification
stage-3/           stage-2 carried forward and extended to the stage-3 specification
stage-4/           stage-3 carried forward and extended to the stage-4 specification
docs/              architect/session notes and the derived work-item registers
```

Read `FACTORY.md` first if you want to know how the work was organised;
`mandates/` if you want to know what each seat is allowed to do; the stage
folders if you want the service.

Each stage folder is **self-contained**: it has its own `Dockerfile`, its own
`RUN.md`, its own `app/` package, its own `tests/`, and it solves its own stage
and no later one. `stage-N/` is graded against suites 1..N, so a stage that broke
an earlier stage does not count.

## What each stage adds

| Stage | Folder | Adds on top of the previous stage |
|---|---|---|
| 1 | `stage-1/` | Reservations API: restaurants/availability, signup/login, bookings, cancel, PATCH, atomic multi-booking moves, idempotency, DST-aware local times, export/import. |
| 2 | `stage-2/` | Browser client (`/`, `/signup`, `/login`, `/lookup`), combined-table (`combinable` pair) bookings, `available_options`, on-grid combination cells, competing-client / lost-response recovery. |
| 3 | `stage-3/` | `explain=true` availability explanations, reservation `history` and `decision`, manager policies (`policy_version`, effective dates), `revision` + `accepted_terms` on every reservation, `expected_revision`/`stale_revision`, recurring `POST /series`, `/series/{id}`, collective moves under policies. |
| 4 | `stage-4/` | Manager `POST /restaurants/{id}/replans` preview (deterministic minimal-cost re-seating after a closure) and `.../apply` (atomic, `stale_plan`, `plan_already_applied`, `reassigned` history), and `POST /series/{series_id}/amend`. |

## Running a stage

Every stage folder runs the same way.

```sh
cd stage-4
python -m app.main            # serves http://127.0.0.1:8080, /health answers {"status":"ok"}
```

The state file defaults to `tablekeeper.sqlite` next to `app/` and can be pointed
elsewhere with the `TABLEKEEPER_DB` environment variable; the port comes from
`PORT` (default `8080`). Full instructions, including the container build, are in
each stage's `RUN.md`.

## Checking a stage the way the judges do

From the challenge package directory (`dark-factory-wearedevs-main/`):

```sh
# offline gates 1, 2 and the mandate half of gate 4 — no Docker needed
python -m harness check <this-repository> --track tablekeeper

# gate 3 — builds the container and runs the shipped suites (needs Docker)
python -m harness run --track tablekeeper --repo <this-repository> --stage 4 --mode isolated
python -m harness run --track tablekeeper --repo <this-repository> --all
```

Without Docker, the same shipped suites can be run against a locally started
service, which is what this factory used while developing:

```sh
# start the stage folder you want to grade
cd stage-4 && PORT=8099 TABLEKEEPER_DB=$TEMP/tk.sqlite python -m app.main &

# run that stage's suite and every earlier one against it
python -m harness run --track tablekeeper \
  --base-url http://127.0.0.1:8099 --stages 1 2 3 4 --out <fresh-output-directory>
```

A `stage-N/` folder is graded against every suite up to N, so `stage-4/` must
pass suites 1, 2, 3 **and** 4 to count as a completed stage 4.

## Verification record

Measured on this host (Windows, Python 3.12.10, **no Docker**). Each number is
reproducible from the command named beside it.

| Check | Result | How to reproduce |
|---|---|---|
| Offline gates (`harness check`) | 1 problem: `room.json` missing (human-supplied; see below) | `python -m harness check <repo> --track tablekeeper` |
| Shipped suite, stage 1 | **120 collected / 120 passed / 0 failed / 0 skipped** | `--base-url <stage-1> --stages 1` |
| Shipped suite, stage 2 | **25 collected / 25 passed / 0 failed** (8 API + 17 UI) | `--base-url <stage-2> --stages 2` |
| Shipped suite, stage 3 | **7 collected / 7 passed / 0 failed** | `--base-url <stage-3> --stages 3` |
| Shipped suite, stage 4 | **6 collected / 6 passed / 0 failed** | `--base-url <stage-4> --stages 4` |
| Full chain against `stage-4/` | **stage 1 + 2 + 3 + 4 all green** (120 + 25 + 7 + 6) | `--base-url <stage-4> --stages 1 2 3 4` |
| Stage-1 in-folder suite | **272 tests green, 0 failures, 0 errors, 0 skipped** | `cd stage-1 && python -m unittest discover -s tests -t .` |
| Container build (all stages) | **UNVERIFIED — Docker is not installed on this host** | `docker build stage-4` |

The shipped suites are a **partial** sample of the grading set; the stage folders
also carry the team's larger in-folder spec suites (see each `RUN.md`). A run
reporting `Ran 0 tests` is not a pass.

## Reproducing the final result

```sh
git clone https://github.com/AbhiRamDathu/tablekeeper-submission
cd tablekeeper-submission
git log --oneline                     # the agent-generated history, oldest → newest

# every stage starts and serves /health
for s in 1 2 3 4; do
  (cd stage-$s && PORT=8080 python -m app.main &) ; sleep 2
  curl -s localhost:8080/health ; kill %1 2>/dev/null || true
done
```

Then run the shipped suites as shown above. The default branch (`main`) holds
this complete Stage-4 submission.

## Architecture

Every stage is one process: a pure-standard-library HTTP service (`app/main.py`
routes, `app/store.py` is the SQLite state layer, `app/tz.py` resolves local
times against IANA zones including DST gaps/repeats, `app/intervals.py` is
half-open `[start, end)` occupancy arithmetic, `app/auth.py` is the password
hashing and tokens). State is a single SQLite file; writes take `BEGIN IMMEDIATE`
so concurrent requests serialise, and idempotency receipts are stored with each
successful write so a retry replays the original response. `app/store.py` also
owns export/import, which is how a later stage accepts an earlier stage's state.

See `FACTORY.md` for the production process; `docs/work-items-stage-{2,3,4}.md`
for the specification citations behind each stage; `docs/architect-handoff-notes.md`
for the session handoff record.

## room.json

`room.json` is the Band room downloaded as a **full session** — it is the only
artifact no seat can produce, because Band offers it through the human console,
not the agent API. To add it:

> Open the room in Band Desktop → room `⋮` menu → **Open in Band** → `⋮` →
> **Download** → **Download full session** → save unchanged as `room.json` at the
> repository root.

`python -m harness check` reports exactly `room.json is missing` until it is
added; every other offline gate passes without it.

## Credentials

`.env` and `agent_config.yaml` are local runtime configuration and are
gitignored; they are not part of the submission. `python -m harness check` scans
the repository for credential shapes and fails on any it finds. If you find a
credential-shaped string anywhere in this repository, treat it as compromised and
rotate it. `room.json` from Band contains the room's full event log — review it
before publishing.
