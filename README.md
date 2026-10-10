# Tablekeeper — ProofForge Dark Factory

**A specification-driven software factory built with BAND Desktop for the WeAreDevelopers × BAND Dark Factory Hackathon.**

ProofForge is an evidence-gated, multi-agent software factory. It coordinates specialized coding-agent seats to plan work, implement bounded changes, challenge results, repair defects, and independently verify software against a written specification.

Its demonstration application is **Tablekeeper**, a restaurant reservation service built around a demanding engineering invariant:

> A reservation system must preserve correctness when requests race, clients retry operations, state changes, and local times cross daylight-saving transitions.

ProofForge's goal is not simply to generate code. It is to make software delivery inspectable: decisions, implementation changes, reviews, test results, defects, and accepted revisions should leave a trace that another developer can examine.

---

## 1. The problem we address

Restaurant reservations appear straightforward until real-world conditions interact:

- Two customers attempt to reserve overlapping intervals.
- A client retries a request after losing the response.
- A reservation is moved or amended while another operation is in progress.
- A restaurant operates in a time zone with daylight-saving transitions.
- State must be exported, imported, and restored without silently corrupting existing records.
- A validation failure must not leave partially applied changes.

These are not merely endpoint-design problems. They are correctness, consistency, concurrency, and recovery problems.

Tablekeeper provides the application challenge. ProofForge provides the process for engineering and verifying the solution.

## 2. Two connected deliverables

| Deliverable | Purpose |
|---|---|
| **ProofForge** | A reusable factory for specification-driven planning, implementation, review, verification, and defect recovery. |
| **Tablekeeper** | A restaurant reservation service used to exercise the factory against concrete API and state-management requirements. |

The application is the product of the factory. The factory is the engineering system that should make the product reproducible, reviewable, and maintainable.

## 3. ProofForge architecture

ProofForge separates responsibilities so that producing a change is not the same as accepting it.

### Architect — planning and coordination

- Inspects the repository and the complete specification.
- Establishes the existing behavior and identifies specification gaps.
- Breaks work into bounded, verifiable tasks.
- Assigns ownership and defines acceptance criteria.
- Coordinates handoffs and tracks evidence.

### Implementer — scoped engineering

- Implements assigned changes against the specification.
- Preserves existing behavior unless a change is justified.
- Produces focused diffs and test evidence.
- Reports the exact revision and any unresolved defects.

### Reviewer — independent challenge

- Inspects the implementation and the exact revision under review.
- Checks specification compliance rather than accepting implementation claims.
- Investigates edge cases, incorrect status codes, ordering, timestamps, and state transitions.
- Rejects defects with reproducible evidence.

### Quality Assurance — independent verification

- Exercises externally observable behavior.
- Tests failure paths and boundary conditions.
- Checks whether the evidence actually supports the claimed result.
- Verifies fixes without relying solely on the implementer's report.

The intended workflow is:

**Specify → Decompose → Implement → Review → Repair → Verify → Accept**

A change is not accepted merely because an agent says it is finished. Acceptance requires evidence appropriate to the change.

## 4. What makes the factory reusable?

ProofForge is designed around general engineering responsibilities rather than application-specific instructions.

Its intended reusable principles are:

- **Specification first:** derive obligations from the requirements before changing code.
- **Bounded ownership:** give each seat a defined task and explicit completion criteria.
- **Independent review:** separate implementation from acceptance.
- **Evidence-bearing handoffs:** report revisions, commands, observed results, and remaining risks.
- **Controlled repair:** route concrete defects back to the responsible seat.
- **History preservation:** retain the relationship between room activity, Git commits, tests, and decisions.
- **Honest reporting:** distinguish verified results from untested assumptions and environmental blockers.

These principles are intended to apply beyond restaurant reservations. The track-specific requirements belong in the task specification, not in the factory's generic operating mandates.

## 5. Tablekeeper engineering

The service is implemented in Python using the standard library, including an HTTP server and SQLite persistence.

