# Plan: Tablekeeper Hackathon Submission — stage 1 hardening (rev. 4.1)

Owner of this plan: Architect (@velishalalingaraju/architect).
Status: **Phase 1 complete — inspection only. No product code has been modified.**
Phase 2 (implementation) starts only when the room owner authorises it.

**Rev. 4.1 supersedes rev. 4.0**, which the Reviewer REJECTED on 2026-10-07T14:18:30Z.
Rev. 4.1 adopts the Reviewer's independent findings, their exact `WI-00 → WI-10` order, and
their exact per-item acceptance evidence.

---

## 0. What changed from rev. 4.0, and why

| rev. 4.0 did | rev. 4.1 does |
|---|---|
| Used `REQUIREMENTS.md` line numbers as the primary citation | Cites the **Tablekeeper Stage-1 specification** (`stage-1.md`) as `§N:line`, per the Reviewer's audit. `REQUIREMENTS.md` is a derived checklist, useful as a pointer, **not** the contract |
| Ordered items by "how much prose has no implementation" | Uses the Reviewer's dependency order **WI-00 → WI-10 exactly** (§7) |
| Treated "make shipped tests pass" as the goal in places | Every defect is classified **SPEC-FIX / TEST-MOVE / UNCOVERED / BOTH** (§3), and the shipped-test defects are listed separately in §6 |
| Numbered the 14 required defects only implicitly | All **14** of the Reviewer's defects are enumerated, cited and mapped to a WI (§3) |
| `NAMED_DEFECTS` "stays at 23" | The registry is an **instrument**: it may change only when the spec requires it, with a paired test and a citation (§11.2) |
| Contained WI-11 (container) and WI-12 (KDF decision) as numbered work items | **Removed from the WI sequence.** PBKDF2, email, `busy_timeout` and extra `user_id` are **not changes** (§4). Container/README/checklist moved to non-numbered registers (§9) |
| Did not state the protected invariants as hard rules | §5 makes idempotency precedence, atomic moves, DST/timezone and import replacement explicit **do-not-break** clauses with named tests |

**No code was modified to produce this revision.** This document is a plan.

---

## 1. Source of truth and citation rules

1. **The specification is the source of truth.** Not shipped tests, not the gate registry, not
   this plan, not the harness's own wording. Where a shipped test contradicts the specification,
   **the test is wrong and must move**; the product is not changed to satisfy it.
