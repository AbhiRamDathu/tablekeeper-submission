# Tablekeeper Stage 1 — traceability checklist

Derived from `tablekeeper/spec/stage-1.md` (read in full, 472 lines). Every row cites the
section it comes from. Use this as the acceptance contract: the repository ships only *part*
of each stage's tests, so the spec prose — not the visible suite — is what is graded.

Legend: `[ ]` open, `[x]` verified by a runnable check.

## §2 Delivery and deployment

- [ ] `Dockerfile` present and buildable
- [ ] `RUN.md` gives one command that builds and starts with no manual setup
- [ ] Runs with `-e PORT=<port>` plus a port mapping
- [ ] No outbound network required at run time; all assets and dependencies in the image
- [ ] Works within 2 vCPU, 2 GiB, 60 s to healthy, 50 concurrent in flight
- [ ] Per-request timeout 5 s; 10 s for `POST /_test/reset`
- [ ] **No 5xx responses, including under concurrent load**

## §3 Runtime contract

- [ ] Listens on `0.0.0.0`, uses `PORT`, defaults to 8080 (§3.1)
- [ ] `GET /health` → 200 `{"status":"ok"}` within 60 s; non-200 allowed before ready (§3.2)
- [ ] `POST /_test/reset` → 204, unauthenticated, enabled in the delivered image (§3.3)
- [ ] After a reset returns 204, subsequent requests see only that fixture
- [ ] Repeated resets supported
- [ ] `application/json; charset=utf-8` in and out (§3.4)
- [ ] Response timestamps are RFC 3339 **with an explicit offset**, e.g. `2026-09-24T19:00:00+02:00`
- [ ] Unknown request body fields ignored, never an error
- [ ] Unknown query parameters ignored
- [ ] IDs are opaque strings of at most 64 characters — **including IDs in reset fixtures**

## §4 Model

- [ ] Restaurants and tables are supplied through reset only; creation endpoints out of scope
- [ ] `timezone` IANA zone; `slot_minutes`; `reservation_duration_minutes`;
      `cancellation_cutoff_minutes`; per-weekday `opening_hours`; table `capacity`
- [ ] `weekday` one of `mon tue wed thu fri sat sun`
- [ ] `opens`/`closes` are local `HH:MM` 24-hour, `closes` always later than `opens` on the
      same local day; hours never cross midnight
- [ ] Seeded users can log in with the fixture password immediately
- [ ] Fixture `reservations` may seed confirmed bookings: same fields as a create body plus
      `id`, `reference`, `user_id`
- [ ] A booking is **not** rejected solely for starting in the past; cutoff rules still apply

## §5 Errors

- [ ] Every 4xx and 5xx body is `{"error":{"code":...,"message":...}}`
- [ ] 400 `malformed_request` — unparseable body, or a field of the wrong JSON type
- [ ] 400 `missing_idempotency_key` — header absent or empty
- [ ] 401 `unauthenticated` — missing, malformed or unknown bearer token
- [ ] 403 `forbidden`
- [ ] 404 `not_found` — no such resource, **or not visible to this caller**
- [ ] 409 `idempotency_key_reuse` — key used by this caller with a different request body
- [ ] 422 `validation_failed` — missing required field or query param, or a stated rule
      violated with no more specific code
- [ ] Correct type but invalid format or out-of-range value → 422 `validation_failed`
- [ ] Invalid `party_size` (including strings and booleans) → 422 `validation_failed`
- [ ] `starts_at_local` not a bare local `YYYY-MM-DDTHH:MM` → 422 `validation_failed`
- [ ] Integer **query** params are plain decimal digits: `1e9`, `4.0`, `+4` → 422
- [ ] `Idempotency-Key` length 1–255, otherwise 422 `validation_failed`

## §6 Authentication

- [ ] `POST /auth/signup` → 201 `{user_id, display_name, token}`
- [ ] `POST /auth/login` → 200 `{user_id, display_name, token}`
- [ ] Email already registered → 409 `email_taken`
- [ ] Password shorter than 8 characters → 422 `validation_failed`
- [ ] Email not of the form `local@domain` → 422 `validation_failed`
- [ ] Wrong password or unknown email on login → 401 `unauthenticated`
- [ ] Bearer token required on everything except `/health`, `/_test/reset`, signup, login,
      and the three public reads `GET /restaurants`, `GET /restaurants/{id}`, `GET /availability`
