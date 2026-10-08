# Independent verification — Tablekeeper Stage 1

Reviewer: `@velishalalingaraju/reviewer`. Machine and paths as in the room plan.

## What this document is, and what it is not

Every claim below comes from a command I ran myself, with its output pasted. Where I could
only read code and not run it, the finding says so in those words. **No
`runs\<fresh>\stage-1.counts.json` is cited anywhere in this file, because no harness run was
possible on this machine**, verified by me:

```
docker  -> "The term 'docker' is not recognized as the name of a cmdlet..."   (Tier 3 impossible)
python -c "import pytest" -> ModuleNotFoundError: No module named 'pytest'    (Tier 2 impossible)
python -c "import httpx"  -> ModuleNotFoundError: No module named 'httpx'
python --version -> Python 3.10.11
```

So the harness's own accounting — the only evidence that measures *the spec's checks* rather
than *my reading of the spec* — does not exist yet. Every §7, §10 and §11 verdict below is
**provisional on that**: it rests on probes I wrote, not on the graded suite. Once the owner
installs, I re-run `python -m harness run --track tablekeeper --repo … --stage 1 --mode
isolated --out runs/<fresh>` and re-issue this file with the counts attached.

Subject of this review: commit `98a359a`, working tree clean
(`git -C …\tablekeeper-submission status --short` → empty), everything on `main`.

Note for the record: there is no `unit-0-tz` branch and no `stage-1/docs/` folder. All nine
units' work arrived as one commit on `main`. That is a process observation, not a finding
against any individual.

---

## Checkpoint 1 — Unit 0 (`app/tz.py`, `app/intervals.py`)

### §9 DST resolution — **CONFIRMED for local-time resolution, REFUTED for duration**

Command:

```
$env:PYTHONPATH="C:\Users\linga\Jam\tablekeeper-submission\stage-1"
python %TEMP%\opencode\unit0_checkpoint.py
```

All four transitions named in `stage-1.md` §9, and both directions:

| Zone | Date | Transition | Observed |
|---|---|---|---|
| Europe/Berlin | 2026-03-29 | spring 02:00→03:00 | 02:00/02:30/02:59 **all rejected** `NonExistentLocalTime`; 01:30 → `+01:00` |
| Europe/Berlin | 2026-10-25 | fall 03:00→02:00 | 02:00/02:30/02:59 `is_ambiguous=True`, `resolve` → `+02:00` **first occurrence**, `fold=0` |
| America/New_York | 2026-03-08 | spring 02:00→03:00 | 02:00/02:30/02:59 **all rejected**; 01:30 → `-05:00` |
| America/New_York | 2026-11-01 | fall 02:00→01:00 | 01:00/01:30/01:59 `is_ambiguous=True`, `resolve` → `-04:00` **first occurrence**, `fold=0` |

Skipped hour never resolves, repeated hour always resolves to the pre-change offset, in both
zones. Offsets follow the IANA database for the zone and date. Also confirmed: the repeated
hour is offered once in availability and the spring-forward day omits the skipped hour, at the
service level (§9 prose), observed via a live probe:

```
repeated hour offered once: 02:00 x1, 02:30 x1, slots=45
spring-forward day omits the skipped hour: 02:00 in slots=False, 02:30 in slots=False
Berlin 18:00 -> 2026-12-01T17:00:00+00:00 ; NY 12:00 -> 2026-12-01T17:00:00+00:00 (same instant)
```

**That is the whole of what §9 gets right. `reservation_duration_minutes` is not absolute
time, and §9 says it must be.** See the next block.

### §1 adjacency — **CONFIRMED as a pure function, but the invariant does not hold on transition days**

Command: the same script, `=== section 1: half-open occupancy ===` block. 90-minute booking,
Europe/Berlin:

```
19:00 then 20:30 (the spec's adjacency example): overlaps=False expected=False OK
19:00 then 20:00 (overlaps):                    overlaps=True  expected=True  OK
19:00 then 20:29 (one minute early):            overlaps=True  expected=True  OK
19:00 then 19:00 (identical):                   overlaps=True  expected=True  OK
naive refused: intervals compare absolute times; a naive datetime was given
```