2. **Citation form** is `§N:line` = section `N`, line(s) `line` of the official Tablekeeper
   Stage-1 specification (`stage-1.md`), as used by the Reviewer throughout their audit.
   The document is **not in this repository**; its rulings are on the record in the room, and the
   harness bundle that encodes them lives at
   `C:\Users\linga\Downloads\dark-factory-wearedevs-main\dark-factory-wearedevs-main\`.
   Where this plan must act on a citation, the acting seat **re-reads the specification text at
   that location and quotes it in the commit message** — a citation without a quotation is a claim.
3. **`REQUIREMENTS.md`** is a derived checklist: **213 lines, 123 rows, 0 checked** (measured for
   this revision: `Get-Content` line count, `Select-String -Pattern "- \[ \]"` = 123, `- "[x]"` = 0).
   Its own section headers `## 2 … ## 11` mirror the specification's sections, so `§8` means the
   same *section* in both documents — **but the line numbers do not match** (verified: the email
   `a@b` rule is at `§6:211` of the specification, while `REQUIREMENTS.md:211` is a Definition-of-
   Done row about the Dockerfile; the KDF rule is `§6:224` of the specification while
   `REQUIREMENTS.md`'s copy of it is at line 73). Rows are orientation aids only. Never settle a
   dispute from a `REQUIREMENTS.md` line number, and never assume two seats mean the same
   document when they write the same `§N:line` notation.
4. **Shipped tests are evidence of current behaviour**, never authority. `tests/test_spec_stage1.py`
   `NAMED_DEFECTS` is a *triage instrument* recording which divergences are known; it does not
   decide what is correct.
5. **The gate never decides truth either.** `GATE PASSED` means "every failing test is a known,
   named divergence" — it is a regression net, not a conformance verdict.

---

## 2. Baseline (measured on this machine, not inferred)

Host: `win32`, Python 3.12.10, **no Docker**. Every number below is command output.

### 2.1 Revision

- `HEAD` = **`2c10382`** (WI-00, tests-only), parent `e72a0c2`.
- `git status --short` → ` M PLAN.md` only (this document; pre-existing, not part of any WI).
- `stash@{0}` exists and is **protected** — see §11.3.

### 2.2 Shipped suite, post-WI-00

| command (from `stage-1/`) | result |
|---|---|
| `python -m unittest tests.test_spec_stage1 tests.test_spec_stage2` | `Ran 30 tests in 32.454s` — `FAILED (failures=8)` — re-run by Architect for rev. 4.1 |
| `python -m unittest discover -s tests -t .` | `Ran 262 tests` — `FAILED (failures=8, skipped=1)` — `errors=0`, run twice by Implementer |
| SPEC GATE | `GATE PASSED — every failing test is a named defect.` `named 23 / red 8 / green 15 / declined 1` |

The 8 red names (all in `tests/test_spec_stage1.py` except the last):

```
internal_error_message_is_redacted
list_reservations_envelope_and_desc_order
party_size_wrong_type_is_422
reservation_body_has_ends_at_and_created_at
restaurants_list_envelope
skipped_local_time_is_invalid_local_time
slot_grid_and_opening_hours_codes
import_is_replacement_and_preserves_receipts   (tests/test_spec_stage2.py)
```

WI-00 removed the former 9th failure (`the_creation_time_is_not_regenerated`, WinError 32) and
with it the gate's own subset-assertion failure. **Errors = 0.**

### 2.3 Official hidden harness (Reviewer's evidence)

```
28 FAILED / 92 PASSED of 120
```

Reproduction (recorded by the Reviewer; the Architect has **not** re-run it — it stands on the
Reviewer's evidence until re-run):

```
service:  cd stage-1 && $env:PORT=8099; $env:TABLEKEEPER_DB="$env:TEMP\opencode\harness.sqlite"; python -m app.main
run:      $env:PYTHONPATH="<bundle>;<bundle>\tablekeeper\test"
          python -m pytest <bundle>\tablekeeper\test\stage_1 --base-url http://127.0.0.1:8099 -p harness.plugin -q
where <bundle> = C:\Users\linga\Downloads\dark-factory-wearedevs-main\dark-factory-wearedevs-main
```

WI-00 is tests-only and cannot move these numbers; **the harness must be re-run after remediation
as the final conformance check**, not the shipped suite alone.

---

## 3. The 14 required defects

Class legend — **SPEC-FIX**: product code must change. **TEST-MOVE**: a shipped test contradicts
the spec and must be corrected (the product is right or is fixed elsewhere). **UNCOVERED**: the
spec obligation has no shipped test, so fixing it moves no counter unless a test ships with it.
**BOTH**: code and tests change in the same work item.

| # | id | defect | spec | class | WI |
|---|---|---|---|---|---|
| 1 | S1-1 | Create never emits `not_on_slot_grid`, `outside_opening_hours`, `party_exceeds_capacity`, `invalid_local_time` | §8:349-350, §8:350, §11:463 | SPEC-FIX + UNCOVERED | **WI-02** |
| 2 | S1-2 | PATCH and moves perform **no** hours/grid validation at all | §8:387 (PATCH identical to create), §11:463 | SPEC-FIX + UNCOVERED | **WI-05** |
| 3 | S1-3 | `GET /reservations` returns a bare array in insertion order, not `{"reservations":[...]}` ordered by `starts_at_utc` desc then `reference` | §8:356-360 | SPEC-FIX | **WI-01** |
| 4 | S1-4 | `GET /restaurants` returns a bare array, not `{"restaurants":[...]}` | §8:271-273 | SPEC-FIX | **WI-01** |
| 5 | S1-5 | Reservation body omits `ends_at` and `created_at` | §8:330-341 | SPEC-FIX | **WI-06** |
| 6 | S1-6 | `starts_at`/`ends_at` rendered `+00:00` (UTC), not in the restaurant zone | §8:305, §8:330-341, §9:416 (readings contested — see §7, WI-10) | SPEC-FIX + TEST-MOVE | **WI-10** |
| 7 | S1-7 | Wrong-typed `party_size` → 400 `malformed_request`, must be 422 `validation_failed` (create **and** PATCH; moves already correct) | §5:172-174, §5:177 | SPEC-FIX + TEST-MOVE | **WI-04** |
| 8 | S1-8 | `GET /availability` response omits `timezone` | §8:294 | SPEC-FIX + UNCOVERED | **WI-07** |
| 9 | S1-9 | `POST /_test/reset` accepts fixture references violating the `A-Z0-9`, length 6–12 rule | §10:420-440, §8:344, §5:168 | SPEC-FIX + UNCOVERED | **WI-08** |
| 10 | S2-10 | Batch swap: not all-or-nothing / conflict among **resulting** bookings not → 409 | §11:459-465 | SPEC-FIX + UNCOVERED | **WI-09** |
| 11 | S2-11 | Client errors answered 5xx (dup email → 500, dup reference → 500, bad `starts_at_utc`/unknown timezone accepted then 500 on `GET /availability`) | §5:185 | SPEC-FIX + UNCOVERED | **WI-08** |
| 12 | S2-12 | Idempotency key resolved **after** field validation: used key + `party_size:"four"` → 400, must be 409 `idempotency_key_reuse` | §7:243-246 | SPEC-FIX + UNCOVERED | **WI-03** |
| 13 | S2-13 | Missing required body field → 400, must be 422 `validation_failed` | §5:160-177 | SPEC-FIX + TEST-MOVE | **WI-04** |
| 14 | S2-14 | Fixture `reservation_id` discarded; body must carry `reference` **and** `reservation_id` | §8:330-341, §4:141-142 | SPEC-FIX | **WI-06** |

**Coverage map of the 8 red names:** 6 belong to the 14 —
`slot_grid_and_opening_hours_codes` and `skipped_local_time_is_invalid_local_time` → defect 1
(S1-1); `party_size_wrong_type_is_422` → defect 7 (S1-7); `list_reservations_envelope_and_desc_order`
→ defect 3 (S1-3); `restaurants_list_envelope` → defect 4 (S1-4);
`reservation_body_has_ends_at_and_created_at` → defect 5 (S1-5).
The other 2 — `internal_error_message_is_redacted` and
`import_is_replacement_and_preserves_receipts` — are **instrument/test defects** (§8, H1/H2), not
among the 14.

**Six of the 14 defects (S1-2, S1-8, S1-9, S2-10, S2-11, S2-12) have no shipped red name at all**,
and defect 1's `party_exceeds_capacity` leg has no test and **0 hits** in `app/` and `tests/`.
Each must ship with a test in the same commit or the fix is invisible to the gate.

---

## 4. Already-correct behaviour — explicitly **not** changes

Confirmed spec-correct by the Reviewer; **no work item may modify these.** Changing any of them is
a rejection of that work item.

| current behaviour | spec | verdict |
|---|---|---|
| Email `a@b` accepted | §6:211 | **no change** (permissive by design) |
| PBKDF2-HMAC-SHA256, 120k rounds | §6:224 ("bcrypt, scrypt or Argon2 **or an equivalent**") | **no change** |
| `busy_timeout=10000` | §2:47 | **no change** (internal, no observable obligation) |
| Extra `user_id` in reservation bodies | — (not prohibited) | **no change** |
| **401 after `POST /_test/reset`** | §3.3, §10:440 | **no change** — reset clears state, re-login is correct |
| Export/import round trip green | §10:440 | preserve |
| Import rejects a bad envelope without changing the destination | §10:440 | preserve |
| Receipt replay, path-scoped idempotency, key reuse after 4xx | §7 | preserve |
| Query-integer rules incl. non-ASCII digits (`\xbd` etc. not decimal) | §5 | preserve |
| 10-client burst → 1×201 + 9×409, no 5xx | §11 | preserve |
| Cancel-twice, 404 non-leak, restaurant-detail shape | §8 | preserve |
| Seeded booking occupancy/ownership, fixture ordinal ordering | §8/§10 | preserve |
| Wrong-type `table_id` / `starts_at_local` stay **400** on amendment paths | §5 general row | preserve — only `party_size` moves to 422 |

The four P4 informational items (email, PBKDF2, `busy_timeout`, extra `user_id`) were reported by
the Reviewer as **open and unaudited**. They are recorded here as **no-change by owner instruction**
and are not scheduled; nothing in this plan claims they were audited.

---

## 5. Protected invariants — do-not-break clauses

These are **hard constraints on every work item**, not goals. A change that breaks one is rejected
regardless of whether its own target went green.

### 5.1 Idempotency precedence (protects S2-12's fix from collateral damage)

- Key resolution **must move before field validation** (§7:243-246), so a reused key with a
  different body answers **409 `idempotency_key_reuse` even when the body is itself invalid**.
- A key spent by a **successful** request still replays with **200 and the stored body intact**.
- A key spent by a **failed write that wrote no receipt** is **not consumed** — the retry must
  succeed. "No receipt written ⇒ key not consumed" must survive the reorder.
- Path scoping stays: `idempotency_key_scoped_by_path` green; the same key on a different path is
  independent.
- **Never** key on the request body alone, and never resolve the key after validating fields —
  both orderings are wrong at opposite ends.

### 5.2 Atomic reservation moves (protects S2-10's fix)

- Batch operations are **all-or-nothing** (§11:459-465): one conflicting booking aborts the whole
  batch and **rolls back**, leaving every slot exactly as it was.
- `a_refused_batch_leaves_the_slot_still_held` must stay green — a refused batch releases nothing
  it had not already released.
- Half-open `[start, end)` occupancy in `app/intervals.py` is unchanged; two bookings sharing an
  instant boundary are still not a conflict.
- `moves`' existing note that a move **onto an existing booking's own start** is not a conflict
  (`app/main.py:855-860`) is preserved.
- Transaction boundaries (`BEGIN IMMEDIATE`, one connection per operation) are not weakened.
- `test_fifty_concurrent_requests_produce_no_5xx` and the 10-client burst remain green after every
  WI that touches occupancy or validation.

### 5.3 DST / timezone handling (protects WI-04, WI-05, WI-10)

- `app/tz.py`'s resolution of naive local times against the IANA zone — spring-forward gap
  rejected, fall-back repeated hour resolved to the first occurrence — is **unchanged**.
- `slot_end_is_absolute_across_transitions` stays green; a slot that spans a transition still ends
  at the right absolute instant.
- Nonexistent local time remains **422 `invalid_local_time`** (this is a code correction inside
  WI-02, not a weakening).
- WI-10's rendering change is **format-only**: no stored value, no instant, no rounding changes —
  only the offset written into the response string, using the existing tz helpers, consistently on
  create, PATCH, moves and list.

### 5.4 Import replacement semantics (protects WI-08)

- Import is a **replacement** of destination state and **preserves receipts** (§10:440);
  `export_and_import_are_served` and
  `import_rejects_a_bad_envelope_without_changing_the_destination` stay green.
- Tokens remain valid after **import** (§:177-180) but are cleared by **reset** (§3.3, §10:440).
  These are two different rules; do not "fix" the 401 by keeping tokens alive across reset, and do
  not invalidate tokens after import.
- Reset returns 204 and leaves only the posted fixture; the destination is untouched when a
  fixture is rejected.

---

## 6. Shipped-test defect register — tests that must move

These are **not** product defects. Listed so that no seat "fixes" a product that is already right.
Each correction ships with its `§N:line` quoted in the commit message.

| # | test | asserts today | must become | why |
|---|---|---|---|---|
| T1 | `tests/test_service.py:407` `test_a_field_of_the_wrong_type_is_malformed` and `:413` | `(400, malformed_request)` for `party_size='3'/3.0/True/None/"four"` | `(422, validation_failed)` for `party_size` **only** | §5:172-174, §5:177 — specific rule beats the general wrong-type row |
| T2 | `tests/test_patch.py` `SameValidationAsCreate` (4 legs) and the `moves` WrongJsonType legs | same 400 for `party_size` | 422 for `party_size`; other fields unchanged | §5:172-174 via §8:387 |
| T3 | `tests/test_moves.py:656` `Occupancy` | `22:00 → 201` | `22:00 → (422, outside_opening_hours)` | §8:305 (`slot + duration ≤ closes`), §8:350. Default fixture `opens 18:00 / closes 23:00 / slot 30 / duration 90` ⇒ latest legal start 21:30 |
| T4 | `tests/test_patch.py:290` `ReleasedAndReservedTogether` | `22:00 → 200` | `(422, outside_opening_hours)` | same |
| T5 | `tests/test_cancel.py:139` `SuccessfulCancellation` | `22:00 → 201` | `(422, outside_opening_hours)` | same |
| T6 | `test_a_move_just_past_an_existing_booking_does_not_conflict` | `20:29 → (409, table_unavailable)` | `20:29 → (422, not_on_slot_grid)`; keep the `20:30 → 201` leg; move the overlap leg on-grid to `20:00 → (409, table_unavailable)`; optionally add `20:29 → 422` to prove off-grid inputs never reach occupancy | §11:463 — non-occupancy errors take precedence over the 409; §8:304-305 — an off-grid start has no well-formed slot for the occupancy question |
| T7 | `tests/test_service.py:390` `test_a_table_too_small_for_the_party_is_a_validation_failure` | `(422, validation_failed)` | `(422, party_exceeds_capacity)` | §8:350 — **product code defect (S1-1)**, not a test defect: the rule is enforced at `app/main.py:545-546` but reports the generic code. `party_exceeds_capacity` has **0 hits** in `app/` and `tests/` |
| T8 | `tests/test_service.py:395` `test_a_nonexistent_local_time_is_rejected` | `validation_failed` | `invalid_local_time` | §8:349-350 — same, product reports the wrong code |
| T9 | `tests/test_spec_stage2.py` `test_import_is_replacement_and_preserves_receipts`, leg "…is undone by the import" | expects `4` | expects `3` | the `party_size=3` batch move runs **before** the export, so `3` is correct and the leg's ordering assumption is wrong |
| T10 | same test, leg "…and clears the imported bookings too" | expects `(200, 0)` after reset | re-authenticate: expects `(401, -1)` from the stale token **and then** `(200, 0)` after re-login | §3.3, §10:440 — 401 after reset is correct; §:177-180 — tokens valid after import |
| T11 | `tests/test_spec_stage1.py:600` `test_internal_error_message_is_redacted` | triggers a `UNIQUE constraint failed: tables.id` 500 that can no longer fire (tables are keyed `(restaurant_id, id)`) | **same assertions**, new trigger: break the schema beneath a valid fixture so the handler's own `SELECT` raises | stale trigger, not a product defect. Redaction (`GENERIC_500_MESSAGE` + correlation id, `app/main.py:1063-1068`) is intact |
| T12 | gate entry `DECLINED["starts_at_rendered_in_restaurant_zone"]` | declined, justified with "the spec never constrains the offset of `starts_at`" | **reversed**: §8:305/§8:330-341/§9:416 require restaurant-zone rendering per the Reviewer's ruling | contested reading — resolved in WI-10 (§7, WI-10) |

**Rule:** where a product defect and a test defect share one input (T1/T7, T8), both are corrected
**in the same commit**, or the suite is left red by the correction and the next seat inherits noise.

---

## 7. Work breakdown — WI-00 → WI-10, in the Reviewer's exact order

Legend: `IMP` Implementer, `REV` Reviewer, `QA` Quality Assurance, `ARC` Architect.
No seat accepts its own work: **IMP produces, REV reviews, QA verifies, ARC accepts.**
Every handoff states: exact revision (`git rev-parse HEAD`), work completed, remaining risks,
checks executed, failures observed, evidence paths.

---

### WI-00 — Baseline / infrastructure hygiene ✅ **DONE, accepted by owner**

- **Scope (infrastructure only, and it must stay that way):** close the leaked SQLite handle in
  `tests/test_moves.py` so `test.sqlite` is not held open on Windows (WinError 32 in
  `tests/support.py:160`). **Tests only** — no `app/` file, no assertion, no `NAMED_DEFECTS`.
- **Evidence (already collected, accepted 2026-10-07T14:29:26Z):**
  commit **`2c10382`**; `git diff --stat` = `stage-1/tests/test_moves.py | 5 ++++-`, 1 file,
  4 insertions, 1 deletion; two full-suite runs `Ran 262 … FAILED (failures=8, skipped=1)`,
  `errors=0`, `GATE PASSED`; `test_the_creation_time_is_not_regenerated` no longer appears.
- **Acceptance evidence the Reviewer asked for:** `python -m unittest
  tests.test_moves.Identity.test_the_creation_time_is_not_regenerated` → `ok`; support teardown
  closes the handle. *(Verified by the suite run above.)*
- **Rule:** this item is closed. **No further scope may be added to WI-00.** Anything else found in
  `tests/support.py` or the harness goes to §8, not here.

---

### WI-01 — Envelope / list responses  *(defects 3, 4 — S1-3, S1-4)*

- **Obligation:** `GET /reservations` answers `{"reservations":[...]}` ordered by `starts_at_utc`
  descending, then `reference` (§8:356-360) — today it returns a bare array ordered
  `ORDER BY created_at, reference` (`app/main.py:430`), which is creation order, a different
  order. `GET /restaurants` answers `{"restaurants":[...]}` (§8:271-273) — today a bare array.
- **Class:** SPEC-FIX.
- **Owner:** IMP · **Input:** the two list handlers and the `ORDER BY` clause.
- **Acceptance evidence (Reviewer's, verbatim):** envelope for the list endpoints;
  `test_keys_are_scoped_per_user` passes (the `KeyError: 0` disappears).
- **Completion condition:** gate names `list_reservations_envelope_and_desc_order` and
  `restaurants_list_envelope` **green**; empty `GET /reservations` still `{"reservations":[]}`.
- **Protection:** the order must be by **`starts_at` instant**, not by flipping `created_at`'s
  sign — QA seeds a reservation created later but starting earlier and proves it sorts first.
  Cancellations must still be listed alongside confirmed.
- **QA:** order probe above; **REV:** confirm no other response shape moved.

---

### WI-02 — Core error codes on create  *(defect 1 — S1-1)*

- **Obligation:** create must emit all four specified codes (§8:349-350):
  `not_on_slot_grid`, `outside_opening_hours`, `party_exceeds_capacity`, `invalid_local_time`.
  Today the grid and hours rules are **not consulted at all** on the booking path; capacity is
  enforced (`app/main.py:545-546`) but reports `validation_failed`; the DST gap is caught
  (`app/main.py:553-556`) but also reports `validation_failed`. The literal
  `party_exceeds_capacity` has **0 hits** in `app/` and in `tests/`.
- **Class:** SPEC-FIX + UNCOVERED (`party_exceeds_capacity` has no test at all).
- **Owner:** IMP · **Input:** `_validate_booking_fields` (`app/main.py:531`), which today checks
  table existence, capacity, local-time parse and DST — and nothing else about hours or grid.
- **Same-commit test moves:** T7 (`test_service.py:390` → `party_exceeds_capacity`) and T8
  (`test_service.py:395` → `invalid_local_time`). **Gate bookkeeping:** register
  `party_exceeds_capacity` as a **new** `NAMED_DEFECTS` entry with its paired test method
  (`NamedDefectsHaveTests` enforces the pair) — see §11.2 for the rule governing that change.
- **Acceptance evidence (Reviewer's, verbatim):** probe create cases produce
  `not_on_slot_grid`, `outside_opening_hours`, `party_exceeds_capacity`, `invalid_local_time`.
- **Completion condition:** gate `slot_grid_and_opening_hours_codes` and
  `skipped_local_time_is_invalid_local_time` green; the new name green; the other wrong-type
  codes (`table_id`, `starts_at_local`) unchanged.
- **Protection (§5.3):** the DST gap rejection must remain a rejection; only its **code string**
  changes. Lift the grid arithmetic already computed in `get_availability` into **one shared
  helper** — REV greps for a second copy.
- **QA:** boundary probes — first slot of the day, last slot that still fits, one step past
  `closes`, a closed weekday, and a DST-transition day (grid vs. skipped hour).

---

### WI-03 — Validation order / idempotency  *(defect 12 — S2-12)*

- **Obligation:** the idempotency key must be resolved **before** field validation
  (§7:243-246), so a reused key with a different body answers **409 `idempotency_key_reuse`**
  rather than 400. Observed today: used key + `party_size:'four'` → `(400, malformed_request)`.
- **Class:** SPEC-FIX + UNCOVERED (no shipped red name for this; a candidate name and test exist
  only in the stash — §11.3).
- **Owner:** IMP · **Input:** the create path in `_resolve_booking` (`app/main.py:588`,
  create branch `:613`), where field validation currently precedes receipt lookup.
- **Acceptance evidence (Reviewer's, verbatim):** unit test
  `key_resolution_outranks_field_validation_on_create` passes (**all 4 legs**); a key spent by a
  success replays **200**; a key spent by a failed write with **no receipt** reuses on retry.
- **Completion condition:** all four legs green; `idempotency_key_scoped_by_path` green; the
  receipt-replay tests green; no change to which failures consume a key.
- **Protection (§5.1):** this is the most delicate reorder in the plan. REV must read the diff for
  the three specific regressions — success replay silently becoming 409, an unreceipted failure
  consuming the key, and path scoping collapsing. QA re-runs every receipt test in
  `tests/test_service.py` and `tests/test_moves.py`.
- **Order note:** this sits **before** WI-04/WI-05 deliberately, so that later validation
  expansions inherit the correct precedence instead of re-breaking it.

---

### WI-04 — Field validation semantics  *(defects 7, 13 — S1-7, S2-13)*

- **Obligation:**
  1. Wrong-typed `party_size` → **422 `validation_failed`** on create **and** PATCH
     (§5:172-174, §5:177). Today both raise `_malformed` 400 (`app/main.py:468`, `:657`).
     `moves` already raises `_invalid` (`app/main.py:869-870`) — **leave it**; its comment at
     `:855-860` already records the §5 general-vs-specific tension.
  2. A **missing required field** → **422 `validation_failed`** (§5:160-177), not 400.
- **Class:** SPEC-FIX + TEST-MOVE (T1, T2 above).
- **Owner:** IMP · **Input:** the `isinstance(... party_size ...)` checks in create and PATCH, and
  the missing-field branch of `_malformed`.
- **Acceptance evidence (Reviewer's, verbatim):** `test_party_size_wrong_type_is_422` passes;
  missing-field cases return 422.
- **Completion condition:** `party_size_wrong_type_is_422` green; string/boolean/null/float legs
  all 422; `tests.test_service` and `tests.test_moves` green after the T1/T2 corrections.
- **Protection (§5.3):** **REV's primary check is what did *not* move** — `table_id` and
  `starts_at_local` wrong types stay **400** on the amendment paths (`tests/test_moves.py:400,407`
  depend on it), and `moves`' existing codes are byte-identical. Specific-beats-general applies to
  `party_size` because §5:172-174 names it explicitly, **not** to every field.

---

### WI-05 — Hours / grid validation on create, PATCH and moves  *(defect 2 — S1-2)*

- **Obligation:** PATCH validation is identical to create (§8:387); moves carry the same
  non-occupancy codes and precedence (§11:463). Today PATCH and `POST /reservation-moves` perform
  **no** opening-hours or slot-grid validation at all.
- **Class:** SPEC-FIX + UNCOVERED (shipped `slot_grid_and_opening_hours_codes` covers create only).
- **Owner:** IMP · **Input:** the shared grid/hours helper from WI-02, wired into
  `patch_reservation` (`app/main.py:657` → `_resolve_booking` `:684` → `:596`) and
  `post_reservation_moves` (`app/main.py:872`).
- **Precedence rule (settled by the Reviewer):** the grid/hours check runs **before** occupancy.
  `20:29` → `(422, not_on_slot_grid)`, **not** 409. Argument of record: §11:463 grants
  non-occupancy errors precedence over the 409 (input order orders errors *across* bookings;
  cutoff is the only within-booking qualifier), and §8:304-305 means an off-grid start has no
  well-formed slot to ask the occupancy question against.
- **Acceptance evidence (Reviewer's, verbatim):** grid check before occupancy — `20:29` move →
  422 `not_on_slot_grid` (not 409); `22:00` → 422 `outside_opening_hours`.
  `test_a_move_just_past_an_existing_booking_does_not_conflict` adjusted per T6; the three 22:00
  tests (T3, T4, T5) return 422.
- **Completion condition:** the adjusted move test green with **both** legs (`20:30 → 201`,
  `20:00 → (409, table_unavailable)`), the three 22:00 tests green at 422, and new coverage proving
  PATCH and moves reject off-grid / out-of-hours input.
- **Protection (§5.2):** `a_refused_batch_leaves_the_slot_still_held` must stay green. **It is not
  a precedence case** — its only error is `party_size: 99` against a capacity-2 table
  (`support.py:45`), so under §8:350 + §5:169 the more specific `(422, party_exceeds_capacity)` is
  correct and the shipped `(422, validation_failed)` is a **product** defect (bucket S1-1/WI-02),
  not a test defect. Do not "fix" it by weakening the capacity rule. Half-open occupancy, the
  move-onto-own-start exemption, and the concurrency tests are untouched.

---

### WI-06 — Reservation body fields  *(defects 5, 14 — S1-5, S2-14)*

- **Obligation:** the reservation body carries `ends_at`, `created_at` (§8:330-341) on create,
  PATCH, moves **and** list responses, and carries **both** `reference` and `reservation_id`
  (§8:330-341, §4:141-142). Today `_reservation_body` (`app/main.py:956-967`) returns 9 fields and
  omits `ends_at`, `created_at` and `reservation_id`.
- **Class:** SPEC-FIX. `created_at` is a real column (`app/store.py:165`); `ends_at` derives from
  `slot_end()` (`app/intervals.py:45`), the same function availability already uses.
- **Owner:** IMP · **Acceptance evidence (Reviewer's, verbatim):** all create/PATCH/moves/list
  responses have `ends_at`, `created_at`, `reference`, `reservation_id`; values match shape.
- **Completion condition:** gate `reservation_body_has_ends_at_and_created_at` green; every
  surface that returns a reservation body carries the full set; no field's existing value moved.
- **Protection:** `reservation_id` is an **alias of `reference`**, not a new identifier — they must
  be equal, and the fixture-supplied value must not be silently discarded. Adding fields must not
  remove or rename any existing one (the `KeyError: 0` class of breakage).

---

### WI-07 — Availability timezone  *(defect 8 — S1-8)*

- **Obligation:** `GET /availability` includes `timezone` (§8:294), e.g. `Europe/Berlin`. **Both**
  response sites omit it: the closed-weekday early return (`app/main.py:376`) and the main return
  (`app/main.py:421`) return only `restaurant_id, date, party_size, slots`.
- **Class:** SPEC-FIX + **UNCOVERED** — no shipped test asserts this.
- **Owner:** IMP adds the field; **QA authors the test** (a seat that writes the requirement test
  is not the seat that verifies it); **REV** confirms the test fails when the field is removed.
- **Acceptance evidence (Reviewer's, verbatim):** `/availability` closed and open branches return
  `timezone`.
- **Completion condition:** new test green on **both** branches; **mutation probe** recorded —
  remove the field → test red; restore → green.

---

### WI-08 — Reset / fixture validation  *(defects 9, 11 — S1-9, S2-11)*

- **Obligation:**
  1. **S1-9:** reset validates fixture references against `A-Z0-9`, length 6–12 (§8:344, §5:168)
     within the reset contract (§10:420-440). Today invalid references are accepted and produce
     bad state. The shipped green name `reset_rejects_invalid_fixture` covers **envelope-level**
     rejection only; the reference-shape dimension is unproven and must gain its own leg.
  2. **S2-11:** no 5xx on client errors (§5:185). Observed: duplicate email → 500; duplicate
     reference → 500; unparseable `starts_at_utc` and unknown timezone accepted with 204 then 500
     on `GET /availability`.
- **Class:** SPEC-FIX + UNCOVERED (both legs).
- **Owner:** IMP · **Input:** the fixture/reset path in `app/store.py` (`export_state` `:705`,
  `import_state` `:754`) and the reset handler's validation.
- **Acceptance evidence (Reviewer's, verbatim):** reset with invalid refs rejected appropriately;
  **no 500s on fixtures**; destination unchanged when a fixture is rejected.
- **Completion condition:** all four observed 5xx inputs answer **422 `validation_failed`** (or the
  specified 4xx) at reset, with the destination still holding the prior state; the reference-shape
  leg green; `export_and_import_are_served`,
  `import_rejects_a_bad_envelope_without_changing_the_destination` and
  `reset_rejects_invalid_fixture` still green.
- **Protection (§5.4):** this WI touches the reset/import path, so it is the highest-risk item for
  import replacement semantics. REV checks explicitly: import still **replaces** destination state,
  receipts still **survive**, tokens still die on **reset** but survive **import**, and a rejected
  fixture changes nothing. T9/T10 test corrections belong to this WI's commit set (§6).

---

### WI-09 — Batch moves semantics  *(defect 10 — S2-10)*

- **Obligation:** a batch is **all-or-nothing**, and an overlap among the **resulting** bookings
  (or with an unlisted booking) → **409 `table_unavailable`** (§11:459-465). Observed: a
  conflict-free swap is refused with 409.
- **Class:** SPEC-FIX + UNCOVERED.
- **Owner:** IMP · **Input:** `post_reservation_moves` (`app/main.py:855-872`) and its occupancy
  computation across the batch.
- **Acceptance evidence (Reviewer's, verbatim):** swap batch → 409 (resulting overlap);
  all-or-nothing preserved; unchanged items retain occupancy; rollback correct.
  *(Note: the reviewer's acceptance line for the swap case reads "Swap test returns 409" — the
  defect as stated is that a **conflict-free** swap is wrongly refused. The implementing seat must
  re-read §11:459-465 and the audit text and record which case each leg covers; ARC will not accept
  an ambiguous leg.)*
- **Completion condition:** a conflict-free swap succeeds **201**; a swap whose *resulting*
  bookings overlap answers 409; after either outcome every slot's occupancy equals what it was
  before the call; `a_refused_batch_leaves_the_slot_still_held` green.
- **Protection (§5.2):** transaction boundaries, rollback, half-open occupancy and the 50-client
  concurrency test are unchanged. **Order:** this WI is deliberately last of the validation items —
  it depends on WI-03's precedence and WI-05's grid-before-occupancy ordering being correct first.

---

### WI-10 — Timezone rendering of `starts_at` / `ends_at`  *(defect 6 — S1-6)*

- **Obligation:** `starts_at` and `ends_at` are rendered in the **restaurant's zone**, not UTC
  (§8:305, §8:330-341, §9:416). Today the handlers call `isoformat()` on UTC values and emit
  `+00:00`; the harness expects `+02:00`/`+01:00` for `Europe/Berlin`.
- **Class:** SPEC-FIX + TEST-MOVE (T12 — the gate's `DECLINED` entry).
- **Contested reading, to be settled inside this WI:** the gate's own `DECLINED` justification
  says "the spec never constrains the offset of `starts_at`: §3.4 requires RFC 3339 with an
  explicit offset and `+00:00` is one, §8 lists `starts_at_local` and `starts_at` as distinct
  fields." The Reviewer ruled that §8:305/§8:330-341/§9:416 require restaurant-zone rendering, and
  the harness agrees. **Rev. 4.1 adopts the Reviewer's ruling** (the owner directed that the plan
  use the Reviewer's findings), reverses the `DECLINED`, and requires the implementing seat to
  **quote the exact specification lines in the commit** before the reversal is accepted. If the
  quoted text does not support restaurant-zone rendering, ARC re-opens the question with the owner
  rather than guessing.
- **Owner:** IMP · **Acceptance evidence (Reviewer's, verbatim):** format is `+HH:MM` for
  `Europe/Berlin`; consistent across endpoints.
- **Completion condition:** `starts_at`/`ends_at` render at restaurant-zone offset on create,
  PATCH, moves **and** list; gate name un-declined and green; **mutation probe** both ways
  recorded (restore UTC → red, restaurant zone → green).
- **Protection (§5.3):** **format-only.** The stored instant, the computed `ends_at`, rounding, and
  every equality/assertion comparing instants must be unaffected — only the written offset changes.
  Use the existing tz helpers; do not re-resolve wall times. This is last in the sequence because
  it touches every response surface and must not confound earlier debugging.

---

## 8. Hygiene register — instrument defects (H1–H5)

**Not numbered work items.** They are test/instrument repairs that must not compete with the WI
sequence. Each runs **alongside** its nearest WI, is reviewed normally, and **never** blocks a WI
gate. All are tests-only: `git diff -- app/` must be empty for each.

| id | item | obligation | owner | evidence |
|---|---|---|---|---|
| **H1** | `internal_error_message_is_redacted` stale trigger | T11: re-point the trigger, **assertions byte-identical**; then a **mutation probe** — restore `str(exc)` in the catch-all → name red; revert → green, both runs recorded | IMP / REV | gate name green with original assertions; probe both directions |
| **H2** | Import residual test legs | T9 (`4` → `3`) and T10 (re-login after reset) — **QA diagnoses and cites the spec row**, IMP edits, REV rules on the reading; never relax an assertion without a citation | QA → IMP → REV | `import_is_replacement_and_preserves_receipts` green; reasoning in the commit message |
| **H3** | Retire the stale skip | `tests/test_moves.py:1644` `@unittest.skip("GET /_test/export is 404 …")` + `NotImplementedError` — export exists and is green, so the batch-receipt round-trip test must be **written and un-skipped** | IMP | suite reports `skipped=0` for it; QA confirms the assertion is real, not a re-run of an existing leg |
| **H4** | `NAMED_DEFECTS` bookkeeping | see §11.2 — register every new spec obligation that had no test; reverse the `DECLINED` in WI-10 | IMP / REV | `NamedDefectsHaveTests` green; no name deleted |
| **H5** | Harness re-run | after the sequence, re-run the official harness (§2.3) and record the new pass/fail count | QA | harness output file + count delta vs `28F/92P` |

---

## 9. Open / deferred items and stage exit conditions

### 9.1 Not among the 14 defects — owner decision required to schedule

| id | item | status |
|---|---|---|
| **O1** | Per-request timeouts (5 s/request, 10 s for `/_test/reset`, 10 s for import) | Real requirement rows, but **not** among the Reviewer's 14 and no harness failure is attributed to it. Risk: a naive socket timeout can break `test_fifty_concurrent_requests_produce_no_5xx`. **Do not schedule without owner instruction**; if scheduled, run the concurrency test in the same commit. |
| **O2** | Docker / Tier-3 container verification | **BLOCKED — environment.** `Get-Command docker` → not found. `test_packaging.py` is a stdlib stand-in and must **never** be reported as a build result. Escalate to the owner as an environment problem (install Docker, or designate a host/CI). Until then every report marks Tier-3 **UNRUN**, never "passing". |
| **O3** | Judge-facing packaging: root `README.md` (what it is, build/run/test, one command each), `REQUIREMENTS.md` rows ticked **only** where a command and its output are recorded, `.gitignore` entries for `.env` and `agent_config.yaml` (both untracked and currently would ship on `git add -A`) | Submission hygiene, outside the 14 defects. Schedule **after stage gate B**, before handoff. REV re-runs every command quoted in the README from a clean checkout. |
| **O4** | PBKDF2, email `a@b`, `busy_timeout=10000`, extra `user_id` | **Not changes** (§4). No work item may alter them. |

### 9.2 Stage exit conditions (gates, not work items)

- **Gate A — after WI-01…WI-04 and H1/H2:** `python -m unittest discover -s tests -t .` →
  `errors=0`; SPEC GATE `GATE PASSED` with **red ≤ 3** (only WI-05/07/08/09/10 names still red);
  no **unnamed** failure.
- **Gate B — after WI-05…WI-09 and H3:** full suite `errors=0`, `skipped=0`, gate `red = 0`;
  QA edge-case report attached (boundaries, DST day, swap/rollback, fixture 5xx).
- **Gate C — after WI-10 and H4:** full suite `OK`, gate `red = 0` with the `DECLINED` reversed;
  mutation probes recorded both ways.
- **Gate D — final:** official harness re-run (H5) improves on `28F/92P`; O2 recorded UNRUN or
  executed; O3 complete; `git status --short` clean of anything that must not ship.

---

## 10. Sequence and gates

```
WI-00 ✅ (accepted) ──► WI-01 ──► WI-02 ──► WI-03 ──► WI-04 ──► WI-05 ──► WI-06 ──► WI-07
                                                                              │
                                                                              ├─► WI-08 ──► WI-09 ──► WI-10
                                                                              │
H1..H5 run alongside, never blocking ─────────────────────────────────────────┘
```

Rationale, in the Reviewer's words: WI-01 early because the envelope change cascades through many
callers (the `KeyError: 0`); WI-02 supplies the codes WI-05 depends on; WI-03 fixes precedence
**before** validation paths expand; WI-04 aligns field codes; WI-05 layers hours/grid on top with
grid-before-occupancy; WI-06/WI-07 are format/field additions; WI-08 sets up state correctly;
WI-09 depends on validation and precedence being right; WI-10 last because it touches every
response surface.

**Default: strictly sequential**, all editing `app/main.py`. WI-07 and WI-08 are independent of
each other and may run in parallel **only if** the owner says so; nothing else parallelises.
Each WI is a **separate commit** and stops for review. **No seat starts the next WI without a
fresh owner authorisation.**

---

## 11. Coordination notes and standing rules

### 11.1 In-flight authorisation vs. the new numbering — **needs an owner ruling**

At `2026-10-07T14:29:26Z` (16 seconds **after** the message that commissioned rev. 4.1) the owner
authorised the Implementer to implement **"WI-01 from Architect plan rev. 4.0"** — slot-grid and
opening-hours validation — with instructions to stop before rev-4.0 WI-02.

**Measured state while this revision was written** (`git status --short`):
` M PLAN.md` and ` M stage-1/app/main.py` — `stage-1/app/main.py` is **dirty and uncommitted**,
`90 insertions(+), 33 deletions(-)`, against `HEAD 2c10382`. Its content is:

- a new `_slot_starts(hours, step, duration)` — described in its own docstring as "the single copy
  of the grid arithmetic", also now used by `get_availability` (which was refactored onto it);
- a new `_assert_within_opening_slot(conn, restaurant, starts)` emitting `outside_opening_hours`
  and `not_on_slot_grid`;
- one added call, in `_validate_booking_fields`.

Because `_validate_booking_fields` is reached from **all three** surfaces — create (`main.py:653`
via `_resolve_booking`), PATCH (`main.py:741` via `_resolve_booking`) and moves (`main.py:929`
directly) — **this single change covers create, PATCH and moves at once.**

| rev. 4.0 item | rev. 4.1 item |
|---|---|
| WI-00 (handle) | **WI-00** ✅ done |
| **WI-01** grid + hours on the booking path | **WI-05** in full, **plus** the grid/hours half of **WI-02** — and **only** that half: `Select-String party_exceeds_capacity` is still **0 hits in `app/` and 0 in `tests/`**, and the `NonExistentLocalTime` branch still reports `validation_failed`, so **S1-1's other two codes are untouched** |
| WI-02 (`invalid_local_time`, `party_exceeds_capacity`) | **WI-02** — **not started** |
| WI-03 (`party_size` → 422) | **WI-04** |
| WI-04 (envelopes, order, `ends_at`/`created_at`) | **WI-01** (envelopes) + **WI-06** (body fields) |
| WI-05 (availability `timezone`) | **WI-07** |
| WI-06 (import residual) | **H2** + parts of **WI-08** |
| WI-07 (redaction trigger) | **H1** |
| WI-08 (stale skip) | **H3** |
| WI-09 (timeouts) | **O1** (not scheduled) |
| WI-10 (README/`.gitignore`/checklist) | **O3** |
| WI-11 (container) | **O2** |
| WI-12 (KDF decision) | **O4** — withdrawn, not a change |

**Recommendation to the owner:** let the in-flight work land as **partial progress on rev. 4.1
WI-02 + WI-05**, reviewed against **rev. 4.1's** acceptance evidence for those two items — with
these conditions: (a) it must not be counted as a completed WI; (b) WI-05's acceptance also
requires the grid-before-occupancy precedence ruling (§7, WI-05) and the corrected shipped tests
T3/T4/T5/T6, neither of which is in the diff; (c) WI-02's two missing codes remain open; (d)
WI-03's precedence work must not be skipped or re-broken; (e) `NAMED_DEFECTS` must not change in
this work (§11.2 rule 5). Alternatively the authorisation can be superseded outright and the work
restarted under rev. 4.1. **This is the owner's call, not the Architect's** — until it is ruled,
§10's sequence cannot be followed literally.

### 11.2 `NAMED_DEFECTS` — the rule for rev. 4.1

Rev. 4.0 said "the registry stays at 23". That is too rigid: six of the 14 defects have no shipped
name at all, so a fixed obligation would remain invisible. The rule is now:

1. A name may be **added** only with (a) the `§N:line` quoted in the commit, and (b) a matching
   test method — `NamedDefectsHaveTests` enforces the pair.
2. A name may be **deleted** never — not to go green, not to "clean up".
3. An entry may be **moved out of `DECLINED`** only with a cited spec line (WI-10, T12).
4. An entry may be **added to `DECLINED`** only with a cited spec line and owner visibility.
5. The **owner's instruction to the Implementer — "do not change `NAMED_DEFECTS`" — binds the
   current rev-4.0 WI-01 authorisation.** The registry changes happen in the WI that owns the
   obligation (WI-02, WI-10), under fresh authorisation, not in the in-flight work.

### 11.3 The stash is protected source, not a patch to apply

`stash@{0}` (and `git archive 5a0f67a`) contain a prior agent's candidate implementation of
several S1/S2 items — measured at **267 tests / 25F / 1E**, gate `named 27, red 6` plus 19 unnamed,
with an audit recording items #1–#4 and #7 implemented, #5 partial, #6/#8–#11/#13/#14 untouched and
#12 named-but-red (`expected (409, idempotency_key_reuse), got (400, malformed_request)`).

Rules:
- **Never drop, re-create, or apply it wholesale.** It stays in place until the owner says
  otherwise; the Implementer has already verified it is untouched.
- Every hunk is **reviewed line by line against the specification** and against the WI it belongs
  to, then re-implemented or consciously adopted with evidence. "It's in the stash" is not evidence
  that it is correct — the stash's own numbers were worse than HEAD's.
- The stash is a **cross-check** for §6 and §3, not an input to any gate.

### 11.4 Citation hygiene — two documents, one notation

The same `§N:line` notation is in use for **two different documents**:

| document | in repo? | form | verified example |
|---|---|---|---|
| official Stage-1 specification (`stage-1.md`) | **no** — rulings live on the room record; harness bundle at `C:\Users\linga\Downloads\dark-factory-wearedevs-main\…` | `§N:line` = section N, absolute line of `stage-1.md` | `§6:211` email `a@b`, `§6:224` KDF, `§8:349-350` the four create codes, `§11:459-465` batch rules |
| `REQUIREMENTS.md` | yes, 213 lines | `§N:line` = section N (`## 2` at line 9 … `## 11` at line 182), **absolute line of `REQUIREMENTS.md`** | `§8:126-127` = rows "not on the slot grid" / "outside opening hours" (lines 126-127, inside `## 8` at 95-144) |

They are **not interchangeable** — `§6:211` resolves to the email rule in one and to a DoD row in
the other. The in-flight `app/main.py` docstrings already cite `§8:126-127`, `§4:101`, `§8:117-118`
in the `REQUIREMENTS.md` sense, while the Reviewer's 14 defects cite the specification. Rules:

1. When acting on one of the 14 defects, cite **the specification**, open it, and **quote the line
   in the commit message**. A citation without a quotation is a claim.
2. When the quotation does not match what this plan says the citation means, **the specification
   wins and this plan is corrected.**
3. Any seat writing `§N:line` must name the document it points into.

---

## 12. Acceptance criteria for the stage

1. All 14 defects (§3) closed, each with the Reviewer's exact acceptance evidence (§7) plus the
   quotation of the specification line it implements.
2. `python -m unittest discover -s tests -t .` from `stage-1/` → `errors=0`, `skipped=0`, and
   SPEC GATE `GATE PASSED` with **red = 0**.
3. Official harness re-run (H5) recorded against the §2.3 baseline of `28F/92P`.
4. No protected invariant (§5) broken: every named test in §5 still green after every WI.
5. No already-correct behaviour changed (§4).
6. Every shipped-test defect in §6 corrected with its specification citation in the commit — and
   **no** product change made solely to satisfy a shipped test.
7. `git status --short` clean of anything that must not ship; `git diff -- app/` reviewed by REV
   for every WI.
8. **No seat accepted its own work**: IMP produced, REV reviewed, QA verified, ARC accepted.
9. Each handoff names: exact revision, work completed, remaining risks, checks executed, failures
   observed, evidence paths.
10. Docker Tier-3 either recorded, or explicitly **UNRUN** with the blocker named (O2).

---

## 13. Evidence and handoff rules (standing)

- **Never accept a claim.** Accepted evidence: command output, test output, `git diff`,
  `git rev-parse HEAD`, a committed artifact. Anything else is a claim, not proof.
- **A green run with `Ran 0 tests` is not a pass** — record the test count.
- **No silent skipping.** A skipped obligation whose blocker is green is un-skipped in the same
  stage (H3).
- **No seat accepts its own work**, and no requirement row is ticked by the person who wrote the
  code for it.
- **Reject on defect:** smallest precise failure description + the evidence required to clear it.
- **Re-run, never re-quote:** a number quoted from an earlier message is a claim until re-run.

---

## 14. Known limitations of this revision

- The official specification (`stage-1.md`) is not in this repository; its rulings are on the room
  record. WI-10's contested reading (§7, WI-10) is **adopted from the Reviewer, not independently
  verified by the Architect**, and is flagged as such.
- The harness results (`28F/92P`) stand on the Reviewer's evidence; the Architect has not re-run
  them (the bundle is outside the repo). H5 closes this.
- Docker/Tier-3 is **UNRUN** (no Docker on this host).
- The in-flight rev-4.0 WI-01 authorisation (§11.1) is unresolved and needs an owner ruling before
  the sequence in §10 can be followed literally.
- `REQUIREMENTS.md` remains **0 of 123 rows** ticked until O3.
- The working tree is **dirty**: `stage-1/app/main.py` carries the uncommitted in-flight rev-4.0
  WI-01 work (§11.1) and `PLAN.md` is this document. No commit was made to produce rev. 4.1.