- [ ] Tokens do not expire; an account may hold multiple valid tokens and concurrent sessions
- [ ] Passwords hashed with bcrypt, scrypt or Argon2 or equivalent; **no plaintext storage**

## §7 Idempotency

- [ ] Required on `POST /reservations` and `POST /reservation-moves` only
- [ ] Scoped to the authenticated user; two users may reuse the same key string independently
- [ ] Replay = same user, same method, same path, same body
- [ ] Same key and body on a *different* path is **not** a replay and must succeed normally
- [ ] Resolution order: after the body parses as a JSON object and after authentication, but
      **before** endpoint-specific field validation and current-resource checks — so a used key
      with a different body returns 409 even when that body is otherwise invalid
- [ ] Header absent or empty → 400 `missing_idempotency_key`
- [ ] First use → the normal response, **201**
- [ ] Replay → **200**, body identical to the original response as a JSON value
- [ ] Same key, different body → 409 `idempotency_key_reuse`
- [ ] Key reused after the original request failed 4xx is treated as a first use
- [ ] "Same body" means the same JSON value after parsing; key order and whitespace irrelevant
- [ ] Concurrent identical requests on an unused key: exactly one 201, the rest 200 with the
      same body, and the operation takes effect only once
- [ ] A successful replay returns the original response even after the resource changed or was
      cancelled, and makes no further state changes

## §8 API

Public, no token:

- [ ] `GET /restaurants` → `{"restaurants":[{id,name,timezone}]}`
- [ ] `GET /restaurants/{id}` → the restaurant with `slot_minutes`,
      `reservation_duration_minutes`, `cancellation_cutoff_minutes`, `opening_hours`, `tables`
      in the fixture's shape; 404 if unknown
- [ ] `GET /availability?restaurant_id=&date=&party_size=` — all three required, a missing one
      is 422 `validation_failed`; `date` is a local calendar date at the restaurant
- [ ] Response carries `restaurant_id`, `date`, `timezone`, and `slots[]` of
      `{starts_at_local, starts_at, available_table_ids}`
- [ ] `starts_at_local` is the full `YYYY-MM-DDTHH:MM` and goes into `POST /reservations`
      unchanged
- [ ] A slot appears for every `slot_minutes` step from `opens` while
      `slot + reservation_duration_minutes <= closes`
- [ ] `available_table_ids` lists tables of that restaurant with `capacity >= party_size` and
      no overlapping confirmed reservation, **in fixture order**
- [ ] A slot with no available table still appears, with an empty list
- [ ] A closed day returns `"slots": []`

Authenticated:

- [ ] `POST /reservations` — requires `Idempotency-Key`
- [ ] Body: `restaurant_id`, `table_id`, `starts_at_local`, `party_size`
- [ ] `starts_at_local` is wall-clock at the restaurant, no offset, no `Z`, resolved against
      the restaurant's `timezone`
- [ ] 201 body: `reservation_id`, `reference`, `restaurant_id`, `table_id`, `party_size`,
      `status`, `starts_at_local`, `starts_at`, `ends_at`, `created_at`
- [ ] `reference` is 6–12 characters of `A-Z0-9`, unique across all reservations, never changes
- [ ] Table taken for an overlapping interval → 409 `table_unavailable`
- [ ] `starts_at_local` not on the slot grid → 422 `not_on_slot_grid`
- [ ] Outside opening hours, or would end after `closes` → 422 `outside_opening_hours`
- [ ] `party_size` exceeds the table's `capacity` → 422 `party_exceeds_capacity`
- [ ] `party_size` below 1 or not an integer → 422 `validation_failed`
- [ ] Non-existent local time → 422 `invalid_local_time`
- [ ] Unknown restaurant, unknown table, or table belonging to another restaurant →
      404 `not_found`
- [ ] `GET /reservations` — caller's reservations, `starts_at` **descending**, confirmed and
      cancelled alike; `{"reservations":[...]}`; empty is `{"reservations":[]}`
- [ ] `GET /reservations/{reference}` — 404 if not the caller's; do not leak others' bookings
- [ ] `POST /reservations/{reference}/cancel` → 200 with current state; frees the table
      immediately so the next `GET /availability` offers the slot again; already cancelled is
      200 not an error; within cutoff of start → 409 `cutoff_passed`; not the caller's → 404
