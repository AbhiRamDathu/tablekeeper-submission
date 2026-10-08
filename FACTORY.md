# FACTORY

What the factory is, how another team could stand it up, what it cost, what it
tried that failed, and how it catches bad work. `mandates/` holds the per-seat
contracts; this file holds the design behind them.

---

## 1. The factory in one paragraph

Four seats in one Band Desktop room, each with its own mandate file, working on
one submission repository. The **architect** reads the specification and turns a
stage into scoped work items on a shared board. The **implementer** takes one
item at a time, changes the smallest thing that makes the specified behaviour
true, and hands back a revision with its output. The **reviewer** re-derives each
claim against the specification at a pinned revision and accepts or rejects it
with a citation. The **quality-assurance** seat attacks the result from
directions the shipped checks did not think to ask about. Nothing is accepted on
the implementer's word: acceptance requires a reproduction at a pinned hash, run
by a seat that did not write the code.

---

## 2. Seats and ownership

| Seat | Harness | Model | Owns | Never does |
|---|---|---|---|---|
| `architect` | OpenCode | `opencode/big-pickle` | specification reading, stage decomposition, work items on the board, handoffs, acceptance, factory documents (`README.md`, `FACTORY.md`, `mandates/`), the gate record | product code, reviewing its own assignments |
| `implementer` | OpenCode | `opencode/big-pickle` | one open work item at a time, the code and its tests, commits at the item boundary | deciding whether its own work landed, editing an expectation to make a check pass |
| `reviewer` | OpenCode | `opencode/big-pickle` | specification-vs-code review at a pinned revision, findings with citations and reproductions | editing the repository |
| `quality-assurance` | OpenCode | `opencode/big-pickle` | adversarial verification, boundaries and ordering, clean-state runs, severity | repairing what it reported |

Two seats are separated on purpose at every step: the one who wrote the change
and the one who judges it. The architect may assign but not accept its own
review; the reviewer may reject but must reproduce first.

### Standing it up

1. Create the four seats in Band Desktop, each with its own seat identity, and
   confirm that `@handle` messages reach each seat and each seat can reply.
2. Point each seat at OpenCode, and copy `mandates/` into the room so each seat
   holds its own mandate. A mandate names how that seat works; it deliberately
   contains no detail about this particular problem — see §4.
3. Give every seat the same checkout of this repository. One repository, shared
   working tree; work items on the board are what keeps two seats off the same
   lines.
4. Configure a distinct Git identity per seat. This repository's own history
   mixes a shared `Tablekeeper Band <band@local>` identity (the early units,
   before per-seat identity was configured) with per-seat authorship from the
   factory units onward; a commit author is how a judge reads the room back out
   of the history.
5. Keep credentials out of the repository. `.env` and `agent_config.yaml` are
   local-only and gitignored; `python -m harness check` fails the repository if
   a credential shape ever reaches a tracked file.

---

## 3. Design choices, and what they cost

**Specification first, code second.** Every work item cites a specification
passage before it names a file. Cost: slower first passes, because a seat reads
the spec before typing. Benefit: disagreements between the shipped checks and
the specification surface as findings against the contract instead of as silent
patches to the test.

**Work items are identified, not described.** Each item gets a stable
identifier, appears on the shared board with it, and every handoff, finding and
return addresses the item by that identifier. Cost: bookkeeping, and the
discipline of never opening a second item for a symptom of the first. Benefit: a
finding can travel from reviewer to architect to implementer and back without
anyone re-deriving it, and the commit message can carry the identifier so history
reads backwards from a defect to the work that caused it.

**One item in flight per seat.** Cost: less apparent parallelism. Benefit: two
seats are never editing the same lines of a shared working tree, which is the
failure mode that a single shared checkout actually has.

**Standard library only.** Cost: the service carries its own interval arithmetic
and time-zone handling rather than delegating to a library. Benefit: the stage
folders build with no network access and no lock file, and `harness run --repo`
cannot fail because a package index was unreachable.

**Stage folders are copied forward, then widened.** `stage-2/` is `stage-1/`
with the stage-2 specification applied, and so on. Each copy is a complete
service with its own `Dockerfile` and `RUN.md`, and each solves its own stage
and no later one, because a stage folder that passes the next stage's suite
claims work it did not do. Cost: duplicated code between folders, and the need
to delete a copied `.git` directory or a clone arrives with an empty folder.
Benefit: a judge can check out any single stage folder and run it.

