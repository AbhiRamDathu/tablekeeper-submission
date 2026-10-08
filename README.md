# Tablekeeper submission — the **tablekeeper** track

A Band Desktop software factory (four coding-agent seats) and the service that
factory built across the track's four stages.

| | |
|---|---|
| **Track** | `tablekeeper` |
| **Team** | Tablekeeper Band — room owner `@velishalalingaraju` |
| **Seats** | `@architect` (lead/coordinator), `@implementer`, `@reviewer`, `@quality-assurance` |
| **Harness** | OpenCode, model `opencode/big-pickle` on every seat |
| **Implementation** | Python 3.12, standard library only (no third-party runtime dependency) |

## How to read this repository

```text
README.md          this file: team, track, and how to read the repository
FACTORY.md         the factory: seats, design choices, costs, failure handling
mandates/          one mandate per seat, named after the seat as the room shows it
room.json          the room, downloaded from Band as a full session
stage-1/           complete, buildable service — the stage-1 specification
stage-2/           stage-1 carried forward and extended to the stage-2 specification
stage-3/           stage-2 carried forward and extended to the stage-3 specification
stage-4/           stage-3 carried forward and extended to the stage-4 specification
docs/              supporting verification notes
```

Read `FACTORY.md` first if you want to know how the work was organised;
`mandates/` if you want to know what each seat is allowed to do; the stage
folders if you want the service. Each stage folder is self-contained: it has its
own `Dockerfile`, its own `RUN.md`, its own tests, and it solves **its own**
stage and no later one.

## Running a stage

Every stage folder runs the same way.

```sh
cd stage-1
python -m app.main            # serves http://127.0.0.1:8080, /health answers {"status":"ok"}
```

The state file defaults to `stage-1/tablekeeper.sqlite` and can be pointed
elsewhere with the `TABLEKEEPER_DB` environment variable; the port comes from
`PORT`. Full instructions, including the container build, are in each stage's
`RUN.md`.

## Checking a stage the way the judges do

From the challenge package directory (`dark-factory-wearedevs-main/`):

```sh
# offline gates 1, 2 and the mandate half of gate 4 — no Docker needed
python -m harness check <this-repository> --track tablekeeper

# gate 3 — builds the container and runs the shipped suites (needs Docker)
python -m harness run --track tablekeeper --repo <this-repository> --stage 1 --mode isolated
```

Without Docker, the same shipped suites can be run against a locally started
service, which is what this factory used while developing:

```sh
python -m harness run --track tablekeeper \
  --base-url http://127.0.0.1:8099 --stage 1 --out <fresh-output-directory>
```

Stage folders are graded against every suite up to their own number, so
`stage-3/` must pass suites 1, 2 **and** 3 to count as a completed stage 3.

## Stage status

| Stage | Folder | Spec implemented | Verified |
|---|---|---|---|
| 1 | `stage-1/` | yes | local suite 272 tests green and official harness 120 passed / 0 failed, measured from a clean clone at `a5b8cdc`; container build **unverified — Docker unavailable on this machine** |
| 2 | `stage-2/` | **no — landing copy only** | copy is faithful at `2eef2cd`: 272 tests green from inside `stage-2/`. Spec implementation is outstanding work items WI-202..WI-216 |
| 3 | `stage-3/` | no | folder not created yet |
| 4 | `stage-4/` | no | folder not created yet |

`python -m harness check --track tablekeeper` currently reports exactly one
problem: `room.json is missing` (downloadable only from the room's Band menu,
so it cannot be produced by any seat). `harness run` for stages 3 and 4 has
not been run because those folders do not exist.

## Credentials

`.env` and `agent_config.yaml` are local runtime configuration and are
gitignored. They are not part of the submission and never have been. If you
find a credential-shaped string anywhere in this repository, treat it as
compromised and rotate it.