Half-open semantics are right, and the service enforces it under contention — 50 racing
distinct keys for one table and slot:

```
50 racing distinct keys for one table in 2.2s -> {201: 1, 409: 49}
50 concurrent mixed reads -> {200: 50}
```

The spec's headline invariant ("two `confirmed` reservations must never occupy the same table
at overlapping times, **including during concurrent requests**") holds on ordinary evenings.

### §9 REFUTED — `slot_end` is wall-clock arithmetic, not absolute time

`app/intervals.py:35-41`, whose docstring says "Added in absolute time, not by adding minutes
to the wall clock":

```python
def slot_end(start: dt.datetime, duration_minutes: int) -> dt.datetime:
    return start + dt.timedelta(minutes=duration_minutes)
```

In Python, `timedelta` added to an *aware* datetime advances the naive (wall) fields and keeps
the original `tzinfo`; the offset is only recomputed on formatting. So the returned pair spans
the transition while the absolute instant is wrong by one hour.

Command and observed output:

```
$env:PYTHONPATH="…\stage-1"; python -c "<see below>"

start                2026-10-25T01:30:00+02:00  UTC=2026-10-24 23:30:00+00:00
truth (UTC + 90min)  2026-10-25T02:00:00+01:00  UTC=2026-10-25 01:00:00+00:00
slot_end(start, 90)  2026-10-25T03:00:00+01:00  UTC=2026-10-25 02:00:00+00:00
absolute-time correct: False
```

This is the spec's own worked example, failing as the spec names it. §9: "A 90-minute
reservation starting at 01:30 on a fall-back night ends 90 real minutes later, and its local
`ends_at` will read **02:00, not 03:00**." The function returns local `03:00`.

Off by one hour on every transition, correct on ordinary evenings — which is why local
testing passes:

```
spring forward  truth UTC+90 = 2026-03-29T04:00:00+02:00   slot_end = 2026-03-29T03:00:00+02:00   match=False
New York fall   truth UTC+90 = 2026-11-01T01:00:00-05:00   slot_end = 2026-11-01T02:00:00-05:00   match=False
ordinary evening truth=2026-10-09T20:30:00+02:00  slot_end=2026-10-09T20:30:00+02:00  match=True
```

**Consequence: §1's invariant is violated, in both directions.** Proven, not inferred:

```
section 1 invariant breach: two confirmed bookings, one table, spring forward
  A 01:30 CEST truth  [2026-03-29T00:30:00+00:00, 2026-03-29T02:00:00+00:00) UTC
  A 01:30 CEST model  [2026-03-29T00:30:00+00:00, 2026-03-29T01:00:00+00:00) UTC
  B 03:30 CEST starts 2026-03-29T01:30:00+00:00 UTC
  truth  A ends after B starts: True
  service overlaps(A,B) = False  <-- must be True

and the mirror image on fall back: a slot that is free is reported busy
  P 02:15 CEST truth  [2026-10-25T00:15:00+00:00, 2026-10-25T01:45:00+00:00) UTC
  Q 03:15 CET  starts 2026-10-25T02:15:00+00:00 UTC
  truth  P ends before Q starts: True
  service overlaps(P,Q) = True  <-- must be False
```

Two confirmed reservations on one table at overlapping instants: an admitted double-booking
on a spring-forward day, and a wrongly-withheld table on a fall-back day. Both flow from the
same one-line defect, and both reach the HTTP surface through `app/main.py:216`, `:225` and
`:394`.

Fix:

```python
def slot_end(start: dt.datetime, duration_minutes: int) -> dt.datetime:
    end = start.astimezone(dt.timezone.utc) + dt.timedelta(minutes=duration_minutes)
    return end.astimezone(start.tzinfo)
```

Keep `start.tzinfo` so the value still renders in the restaurant's zone.

**Correction to my own earlier statement.** In my first report I wrote that the tz core was
right and that "`slot_end` adds to an aware datetime, so duration is absolute." That was
wrong. I read the docstring's claim instead of testing the function against UTC, which is
precisely the substitution the review contract forbids. The claim is withdrawn; the block
above is the evidence.

### Why the project's own green gate misses this

```
$ cd …\stage-1; python -m unittest discover -s tests -t . -v
-> Ran 70 tests in 53.015s / OK
```