The inspected implementation includes the following engineering mechanisms:

| Area | Implementation approach |
|---|---|
| HTTP service | `ThreadingHTTPServer` with explicit request routing |
| Persistence | SQLite database with WAL journaling |
| Write transactions | `BEGIN IMMEDIATE` for serialized write operations |
| Reservation identity | Restaurant-scoped record identifiers |
| Fixture ordering | Explicit ordinal values for deterministic fixture ordering |
| Authentication | PBKDF2-HMAC-SHA256 password derivation and opaque bearer tokens |
| Idempotency | Persisted receipts keyed by request identity and operation |
| Time handling | IANA time zones and explicit handling of daylight-saving transitions |
| Interval conflicts | Half-open intervals, `[start, end)`, compared in absolute time |
| Test isolation | Real HTTP service exercised against temporary databases |

These mechanisms are engineering choices, not proof that every requirement has passed. Their correctness must be established through specification-based tests and reproducible verification.

### Important correctness properties

**No overlapping reservations**

Concurrent operations must not create conflicting bookings for the same table. Transaction boundaries and interval arithmetic are part of the solution, but concurrency must still be tested under realistic contention.

**Safe retries**

Repeated requests must not accidentally duplicate successful operations. Idempotency behavior must be checked for both identical and conflicting request payloads.

**Atomic state changes**

Multi-item operations and imports must either satisfy their required invariants or fail without leaving partially applied state.

**Correct time semantics**

Local wall-clock times must be interpreted in the restaurant's time zone. Nonexistent local times, repeated local times, and interval boundaries require explicit handling.

**Predictable API contracts**

Response envelopes, field types, ordering, status codes, and error messages are part of the specification—not cosmetic implementation details.

## 6. Verification philosophy

ProofForge treats verification as a separate engineering responsibility.

The verification process is intended to cover:

1. Specification-derived acceptance criteria.
2. API behavior and response contracts.
3. Invalid inputs and boundary conditions.
4. Concurrent operations and transaction safety.
5. Idempotency and retry behavior.
6. Time-zone and daylight-saving behavior.
7. State export/import and failure recovery.
8. Independent review of the exact Git revision.
9. Clean-container startup and reproducibility.
10. Evidence that distinguishes passing checks from unresolved defects.

A test suite passing is meaningful only when the test count, scope, environment, and actual outcome are recorded. Passing the supplied tests alone does not establish complete specification compliance.

## 7. Repository map

The repository should be read as two related artifacts: the factory's evidence and the application it produces.

| Path | Purpose |
|---|---|
| `stage-1/` | Current tracked application stage |
| `stage-1/app/main.py` | HTTP routing, request validation, and service behavior |
| `stage-1/app/store.py` | Persistence and state operations |
| `stage-1/app/auth.py` | Authentication functionality |
| `stage-1/app/tz.py` | Time-zone handling |
| `stage-1/app/intervals.py` | Reservation interval calculations |
| `stage-1/tests/` | Automated behavioral and specification tests |
| `stage-1/RUN.md` | Stage-specific execution instructions |
| `stage-1/Dockerfile` | Container build definition |
| `REQUIREMENTS.md` | Requirement traceability checklist |
| `PLAN.md` | Engineering plan and work tracking |

**Repository scope:** the latest audited snapshot contained `stage-1/` as its only stage directory. The presence of functionality associated with later requirements inside that directory must not be confused with a separately completed, independently buildable Stage 2, 3, or 4 deliverable.

The full submission structure, room export, factory documentation, mandates, and stage folders should be checked against the official participant guide and the actual repository before any completeness claim is made.

## 8. Running and testing the current stage

### Prerequisites

- Python 3.12 or later
- Git
- Docker for the official container verification workflow

### Run the service

From the repository root, follow the instructions in `stage-1/RUN.md`. The application is designed to listen on the configured `PORT`.

### Run the automated tests

From the `stage-1/` directory:

```bash
python -m unittest discover -s tests -t .
```