- [ ] `PATCH /reservations/{reference}` — any subset of `table_id`, `starts_at_local`,
      `party_size`; no idempotency key required; same validation as create; same cutoff rule
      measured against the **current** start; cancelled → 409 `reservation_cancelled`; releases
      the old slot and reserves the new one together; a failed amendment leaves the original
      booking and its occupancy unchanged; `reference` and `reservation_id` survive

## §9 Time and DST

- [ ] Local times follow the restaurant's `timezone` including DST transitions
- [ ] **Spring forward:** times in the skipped hour do not exist, never appear in availability,
      and booking one is 422 `invalid_local_time`
- [ ] **Fall back:** times in the repeated hour occur twice and **always resolve to the first
      occurrence**, the one before the clocks change; the slot appears once in availability and
      the second occurrence is not bookable
- [ ] `reservation_duration_minutes` is **absolute time, not wall-clock**: a 90-minute booking
      at 01:30 on a fall-back night ends 90 real minutes later and its local `ends_at` reads
      02:00, not 03:00
- [ ] Transitions handled: Europe/Berlin 2026-03-29 02:00→03:00 and 2026-10-25 03:00→02:00;
      America/New_York 2026-03-08 02:00→03:00 and 2026-11-01 02:00→01:00
- [ ] Offsets follow the IANA rules for the specified zone and date

## §10 Export and import

- [ ] `GET /_test/export` and `POST /_test/import` unauthenticated, 10 s timeout
- [ ] Export → 200 with `track: "tablekeeper"`, `format_version: 1`, and `state` as an
      implementation-defined JSON object, opaque to the caller and accepted unchanged by import
- [ ] Import takes that entire object, atomically replaces state, returns 204
- [ ] Import is replacement, not merge; repeating it restores the exported state without
      duplicating anything
- [ ] No dependency on the source process, files, volume, port or network address
- [ ] Invalid JSON follows §5; missing fields, wrong track/version or invalid state give 422
      `validation_failed` **without changing the destination**
- [ ] Export is an atomic read-only snapshot; subsequent source writes do not change it
- [ ] Preserved: accounts and hashed-password login, existing bearer tokens, fixture
      configuration, reservations, references, all completed idempotent request bodies and
      original responses, and successful batch receipts
- [ ] Identities, statuses and timestamps are **not** regenerated
- [ ] Failed request keys remain reusable after import
- [ ] Existing receipts, references, tokens and retries remain valid after import —
      **replacing state with a fresh fixture does not satisfy this**
- [ ] Import removes all previous destination data and credentials
- [ ] Reset still clears all state, including imported state

## §11 Atomic reservation moves

- [ ] `POST /reservation-moves` requires authentication and an idempotency key
- [ ] Body `{"moves":[{"reference":...,"table_id":...}, ...]}`
- [ ] 1–8 objects with distinct string references; invalid shape or duplicate references →
      422 `validation_failed`
- [ ] Every booking belongs to the caller and to the same restaurant
- [ ] Unknown or another owner's reference → 404 `not_found`
- [ ] Different restaurants → 422 `validation_failed`
- [ ] No token → 401
- [ ] Each item accepts the ordinary PATCH fields `table_id`, `starts_at_local`, `party_size`;
      omitted fields retain current values; unknown fields ignored
- [ ] Identity, owner and creation time never change
- [ ] Cancelled bookings → 409 `reservation_cancelled`
- [ ] Each booking's existing cutoff applies
- [ ] Non-occupancy errors use ordinary amendment codes and take precedence **in input order**,
      with cutoff errors preceding other changes for that booking
- [ ] Overlap among resulting bookings or with an unlisted booking → 409 `table_unavailable`
- [ ] Unchanged listed bookings retain their occupancy
- [ ] Either every move commits or nothing changes: occupancy, reservation records and retry keys
- [ ] Success → 201 `{"reservations":[...]}` in input order, including unchanged items
- [ ] Replays → 200 with that original response, even after amendments or cancellations
- [ ] No-op moves retain all existing values
- [ ] Export/import preserves successful batch receipts and the resulting bookings
- [ ] No batch UI required

## Definition of done for Stage 1

1. Every row above checked, each backed by a command and its observed output.
2. The image builds from the submitted `Dockerfile` and passes the harness's HTTP checks.
3. No 5xx under 50 concurrent in flight.
4. Evidence captured as command output, not as assertion.