`tests/test_tz.py:133-134` asserts `(self.end - self.start) == dt.timedelta(minutes=90)`.
Observed for the fall-back start that is one hour wrong:

```
  observed: 1:30:00  -> assertion True  PASSES
  absolute instants: 2026-10-25 01:00:00+00:00 != 2026-10-25 02:00:00+00:00
```

Subtracting two aware datetimes that share one `tzinfo` is taken on the naive fields, so a
wall-clock error cancels out and the assertion cannot see it. The test is not weak — it is
incapable of failing for the property it names.

Worse, the whole class is pinned to a date where the bug cannot exist.
`tests/test_tz.py:129-131`, `HalfOpenIntervals.setUp`, starts every one of its six cases at
`2026-06-01T19:00` Berlin — an ordinary June evening:

```
129:    def setUp(self):
130:        self.start = resolve(parse_local("2026-06-01T19:00"), BERLIN)
131:        self.end = slot_end(self.start, 90)
133:    def test_a_booking_ends_after_ninety_minutes(self):
134:        self.assertEqual((self.end - self.start), dt.timedelta(minutes=90))
136:    def test_a_booking_starting_exactly_at_the_end_does_not_conflict(self):
137:        """The §1 adjacency case: 19:00-20:30 and a booking starting 20:30 are back to back."""
```

So line 137 names the §1 adjacency case in its own docstring and still cannot catch the §1
invariant breach, because on 2026-06-01 wall-clock and absolute arithmetic agree. The
service-level neighbour `tests/test_service.py:224-232` has the same shape — it books
`at="19:00"` and asserts `20:30` is free, again off-transition.

Any replacement must assert an absolute property for a start inside every one of the four
transitions, e.g.

```python
for zone, date in (("Europe/Berlin", "2026-10-25T01:30"),
                   ("America/New_York", "2026-11-01T00:30"),
                   ("Europe/Berlin", "2026-03-29T01:30"),
                   ("America/New_York", "2026-03-08T01:30")):
    z = ZoneInfo(zone)
    s = resolve(parse_local(date), z)
    self.assertEqual(slot_end(s, 90).astimezone(dt.timezone.utc),
                     s.astimezone(dt.timezone.utc) + dt.timedelta(minutes=90))
```

and the §9 prose example directly: a 90-minute booking from `2026-10-25T01:30` Berlin must
carry a local `ends_at` of `02:00`, never `03:00`.

#### Read this before applying the fix: the correct code makes that assertion go red

`test_tz.py:134` compares two aware datetimes sharing a `tzinfo`, so it measures the **wall
clock** difference, not elapsed time. Across a transition those diverge — and the divergence
runs *against* the correct implementation:

```
Would my fix satisfy test_tz.py:134, (self.end - self.start) == timedelta(minutes=90)?
  2026-06-01  (no transition, what the suite uses)
    current slot_end: diff=1:30:00  suite assertion -> True
    fixed   slot_end: diff=1:30:00  suite assertion -> True
    fixed ends_at local = 20:30, absolute correct = True
  2026-10-25  (fall back)
    current slot_end: diff=1:30:00  suite assertion -> True
    fixed   slot_end: diff=0:30:00  suite assertion -> False
    fixed ends_at local = 02:00, absolute correct = True
  2026-03-29  (spring forward)
    current slot_end: diff=1:30:00  suite assertion -> True
    fixed   slot_end: diff=2:30:00  suite assertion -> False
    fixed ends_at local = 04:00, absolute correct = True
```

`diff=1:30:00` on **every** date for the current code is the defect in one line: a function
that always reports a 90-minute wall-clock span cannot be doing absolute arithmetic. A correct
`slot_end` reports `0:30` across a fall-back hour and `2:30` across a spring-forward hour,
because the local clock genuinely moves.

So `test_tz.py:134` is not merely blind — it is **backwards**. Fix `slot_end` and it goes red,
which is the expected and correct outcome; do not soften `slot_end` to keep it green. Change
the assertion to compare in UTC, which is the property §9 actually specifies. The suite as
written currently prices the bug in.

### stdlib `zoneinfo`, not a hand-rolled offset table — **CONFIRMED**