Record the full test count and all failures, errors, and skips. Do not report a passing gate when the command exits unsuccessfully.

### Container verification

The Dockerfile must be built and the resulting service tested from a clean environment. A successful unit-test run or static packaging test is not a substitute for a successful container build and HTTP smoke test.

See the official [Dark Factory participant guide](https://github.com/band-ai/dark-factory-wearedevs/blob/main/docs/participant-guide.md) for the required harness and isolated-stage verification workflow.

## 9. Verification status and known limitations

Engineering credibility requires reporting the state of the artifact as measured, not as hoped for.

**Last recorded verification snapshot: 7 October 2026.**

| Check | Recorded result |
|---|---|
| Standard-library unit suite | 262 tests: 9 failures, 1 error, 1 skipped |
| Specification gate | Failed |
| Official Stage 1 harness | 92 passed, 28 failed out of 120 |
| Docker build and clean-container verification | Not run; Docker was unavailable on the audited host |
| Separate stage directories | Only `stage-1/` was present in the audited repository snapshot |
| Credential protection | An untracked `agent_config.yaml` required `.gitignore` protection and credential review |

These results are historical evidence from the supplied audit reports, not a claim about a later revision. A subsequent revision should replace this table only after the corresponding checks have been rerun and their outputs recorded.

The principal outstanding work identified by the audits included reservation slot-grid and opening-hours validation, response envelopes and ordering, required response fields, validation status codes, availability time-zone metadata, import/replacement behavior, and test-harness cleanup.

Container verification, the full required submission structure, and independent acceptance of repaired changes also remained to be established in those reports.

## 10. Evidence and reproducibility

The repository and factory records should make it possible to answer four questions:

- **What was required?** Inspect the written specification and requirement traceability.
- **What changed?** Inspect the relevant commit and its diff.
- **How was it challenged?** Inspect the review findings, test evidence, and handoff records.
- **Was it accepted?** Inspect the independent verification result for the exact revision.

The official submission requires a complete, unchanged BAND room export and the corresponding Git history. These are primary evidence of collaboration; descriptive documentation alone cannot establish that agents performed the work or that a review changed an implementation.

Secrets must not be published in source files, configuration, or room exports. The room export should be inspected for private values before being committed.

## 11. Alignment with the judging rubric

The project is designed around the competition's three criteria:

**Factory — 50%**

Reusable mandates, bounded responsibilities, specification-derived tasks, evidence-bearing handoffs, independent review, failure recovery, and reproducible setup.

**App — 25%**

Reservation correctness, consistent API contracts, state integrity, concurrency safety, retry handling, and correct time-zone behavior.

**Agent Teamwork — 25%**

Observable collaboration among distinct BAND seats, reciprocal handoffs, review that challenges actual changes, and a Git history traceable to the room.

These are design goals, not a declaration of a particular score. Actual results depend on the committed artifacts, stage completion, full judging checks, and the evidence available to the judges.

## 12. Project information

- **Project:** Tablekeeper — ProofForge Dark Factory
- **Track:** Tablekeeper — restaurant reservation system
- **Platform:** BAND Desktop
- **Event:** WeAreDevelopers × BAND Dark Factory Hackathon
- **Submission page:** [Tablekeeper — ProofForge Dark Factory](https://lablab.ai/ai-hackathons/wearedevelopers-hackathon/proofforge/tablekeeper-proofforge-dark-factory)
- **Rules and verification instructions:** [Official participant guide](https://github.com/band-ai/dark-factory-wearedevs/blob/main/docs/participant-guide.md)

---

## Closing principle

ProofForge is built around one principle:

**Software should earn acceptance through evidence, not confidence.**

The long-term goal is a reusable factory in which specialized agents produce software, independently challenge it, repair concrete defects, and leave enough reproducible evidence for another engineer to understand and verify the result.

The quality of that factory is measured not by how confidently it describes itself, but by what it builds, what it catches, what it repairs, and what an independent reviewer can reproduce.