**Verification without Docker, when Docker is absent.** `harness run --repo`
builds a container. On a machine with no Docker daemon, the same shipped suites
are run against a locally started service with `harness run --base-url`, which
gives the identical assertions and a different transport. Cost: the container
path itself is then unproven, and the report has to say so rather than imply it
was run. Benefit: the suites are still objective and still re-runnable.

---

## 4. Why the mandates say nothing about this problem

`harness check` scans `mandates/` against the vocabulary of the entered track
and fails on any of it. That is not a formality: a mandate that names this
problem's identifiers is a factory that only works here. The test another team
should apply is *"could I hand these mandates to a team building something
completely different and would they still make sense?"* — so each mandate
describes how a seat takes work, how it verifies, what a handoff must contain,
and what the seat must not do, and nothing else. Track-specific detail lives in
the task the architect dispatches in the room, where it can change without
touching the factory.

---

## 5. What it costs

Measured on this repository (all figures from `git` and the shipped suites, so
they are re-derivable):

| Measurement | Value |
|---|---|
| Repository lifetime | 2026-10-02 → 2026-10-08 (six days) |
| Commits | see `git rev-list --count HEAD` |
| Stage-1 suite | 272 tests plus 146 subtests, ≈275 s wall clock on this machine |
| Official harness, one stage, `--base-url` | ≈344 s wall clock including the stage's overshoot suite |
| Runtime dependencies | none (Python standard library) |
| Model spend | not derivable from this repository; it is recorded per seat by the harness that runs each seat, and no seat here emits spend into the tree |

Wall-clock time is the number this factory can honestly measure. Model spend is
not: nothing in the repository records token usage, and inventing a figure would
be worse than saying where the real one lives.

---

## 6. How the factory catches a bad result

Four independent lines, in the order they fire.

1. **The implementer's own clean-state run.** The handoff is not valid without
   it, and its output is quoted verbatim rather than summarised. This catches
   the ordinary case: the change broke something adjacent.
2. **Reviewer reproduction at a pinned revision.** The reviewer records the
   commit hash before reading anything and refuses work that has moved
   underneath it. Every claimed fix is re-run by the reviewer with the command
   quoted in full. This catches the persuasive case: a handoff that describes a
   result the code does not produce.
3. **Quality-assurance attack.** Boundaries, ordering, repetition, wrong types,
   empty input, concurrency — the directions the shipped checks did not ask
   about. This catches the coverage case: code that passes everything that was
   written down and is still wrong.
4. **The objective harness.** `python -m harness check` for the repository's
   offline gates, and `python -m harness run` for the shipped suites per stage.
   This catches the case no seat's opinion can: a submission that does not build
   or does not serve.

**Recovery.** A rejected item goes back to the architect carrying its original
identifier, the reproduction, and the specification passage. The architect
either re-scopes it or routes it to the implementer; the reviewer that raised it
is never the seat that repairs it, and quality-assurance re-runs the same
reproduction at the new revision and records whether it moved. An item is closed
only when that reproduction is green — which is why a defect can be returned
several times without anyone losing track of which defect it is.

**What failed while building this factory.** Three things are worth recording
because they changed the design rather than being worked around:

- *Quoted results that did not reproduce.* Early handoffs reported green runs
  that were not re-runnable at the stated revision. Fix: acceptance now requires
  the architect to re-execute the command at the pinned hash; a handoff without
  a hash is returned unread.
- *Checks edited to match the code.* One unit changed an expectation instead of
  the behaviour. Fix: editing a check became a separate work item with its own
  justification, reviewable on its own, and the reviewer treats an expectation
  change as a claim needing a specification citation.
- *Verification blocked by a missing Docker daemon.* The container path could not
  be run on this machine at all. Fix: the offline gates and the shipped suites
  are run locally against a started service, and every report states plainly
  that the container build is unverified rather than implying it passed.

---

## 7. Current status

The status table in `README.md` is authoritative for which stages exist and what
has been verified; this file should not be read as a claim that all four stages
are complete. Anything marked unverified here is unverified because the tool
that would verify it was unavailable, not because it was attempted and skipped.