```
$ Select-String -Path app\tz.py,app\intervals.py -Pattern "timedelta\(hours|[+-]0?[12]:00|offset\s*=|ZoneInfo|zoneinfo|gettz|pytz|dateutil"
tz.py:10  (docstring)  `zoneinfo` already encodes both facts, so we read them off it rather than maintaining an offset
tz.py:23  from zoneinfo import ZoneInfo
tz.py:65/77/82/89  ZoneInfo used as the type
```

No `timedelta(hours=…)`, no literal offsets, no third-party time library. `tz.py:65-74`
discriminates a gap from a repeat by a UTC round trip rather than by comparing `fold=0` and
`fold=1` offsets — which is the right discriminator, and its comment at `tz.py:69-71` is
correct about why: PEP 495 gives a spring-forward gap two different offsets too, so an offset
comparison would report every gap as ambiguous. I checked that reasoning against both zones
and it holds.

---

## Per-section status at commit `98a359a`

| § | Status | Basis |
|---|---|---|
| 1 half-open occupancy | **CONFIRMED** on ordinary evenings; **REFUTED** across a DST transition (`slot_end`) | run, above |
| 1 no duplicate/partial on retry | **CONFIRMED** (create path) | 50-way race → `{201:1, 409:49}` |
| 2 delivery | **UNVERIFIABLE** — docker absent. **One blocker**: `.dockerignore:1` is `app/` while `Dockerfile:10` is `COPY app ./app`, so the image cannot build | read-only |
| 3.1–3.2 listen/health | **CONFIRMED** | `health -> 200`; binds `0.0.0.0:$PORT` (`main.py:539`) |
| 3.3 reset | **REFUTED** | 500 on two spec-shaped fixtures; no fixture validation; `Content-Type` lacks `charset=utf-8` |
| 4 model, fixtures | **REFUTED** | §4 seeded reservation 500s; restaurant missing a field 500s |
| 5 errors | **REFUTED** | 5 of the endpoint codes wrong or absent |
| 6 auth | **REFUTED** | `display_name` absent from signup and login responses |
| 7 idempotency | **PARTLY CONFIRMED, one refutation** | ordering, replay, reuse-after-4xx and the 20-way burst all correct; the key is not scoped by path |
| 8 API | **REFUTED** | list shape, ordering, `ends_at`/`created_at`, UTC rendering, five error codes |
| 9 time and DST | **REFUTED** on duration; resolution rules confirmed | `slot_end`, above |
| 10 export/import | **NOT IMPLEMENTED** — 404. No evidence obtainable | live probe |
| 11 moves | **NOT IMPLEMENTED** — 404. No evidence obtainable | live probe |

### Blocking findings, with evidence

Every one of these I ran or read directly. Commands are the probe scripts in
`%TEMP%\opencode\`; the service was started with
`python -m app.main` on `127.0.0.1:8099` with a fresh `TABLEKEEPER_DB` per run.

**B1 — `slot_end` is wall-clock, not absolute.** `app/intervals.py:41`. Breaks §9's duration
rule and §1's overlap invariant on all four transitions. Command and output above. Fix given
above.

**B2 — `stage-1/.dockerignore:1` is `app/`, `stage-1/Dockerfile:10` is `COPY app ./app`.**
Docker reads the context root's `.dockerignore`, so the source is stripped and the build fails.
Grading is `--mode isolated`, which builds the image. **Read-verified only** — I could not run
`docker build`, docker is absent, verified above. Treat as blocking until
`docker build -t tk-s1 .` exits 0.

**B3 — reset 500s when two restaurants share a table id.** `app/store.py:47` makes table ids
globally unique; the spec does not. `test_retries_time_input.py:76-86` builds `r_berlin` and
`r_ny` both with `t_1..t_3`. Observed: `reset … -> 500 internal_error`. That is the entire
DST surface of the graded suite. Fix: composite key `(restaurant_id, id)` — and **do not
rewrite ids**, since §8 returns them verbatim and `test_sample.py:84` asserts `["t_2","t_3"]`.

**B4 — reset 500s on the §4 seeded-reservation shape.** `app/store.py:174` reads
`reservation["starts_at_utc"]`; §4 supplies `starts_at_local`. Observed 500.
`test_seeded_state.py:16-22` sends exactly the spec's shape. Fix: resolve
`starts_at_local` through `tz.resolve` against the restaurant's zone; 422 if it does not exist.

**B5 — reservation responses omit `ends_at` and `created_at`.** `app/main.py:416-427`.
Observed 201 keys: `['party_size','reference','reservation_id','restaurant_id','starts_at',
'starts_at_local','status','table_id','user_id']`. §8 lists both. Note this is also what hides
`test_duration_is_absolute_time_not_wall_clock` from reaching B1.

**B6 — `starts_at` is rendered in UTC on create, in local time in availability.**
`app/main.py:424` vs `app/main.py:234`. Same slot, two strings:
`availability 2026-10-09T19:00:00+02:00` / `created 2026-10-09T17:00:00+00:00`. §8's example
and §9's offsets rule require the restaurant's zone. Fix: render every instant in the
restaurant's zone.

**B7 — `GET /reservations` returns a bare list, ascending.** `app/main.py:253` returns a list,
not `{"reservations":[…]}`; `app/main.py:248` orders by `created_at, reference`, not `starts_at`
descending. Observed: `shape=list`, `descending=False`.

**B8 — `party_size` of the wrong JSON type answers 400.** `app/main.py:307-308`. §5's
endpoint bullet and `test_retries_time_input.py:173-177` require 422 `validation_failed`.
Observed: `"4" → 400 malformed_request`, `1.5 → 400`. (`party_size 0` is already correct at
422 — the range path is right.)

**B9 — unknown `table_id` answers 422.** `app/main.py:368-369`; §8 requires 404 `not_found`.
Observed 422.

**B10 — `not_on_slot_grid`, `outside_opening_hours`, `party_exceeds_capacity` do not exist in
`app/`.** Observed: 19:15 → **201**, 17:00 → **201**, 22:00 → **201**, over-capacity → 422
`validation_failed`. §8's table requires 422 for each. Also reaches PATCH and moves, which
inherit create's validation.

**B11 — a skipped local time answers `validation_failed`, not `invalid_local_time`.**
`app/main.py:383-384`. Observed for Berlin 2026-03-29T02:30 and New York 2026-03-08T02:30.

**B12 — reset does no fixture validation.** Observed: 65-character user id → 204 (wants 422,
§3.4); `reference: "lower01"` → 500 (wants 422); restaurant missing
`reservation_duration_minutes` → 500; `closes == opens` → 204 with zero slots.

**B13 — signup and login omit `display_name`.** `app/auth.py:102`, `app/auth.py:130`. Observed
keys `['email','token','user_id']`. §6: `{user_id, display_name, token}`.

**B14 — the idempotency key is not scoped by path.** `app/store.py:77`
`PRIMARY KEY (key, user_id)`; `app/main.py:322-325` looks up by `(key, user_id)` only. §7:
the same key with the same body on a *different* path is not a replay and must succeed
normally. Both key-requiring endpoints are write paths, so the first use on either poisons the
other. Not demonstrable until `/reservation-moves` exists — **read-verified only**. Fix: match
on (user, key, method, path) before comparing the body hash.

### Risks and gaps

- **[Risk]** `PATCH` → **501 with an HTML body** (no `do_PATCH`). §5 forbids 5xx and requires
  the error envelope on every 4xx/5xx. Add `do_PATCH`/`do_DELETE` and override `send_error`.
- **[Risk]** `busy_timeout=10000` (`app/store.py:94`, `:98`) exceeds the harness's 5 s
  per-request timeout (`harness/http.py:13`). Under a write burst the server waits longer than
  its client will wait. Keep the busy timeout under 5 s and map a lock failure to a retry or a
  4xx, never a 500.
- **[Risk]** `Content-Type: application/json` without `charset=utf-8` (`app/main.py:518`);
  §3.4 names `application/json; charset=utf-8`.
- **[Risk]** availability omits `timezone` (`app/main.py:192-195`, `:239-240`); §8 says the
  response carries `restaurant_id`, `date`, `timezone`, `slots[]`. Observed keys on open *and*
  closed days: `['date','party_size','restaurant_id','slots']`.
- **[Risk]** `_EMAIL_RE` (`app/auth.py:23`) requires a dot in the domain; signup `x@y` → 422.
  §6 says `local@domain`.
- **[Gap]** `auth.py:43-60` — PBKDF2-HMAC-SHA256, 120k rounds, per-user salt, `compare_digest`.
  No plaintext at rest: confirmed by reading. Defensible under §6's "or an equivalent", but
  `hashlib.scrypt` is stdlib *and* named in the spec, so it is cheaper to defend and cheaper
  under 50 concurrent logins.
- **[Gap]** duplicate `post_reservation` at `app/main.py:256` and `app/main.py:293`; the first
  is shadowed dead code and would return `None` → 500.
- **[Gap]** `_new_reference` (`app/main.py:412`) never retries on collision, so a primary-key
  collision surfaces as a 500 rather than a retry. §8 wants uniqueness across all reservations.
- **[Gap]** no fixture validation of `opening_hours` (`closes` after `opens`, `weekday` names,
  positive `slot_minutes` / `reservation_duration_minutes`).
- **[Architecture — mine, and it is a mandate]** the overlap check belongs in the one place
  every write path funnels through. `_create_reservation` (`app/main.py:387-395`) inside the
  caller's `BEGIN IMMEDIATE` is that place and it is currently correct in structure. §11's
  moves must go through the same door rather than re-implementing overlap detection; if moves
  need their own copy, that is the signal the door is in the wrong layer.

### §7 — the ordering rule, measured rather than read

All five orderings from §7, each on clean state, live:

```
§7 first use -> 201
§7 replay -> 200 identical_body=True
§7 same key different body -> 409 idempotency_key_reuse
§7 used key + invalid body (party_size 0) -> 409 idempotency_key_reuse   (spec: before field validation)
§7 used key + unknown table -> 409 idempotency_key_reuse                 (spec: before resource checks)
§7 key reusable after a 4xx failure -> failed 422/validation_failed, then 201
§7 20 concurrent identical requests, one unused key -> {200: 19, 201: 1} distinct_bodies=1
```

This is the rule the brief warned about, and it holds. Late binding would have shown up as
422/404 on the two "used key + invalid" lines. It does not. The one §7 defect is B14, which is
about path scoping and is invisible until moves exist.

### §10 and §11 — no evidence obtainable

`GET /_test/export`, `POST /_test/import` and `POST /reservation-moves` all answer 404 "no
route". Nothing can be confirmed or refuted until those land. The §10 requirement I will hold
them to, restated from `REQUIREMENTS.md:178` so it is not lost: **a round trip against a fresh
reset does not satisfy §10.** It must be against a *changed* destination, and it must preserve
accounts and hashed-password login, existing bearer tokens, references, completed idempotent
request bodies and their original responses, successful batch receipts, and identities,
statuses and timestamps unregenerated — while failed keys stay reusable. For §11: a rolled-back
batch must leave its retry keys reusable, which means the occupancy write, the reservation
records and the idempotency record have to share one transaction boundary. That is the same
boundary argument that makes the current create path correct, and it is the thing to check
first when moves land.

---

## Ambiguities I will not decide locally

`README` routes spec ambiguities to the BAND Discord, where the answers are public. Raising,
not resolving:

1. §7 says the key resolves *before* "endpoint-specific field validation"; §5 says
   endpoint-specific rules take precedence for a wrong-typed `party_size`. With a **used** key
   and `{"party_size": "4"}` — 409 or 422? The current code answers 400, which is wrong under
   either reading. My reading is 409 (the body still parses as an object, and §7 states an
   ordering), but this is the Discord's to settle.
2. §9's "the second occurrence is not bookable": does booking the repeated hour's wall time
   always land on the first occurrence (current behaviour), or must the second instant be
   unselectable?
3. §5's 403 `forbidden` has no Stage 1 endpoint that uses it; every cross-owner case is 404.
   Confirm it is unused this stage.
4. §8's "the restaurant … in the fixture's shape" — must `opening_hours` come back in fixture
   order? `app/main.py:127` sorts by weekday; the visible test compares counts only.
5. §10 — confirm the judges do not introspect `state`. An opaque blob satisfies
   "implementation-defined".