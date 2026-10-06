"""SQLite store for Tablekeeper Stage 1.

One file-backed database, WAL journaling, and `BEGIN IMMEDIATE` for every write. Both are load
bearing rather than decorative:

* WAL lets the HTTP layer's reader threads run without blocking the writer, which is what keeps 50
  concurrent requests off each other's toes.
* `BEGIN IMMEDIATE` takes the write lock up front, so two writers cannot both read "no conflict",
  both decide to proceed, and both commit. §7 and §11 both depend on that being impossible rather
  than unlikely.

The connection is opened per operation instead of shared. `sqlite3` connections are not safe to use
from several threads at once, and the alternative — one shared connection plus a mutex — would
serialise reads behind the writer and defeat the point of WAL.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import pathlib
import re
import sqlite3
from typing import Iterator
from zoneinfo import ZoneInfo

from .tz import parse_local, resolve

__all__ = ["database_path", "connect", "transaction", "reset_database", "export_state",
           "import_state", "EXPORT_TRACK", "EXPORT_FORMAT_VERSION", "STATE_SCHEMA",
           "SCHEMA_TABLES", "InvalidFixture", "InvalidState"]

#: §3: "IDs are opaque strings of at most 64 characters -- **including IDs in reset fixtures**".
#: A fixture is the only way restaurants, tables and users come into existence, so a 65-character
#: id admitted here is a 65-character id everywhere else, and §3 states the bound without carving
#: out the seeding path.
MAX_ID_LENGTH = 64

#: §4: "`weekday` one of `mon tue wed thu fri sat sun`", and "`opens`/`closes` are local `HH:MM`
#: 24-hour". The hour bound is not decoration either: "24-hour" excludes 24:00 itself.
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

# Kept apart from the rest of the schema because the migration below has to reissue exactly this
# statement. A second hand-written copy of the DDL would be free to drift from the table every fresh
# database gets, and the drift would only show up in an upgraded one.
IDEMPOTENCY_DDL = """
CREATE TABLE IF NOT EXISTS idempotency (
    key           TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    scope         TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    PRIMARY KEY (key, user_id, scope)
);
"""

# Kept apart for the same reason, and it is the one DDL whose *shape* is a requirement rather than a
# storage detail. §4 constrains a table id by nothing: not by length, not by format, and not by
# belonging to one restaurant only, so `t_2` is a legal id for every restaurant in the model. A
# global `id TEXT PRIMARY KEY` says otherwise, and a fixture the specification permits -- two
# restaurants both owning `t_1..t_3` -- dies at reset on a uniqueness constraint.
#
# The composite key is what `available_table_ids` being "tables *of that restaurant*" (§8) is
# measured against, so it has to exist before any occupancy question across two restaurants can be
# asked at all.
# `ordinal` is the position of the row in its parent's *fixture* array, and it is the only ordering
# §8:112 can be read against: it says "in fixture order" in as many words. Sorting by `id` answers a
# different question -- alphabetical -- and on a fixture whose ids happen to be `t_1,t_2,t_3` the two
# agree, which is why an unmet requirement can sit behind a green suite.
#
# It is per parent, not global: `ordinal` restarts at 0 for each restaurant, because the arrays are
# nested and a table's position among its own restaurant's tables is the thing §8:112 means. The
# unique index is on the pair so two tables of one restaurant cannot claim the same position; it is
# NOT on `ordinal` alone, which would forbid the same position under two different restaurants.
#
# `ORDER BY rowid` is the cheaper-looking answer and is deliberately not used. `tables` has a
# composite primary key and no INTEGER PRIMARY KEY, which is exactly the case where SQLite documents
# that VACUUM may renumber ROWIDs -- so rowid order is an accident of insertion that a later
# maintenance operation can silently invalidate. An ordinal written at insert is a contract; a rowid
# is a side effect.
TABLES_DDL = """
CREATE TABLE IF NOT EXISTS tables (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
    id           TEXT NOT NULL,
    label        TEXT NOT NULL,
    capacity     INTEGER NOT NULL,
    ordinal      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (restaurant_id, id)
);
"""

# `ordinal` is the same contract as `tables.ordinal`, for the same phrase of the specification:
# §8:101 returns `opening_hours` and `tables` "in the fixture's shape", and the room reads that as
# fixture *order* -- `opening_hours_in_fixture_order` and `table_ids_are_returned_in_fixture_order`
# are two named defects off one clause. Reading it for `tables` and not for `opening_hours` would be
# an arbitrary line, and the shipped red test for the second one exists precisely to say so.
#
# The justification is not "rowid might renumber". Measured on this host, SQLite 3.40.1 did *not*
# renumber rowids across a VACUUM for either shape, so that argument, though it is what SQLite
# documents, did not reproduce and is not what this column is for. The measured reason is stronger
# and simpler: with no `ORDER BY` at all, the returned order is *insertion sequence*, which equals
# fixture order only while every writer happens to insert in array order. Deleting one row and
# inserting it again moves it to the end for good, with or without a VACUUM, and `POST /_test/import`
# (§10) is a second writer that does not exist yet and would have to get this right by accident. A
# column written at insert is a contract; a rowid is a side effect.
OPENING_HOURS_DDL = """
CREATE TABLE IF NOT EXISTS opening_hours (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
    weekday       TEXT NOT NULL,
    opens         TEXT NOT NULL,
    closes        TEXT NOT NULL,
    ordinal       INTEGER NOT NULL DEFAULT 0
);
"""

#: Applied after the ordinal migrations, never before: both indexes name a column that a
#: pre-ordinal database does not have. See `ensure_schema`.
#:
#: `tables_by_ordinal` is UNIQUE and `opening_hours_by_ordinal` deliberately is not, and the
#: difference is not an oversight. `tables` is keyed `(restaurant_id, id)`, so two rows of one
#: restaurant cannot be identical and every position among them is unambiguous -- a unique index can
#: only ever be satisfied. `opening_hours` has **no primary key at all**, so a byte-identical
#: duplicate row is representable, and a unique index would make `ensure_schema` raise `IntegrityError`
#: on start for such a database: turning a harmless duplicate into a service that will not boot.
#: Two indistinguishable rows sharing one ordinal is unobservable in a response, because they render
#: identically -- so the looser constraint costs nothing and refuses nothing.
SCHEMA_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS tables_by_ordinal
    ON tables(restaurant_id, ordinal);
CREATE INDEX IF NOT EXISTS opening_hours_by_ordinal
    ON opening_hours(restaurant_id, ordinal);
"""

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token   TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS restaurants (
    id                           TEXT PRIMARY KEY,
    name                         TEXT NOT NULL,
    timezone                     TEXT NOT NULL,
    slot_minutes                 INTEGER NOT NULL,
    reservation_duration_minutes INTEGER NOT NULL,
    cancellation_cutoff_minutes  INTEGER NOT NULL
);
""" + TABLES_DDL + OPENING_HOURS_DDL + """
CREATE TABLE IF NOT EXISTS reservations (
    reference       TEXT PRIMARY KEY,
    restaurant_id   TEXT NOT NULL,
    table_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    starts_at_utc   TEXT NOT NULL,
    starts_at_local TEXT NOT NULL,
    party_size      INTEGER NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_by_table
    ON reservations(table_id, starts_at_utc);
""" + IDEMPOTENCY_DDL

#: The table declarations, under the name callers already read. It is `SCHEMA_TABLES` rather than the
#: whole schema because the one index that must run after a migration lives in `SCHEMA_INDEXES`;
#: concatenating it in would restore the failure `ensure_schema` documents. Kept so that code reading
#: the DDL text -- `test_spec_stage1` takes `tables`' primary key out of it -- does not have to change
#: to accommodate an ordering constraint it has no stake in.
SCHEMA = SCHEMA_TABLES


def database_path() -> pathlib.Path:
    """Where the database lives. `TABLEKEEPER_DB` overrides, for tests."""
    override = os.environ.get("TABLEKEEPER_DB")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(__file__).resolve().parent.parent / "tablekeeper.sqlite"


def connect() -> sqlite3.Connection:
    """A connection with foreign keys and WAL enabled."""
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextlib.contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run a write inside `BEGIN IMMEDIATE`, committing on success and rolling back on any error.

    The rollback is not tidiness. §11 requires that a failed amendment leave the original booking
    and its occupancy untouched, and that is only true if the failure and the write share one
    transaction boundary.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class InvalidFixture(ValueError):
    """A reset fixture breaks one or more rules REQUIREMENTS states about it.

    One message naming every rule that broke, in fixture order, each carrying the JSON path it
    applies to. §5:47 pins every 4xx and 5xx body to exactly two keys, so these cannot be a list --
    they are one sentence in `message`, separated by `; `. That constraint and the requirement to
    report everything are not in tension: `:47` fixes the *shape* of the body, and nothing in the
    specification asks for one violation per round trip.

    Reporting only the first was the older behaviour, and it was wrong in a way the transport hid:
    a fixture with a user missing `password` *and* a reservation with `party_size: 0` answered
    `users[1].password must be a non-empty string`, and the caller had no way to learn a second
    defect existed until it had fixed the first, re-sent, and been told again. The masked error was
    not a smaller sin than the reported one; it was invisible.
    """


class _Violations:
    """Every rule a fixture breaks, in the order the fixture states them.

    Collected rather than raised so that one bad field cannot hide the next. Validation still
    happens **before** the transaction opens (see `_validated_fixture`), which is what keeps a
    rejected fixture from being half-applied -- reporting everything does not weaken that ordering,
    because nothing is written until the whole fixture has been walked.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def add(self, message: str) -> None:
        self.messages.append(message)

    def __bool__(self) -> bool:
        return bool(self.messages)

    @property
    def text(self) -> str:
        return "; ".join(self.messages)


def _entries(fixture: dict, key: str, bad: _Violations) -> list[tuple[int, dict]]:
    """`[(index, row)]` for one top-level array, skipping elements that are not objects.

    A non-object element is recorded and stepped over rather than ending the walk, so the indices
    that follow it still name the positions the caller wrote. Those indices are what make the
    message addressable: `users[3].email` has to mean the fourth entry of `users`.
    """
    rows = fixture.get(key, [])
    if not isinstance(rows, list):
        bad.add(f"{key} must be an array")
        return []
    usable = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            bad.add(f"{key}[{index}] must be an object")
            continue
        usable.append((index, row))
    return usable


def _text(value: object, path: str, bad: _Violations) -> str | None:
    if not isinstance(value, str) or not value:
        bad.add(f"{path} must be a non-empty string")
        return None
    return value


def _identifier(value: object, path: str, bad: _Violations) -> str | None:
    value = _text(value, path, bad)
    if value is None:
        return None
    if len(value) > MAX_ID_LENGTH:
        bad.add(f"{path} must be at most {MAX_ID_LENGTH} characters")
        return None
    return value


def _integer(value: object, path: str, bad: _Violations) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        bad.add(f"{path} must be an integer")
        return None
    return value


def _positive(value: object, path: str, bad: _Violations) -> int | None:
    value = _integer(value, path, bad)
    if value is None:
        return None
    if value < 1:
        bad.add(f"{path} must be a positive integer")
        return None
    return value


def _hhmm(value: str | None, path: str, bad: _Violations) -> int | None:
    """Minutes past local midnight, or `None` having recorded why not.

    Returning the number rather than the string is what lets the closes-after-opens rule be a
    comparison instead of a second parse, so a malformed `HH:MM` is reported once rather than
    reported as a format error and again as a nonsensical ordering.
    """
    if value is None:
        return None
    if not _HHMM_RE.match(value):
        bad.add(f"{path} must be local HH:MM on a 24-hour clock")
        return None
    return int(value[:2]) * 60 + int(value[3:])


def _nested(restaurant: dict, key: str, path: str, bad: _Violations) -> list[tuple[int, dict]]:
    rows = restaurant.get(key, [])
    if not isinstance(rows, list):
        bad.add(f"{path}.{key} must be an array")
        return []
    usable = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            bad.add(f"{path}.{key}[{index}] must be an object")
            continue
        usable.append((index, row))
    return usable


def _instant(reservation: dict, zones: dict[str, str], path: str, bad: _Violations) -> str | None:
    """The instant a seeded booking occupies, as `YYYY-MM-DDTHH:MM:SS±HH:MM`.

    §4 gives a fixture reservation "the same fields as a create body plus `id`, `reference`,
    `user_id`", and a create body has no `starts_at_utc` in it -- so a fixture author who follows
    the specification cannot supply one, and inserting the row as written dies on a `KeyError`
    that reaches the caller as a 500. It is derived here instead, from the same two facts a create
    derives it from: the wall clock the guest means, and the restaurant's zone.

    A fixture that *does* carry `starts_at_utc` keeps it. A test pinning an absolute instant is
    isolating something other than this rule, and §9's absolute-time arithmetic is only checkable
    when the instant is one the test chose.

    Returns `None` having recorded why, so one unusable booking does not stop the rest being
    checked. Every read of the reservation is therefore guarded: the caller reaches here only when
    `restaurant_id` is a usable id present in `zones`, but `starts_at_local` itself is a field the
    fixture may simply omit, and indexing it unguarded is the `KeyError` this docstring is about.
    """
    if "starts_at_utc" in reservation:
        return _text(reservation["starts_at_utc"], f"{path}.starts_at_utc", bad)

    local = _text(reservation.get("starts_at_local"), f"{path}.starts_at_local", bad)
    if local is None:
        return None
    try:
        naive = parse_local(local)
    except ValueError:
        bad.add(f"{path}.starts_at_local must look like YYYY-MM-DDTHH:MM")
        return None
    try:
        zone = ZoneInfo(zones[reservation["restaurant_id"]])
    except LookupError:
        bad.add(f"{path}.restaurant_id names a restaurant whose timezone is not"
                " known to this platform")
        return None
    try:
        return resolve(naive, zone).isoformat()
    except ValueError:
        bad.add(f"{path}.starts_at_local falls inside a clock change, so that local"
                " time never happened")
        return None


def _validated_fixture(fixture: dict) -> tuple[dict[str, str], dict[int, str]]:
    """Every rule §3 and §4 state about a fixture, checked before any transaction is opened.

    Returns `(zones, instants)`: each restaurant's timezone keyed by id, and the derived instant of
    each seeded reservation that did not carry one, keyed by its index in `reservations`.

    **Whole fixture, in fixture order, before the transaction.** A failure halfway through a
    transaction would be a rollback, not a rejection: `transaction()` restores the *previous*
    fixture, so a 422 raised mid-insert would hand the caller an error and leave the world it asked
    to replace still in place. That ordering is what makes a rejection trustworthy, and it is why
    the whole walk completes before `reset_database` writes a single row.

    **Every violation, not the first.** Each check records and continues rather than raising, so the
    answer names every rule the fixture broke instead of the first one the walk reached. The two
    decisions are independent: reporting everything does not weaken "validate before you write",
    because nothing is written until the walk finishes either way.

    Reporting every violation does mean a field the walk could not use is skipped rather than
    descended into -- a `restaurant_id` that is not a usable string is reported once, and the
    reservation's own `starts_at_local` is not additionally reported as missing, because the
    fixture is already known to be unusable and a second sentence about it would be noise.

    What is deliberately *not* checked: the timezone against the IANA database, and a seeded
    reservation against its table's capacity or opening hours. §4 constrains neither, a restaurant
    is allowed a zone this platform cannot resolve (§9 resolves zones when a zone is *read*, not
    when it is stored), and §4 says a booking is not rejected for being in the past -- so the
    rules that do exist are the ones checked here.
    """
    bad = _Violations()

    for index, user in _entries(fixture, "users", bad):
        path = f"users[{index}]"
        _identifier(user.get("id"), f"{path}.id", bad)
        _text(user.get("email"), f"{path}.email", bad)
        _text(user.get("password"), f"{path}.password", bad)
        if "display_name" in user:
            _text(user["display_name"], f"{path}.display_name", bad)

    zones: dict[str, str] = {}
    for index, restaurant_row in _entries(fixture, "restaurants", bad):
        path = f"restaurants[{index}]"
        restaurant_id = _identifier(restaurant_row.get("id"), f"{path}.id", bad)
        _text(restaurant_row.get("name"), f"{path}.name", bad)
        timezone = _text(restaurant_row.get("timezone"), f"{path}.timezone", bad)
        if restaurant_id is not None and timezone is not None:
            zones[restaurant_id] = timezone
        _positive(restaurant_row.get("slot_minutes"), f"{path}.slot_minutes", bad)
        _positive(restaurant_row.get("reservation_duration_minutes"),
                  f"{path}.reservation_duration_minutes", bad)
        cutoff = _integer(restaurant_row.get("cancellation_cutoff_minutes"),
                          f"{path}.cancellation_cutoff_minutes", bad)
        if cutoff is not None and cutoff < 0:
            bad.add(f"{path}.cancellation_cutoff_minutes must not be negative")

        for hours_index, hours in _nested(restaurant_row, "opening_hours", path, bad):
            hours_path = f"{path}.opening_hours[{hours_index}]"
            if hours.get("weekday") not in _WEEKDAYS:
                bad.add(f"{hours_path}.weekday must be one of "
                        f"{' '.join(_WEEKDAYS)}")
            opens = _text(hours.get("opens"), f"{hours_path}.opens", bad)
            closes = _text(hours.get("closes"), f"{hours_path}.closes", bad)
            opens_minutes = _hhmm(opens, f"{hours_path}.opens", bad)
            closes_minutes = _hhmm(closes, f"{hours_path}.closes", bad)
            if opens_minutes is not None and closes_minutes is not None \
                    and closes_minutes <= opens_minutes:
                bad.add(f"{hours_path}.closes must be later than .opens on the same "
                        "local day, because hours never cross midnight")

        for table_index, table_row in _nested(restaurant_row, "tables", path, bad):
            table_path = f"{path}.tables[{table_index}]"
            _identifier(table_row.get("id"), f"{table_path}.id", bad)
            _text(table_row.get("label"), f"{table_path}.label", bad)
            _positive(table_row.get("capacity"), f"{table_path}.capacity", bad)

    instants: dict[int, str] = {}
    for index, reservation in _entries(fixture, "reservations", bad):
        path = f"reservations[{index}]"
        _identifier(reservation.get("reference"), f"{path}.reference", bad)
        restaurant_id = _identifier(reservation.get("restaurant_id"),
                                    f"{path}.restaurant_id", bad)
        known = restaurant_id is not None and restaurant_id in zones
        if restaurant_id is not None and not known:
            bad.add(f"{path}.restaurant_id must name a restaurant in this fixture")
        _text(reservation.get("table_id"), f"{path}.table_id", bad)
        _identifier(reservation.get("user_id"), f"{path}.user_id", bad)
        _positive(reservation.get("party_size"), f"{path}.party_size", bad)
        if known:
            instant = _instant(reservation, zones, path, bad)
            if instant is not None:
                instants[index] = instant

    if bad:
        raise InvalidFixture(bad.text)

    return zones, instants


def reset_database(fixture: dict) -> None:
    """Replace the entire contents with `fixture`, atomically.

    Everything observable after this call comes from `fixture` and nothing else, which is what
    makes a test's starting state predictable. One transaction: a partially applied reset would
    leave a world that no fixture describes.

    The fixture is checked in full before that transaction opens, so a fixture the specification
    does not permit is *rejected* rather than half-applied -- see `_validated_fixture` for why the
    ordering is load-bearing.
    """
    _zones, instants = _validated_fixture(fixture)
    with transaction() as conn:
        for table in _STATE_DELETE_ORDER:
            conn.execute(f"DELETE FROM {table}")

        for user in fixture.get("users", []):
            # A seeded account has a known password in the fixture only when it is given one.
            # The harness seeds users with a plaintext password and logs in immediately, so the
            # fixture's password must be hashed the same way a signup would be.
            from . import auth

            conn.execute(
                "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
                (user["id"], user["email"], auth.hash_password(user["password"]),
                 user.get("display_name", user["id"])),
            )

        for restaurant in fixture.get("restaurants", []):
            conn.execute(
                "INSERT INTO restaurants (id, name, timezone, slot_minutes,"
                " reservation_duration_minutes, cancellation_cutoff_minutes)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (restaurant["id"], restaurant["name"], restaurant["timezone"],
                 restaurant["slot_minutes"], restaurant["reservation_duration_minutes"],
                 restaurant["cancellation_cutoff_minutes"]),
            )
            for hours_ordinal, hours in enumerate(restaurant.get("opening_hours", [])):
                conn.execute(
                    "INSERT INTO opening_hours (restaurant_id, weekday, opens, closes, ordinal)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (restaurant["id"], hours["weekday"], hours["opens"], hours["closes"],
                     hours_ordinal),
                )
            for table_ordinal, table in enumerate(restaurant.get("tables", [])):
                conn.execute(
                    "INSERT INTO tables (restaurant_id, id, label, capacity, ordinal)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (restaurant["id"], table["id"], table["label"], table["capacity"],
                     table_ordinal),
                )

        for index, reservation in enumerate(fixture.get("reservations", [])):
            conn.execute(
                "INSERT INTO reservations (reference, restaurant_id, table_id, user_id,"
                " starts_at_utc, starts_at_local, party_size, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reservation["reference"], reservation["restaurant_id"],
                 reservation["table_id"], reservation["user_id"],
                 instants[index], reservation["starts_at_local"], reservation["party_size"],
                 reservation.get("status", "confirmed"),
                 reservation.get("created_at", dt.datetime.now(dt.timezone.utc).isoformat())),
            )


# ---- §10: export and import -------------------------------------------------------------------
#
# §10 asks for one object that leaves the service carrying everything it had, and one endpoint that
# puts it back. The shape of the work is decided by what §10:167 and §10:175 require and by what
# they refuse:
#
# * **Replacement, not reseeding.** §10:167 says import "is replacement, not merge; repeating it
#   restores the exported state without duplicating anything", and §10:180 spells out the failure
#   mode by name: "replacing the state with a fresh fixture does not satisfy this requirement". A
#   fixture carries plaintext passwords and no receipts, no tokens and no timestamps, so routing
#   import through `reset_database` would pass every status-code assertion in the section and lose
#   every identity in it. Hence a row-level dump and a row-level restore, with `reset_database` left
#   doing what §3.3 asks of it.
# * **Everything, or nothing.** §10:169-170 requires an invalid envelope to answer 422 "without
#   changing the destination", so the whole state is validated before a transaction opens, and the
#   truncate-and-rewrite then shares one `BEGIN IMMEDIATE`. There is no window in which the
#   destination is half an imported world.
# * **Opaque to the caller, and a round trip.** §10:165 makes `state` "implementation-defined" but
#   requires import to accept an unchanged export "produced by this service" -- so `import` reads
#   back everything `export` wrote, in a deterministic order, with nothing derived on either side.

#: §10:163-164 pins the two fields the envelope carries besides `state`, and §10:165 makes `state`
#: opaque. These three constants are the whole of what a caller may rely on. They are declared here,
#: next to the state they wrap, because they are one declaration: an export written with one `track`
#: and refused by an import reading another is a bug a single source cannot have.
EXPORT_TRACK = "tablekeeper"
EXPORT_FORMAT_VERSION = 1

#: Inside `state`, and deliberately *not* one of the three envelope fields. §10 names `track` and
#: `format_version` and says of `state` only that it is "an implementation-defined JSON object" which
#: import "must accept unchanged"; it also requires import to answer 422 to "an invalid state",
#: which needs something to tell valid from invalid. This marker is that something: an object
#: carrying it is one of our exports, and one that does not is refused before a single row is
#: written rather than being handed to the table writers and failing there as a `sqlite3` error.
#:
#: It is a *marker*, not a compatibility promise. Bumping the schema is a deliberate migration, and
#: the honest form of one is to teach `_validated_state` the old shape beside the new -- not to
#: accept a shape and hope.
STATE_SCHEMA = "tablekeeper/state/1"

#: Every state table, with the columns an export writes and an import reads back, and the JSON type
#: each column must hold. One structure for all three jobs on purpose: the column list an export
#: reads, the column list an import writes and the type list a validation pass checks are three views
#: of the same declaration, and three separate lists would be free to disagree -- an import that
#: wrote a column the export did not carry would fail at the primary key, inside the transaction,
#: as a 500 rather than as the 422 §10:169 asks for.
#:
#: Iteration order is load-bearing too: this is the order rows are *inserted*, and `tokens`,
#: `opening_hours` and `tables` all carry a foreign key, so a parent must be written before its
#: children or the insert fails.
_STATE_TABLES_SPEC: dict[str, tuple[tuple[str, type], ...]] = {
    "users": (("id", str), ("email", str), ("password_hash", str), ("display_name", str)),
    "tokens": (("token", str), ("user_id", str)),
    "restaurants": (("id", str), ("name", str), ("timezone", str), ("slot_minutes", int),
                    ("reservation_duration_minutes", int), ("cancellation_cutoff_minutes", int)),
    "opening_hours": (("restaurant_id", str), ("weekday", str), ("opens", str), ("closes", str),
                      ("ordinal", int)),
    "tables": (("restaurant_id", str), ("id", str), ("label", str), ("capacity", int),
               ("ordinal", int)),
    "reservations": (("reference", str), ("restaurant_id", str), ("table_id", str),
                     ("user_id", str), ("starts_at_utc", str), ("starts_at_local", str),
                     ("party_size", int), ("status", str), ("created_at", str)),
    "idempotency": (("key", str), ("user_id", str), ("scope", str), ("request_hash", str),
                    ("status_code", int), ("response_body", str)),
}

#: The order a replacement empties the tables in, and it is not the insert order reversed by habit
#: but the order the foreign keys force: every child has to go before its parent, because
#: `connect()` sets `PRAGMA foreign_keys=ON` and a parent row with a child still pointing at it
#: cannot be deleted. `reset_database` deletes in this same order, and the two are one invariant
#: rather than two coincidentally equal literals -- a second list would be free to drift and would
#: fail as an `IntegrityError` inside the transaction, which for `import` means a rollback on a
#: correct object.
_STATE_DELETE_ORDER = ("idempotency", "reservations", "tokens", "opening_hours",
                       "tables", "restaurants", "users")

#: The order an export reads each table in. Nothing here is a product requirement -- an export may
#: list its rows in whatever order is cheapest -- but it *is* the reason
#: `export(import(state)) == state` holds, which §10:165 turns on: import accepts the object
#: unchanged and the caller may reasonably export it again and compare. Ordering on the primary key
#: makes that true of a database the service cannot otherwise tell apart from any other.
#:
#: `tables` and `opening_hours` are the two that are not keyed by something unique. They are read on
#: `(parent, ordinal)` first so fixture order survives the round trip, and the remaining columns are
#: in the ORDER BY only to make the read total: `opening_hours` has no primary key at all, so two
#: byte-identical rows are representable, and a reader that stopped at `ordinal` could emit them in
#: either order on two runs over one database.
_STATE_READ_ORDER = {
    "users": "id",
    "tokens": "token",
    "restaurants": "id",
    "opening_hours": "restaurant_id, ordinal, weekday, opens, closes",
    "tables": "restaurant_id, ordinal, id",
    "reservations": "reference",
    "idempotency": "key, user_id, scope",
}


class InvalidState(ValueError):
    """An import object breaks one of the rules §10 states about it.

    Separate from `InvalidFixture` rather than shared with it because the two are judged by
    different contracts and are reached from different endpoints: §3.3 validates a fixture, §10
    validates an opaque round trip of this service's own making. A caller cannot author a valid
    `state` by reading §4, so reusing the fixture's rules here would refuse objects §10 requires
    import to accept.
    """


def export_state() -> dict:
    """The whole service state as one JSON value. §10:162-167.

    **One read transaction, so the object is one instant.** §10:167 requires export to be "an
    atomic, read-only snapshot; subsequent source writes do not change it". WAL gives a deferred
    transaction its snapshot at the first read, so every table below is read from the same picture of
    the database -- a reader taking seven separate connections could otherwise be handed a
    reservation whose receipt had not been written yet, which is precisely the pairing §10:176
    requires to survive the round trip.

    **Row-level, and every row of every table.** §10:179 lists what has to come back: accounts and
    their hashed passwords, existing bearer tokens, fixture configuration, reservations and
    references, and "all completed idempotent request bodies and original responses". The
    `idempotency` table is therefore exported whole, `request_hash` and `response_body` included --
    it is what makes a lost-response retry replay rather than book twice after the import, which is
    the requirement §10 and §11:205 both rest on.

    **The fixture's `ordinal` columns are carried, not recomputed.** `tables` and `opening_hours`
    order by a position in the fixture array that exists nowhere else, and §8:112 asks for
    "fixture order" in as many words. An import that re-derived an ordinal from the row order would
    be guessing at the one thing the export was able to state exactly.

    Nothing here is filtered, hashed or redacted. §10:163 says an export "may contain credentials
    and session tokens", so `password_hash` and `token` travel, and `track`/`format_version` are how
    the caller knows it is holding something private.
    """
    conn = connect()
    try:
        conn.execute("BEGIN")
        state: dict[str, object] = {"schema": STATE_SCHEMA}
        for table, spec in _STATE_TABLES_SPEC.items():
            names = [name for name, _ in spec]
            rows = conn.execute(
                f"SELECT {', '.join(names)} FROM {table}"
                f" ORDER BY {_STATE_READ_ORDER[table]}"
            ).fetchall()
            state[table] = [{name: row[name] for name in names} for row in rows]
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return state


def import_state(state: object) -> None:
    """Replace the entire contents with `state`, atomically. §10:167-180.

    **Validated in full before the transaction opens**, for the reason `_validated_fixture` gives
    and not because the two share a rule: §10:170 requires 422 "without changing the destination",
    and a validation failure raised mid-truncate would be a rollback, which leaves the destination
    intact only by luck of where the walk stopped. Validating first makes the refusal a refusal
    rather than an accident.

    **Rows are written back verbatim, by column name.** `reference`, `reservation_id`,
    `created_at`, `starts_at_utc`, token strings and receipt bodies are all copied rather than
    regenerated, because §10:175 requires identities, statuses and timestamps not to move and
    §10:178 requires an existing retry to still replay the original response afterwards. An unknown
    key in a row is ignored and a missing one is refused, both during validation, so nothing a
    caller adds can reach a column the export did not name.

    **Replacement, so everything after the export is gone.** Every table is emptied before any row
    is written, which is what removes "all previous destination data and credentials" (§10:180) --
    including an account created after the export, whose token then resolves to nothing and answers
    401 rather than authenticating an account that no longer exists.
    """
    _validated_state(state)
    with transaction() as conn:
        for table in _STATE_DELETE_ORDER:
            conn.execute(f"DELETE FROM {table}")
        for table, spec in _STATE_TABLES_SPEC.items():
            names = [name for name, _ in spec]
            placeholders = ", ".join("?" * len(names))
            statement = (f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})")
            for row in state[table]:
                conn.execute(statement, tuple(row[name] for name in names))


def _validated_state(state: object) -> None:
    """Every rule §10 states about a `state`, checked before any transaction is opened.

    Three levels, and which one a bad object fails at is worth stating because it decides the
    message: a `state` that is not an object, or that does not carry `STATE_SCHEMA`, is refused
    immediately -- it is not this service's export and there is nothing to walk -- while a row or a
    column that is the wrong shape is collected, so one object with two bad fields names both.

    The type of every column is checked rather than assumed, and `int` excludes `bool`. That is not
    fastidiousness: `sqlite3` binds a Python `True` as `1` without complaint, so an export claiming
    `party_size: true` would be stored as a party of one and answer 201 for a booking nobody asked
    for. §10:170 says an invalid state is 422, and the only place to notice is before the write.

    Value rules §4 states about a *fixture* are deliberately not restated here. A `state` is not
    authored by a caller from the specification -- §10 makes it opaque and requires import to accept
    an unchanged export of ours -- so there is nothing to validate a length against; refusing a
    state our own export produced would break the round trip the section is built on.
    """
    if not isinstance(state, dict):
        raise InvalidState("state must be a JSON object")
    if state.get("schema") != STATE_SCHEMA:
        raise InvalidState(f"state.schema must be {STATE_SCHEMA!r}")

    bad = _Violations()
    for table, spec in _STATE_TABLES_SPEC.items():
        rows = state.get(table)
        if not isinstance(rows, list):
            raise InvalidState(f"state.{table} must be an array")
        for index, row in enumerate(rows):
            path = f"state.{table}[{index}]"
            if not isinstance(row, dict):
                raise InvalidState(f"{path} must be an object")
            for column, kind in spec:
                value = row.get(column)
                if kind is int:
                    # `bool` is an `int` in Python and binds as 1, so it has to be named out.
                    usable = isinstance(value, int) and not isinstance(value, bool)
                else:
                    usable = isinstance(value, str)
                if not usable:
                    bad.add(f"{path}.{column} must be "
                            + ("an integer" if kind is int else "a string"))
    if bad:
        raise InvalidState(bad.text)


def ensure_schema() -> None:
    """Create tables if absent, and carry an older database forward. Idempotent.

    Safe to call on every start, which is what `serve()` does: `CREATE TABLE IF NOT EXISTS` only
    creates what is missing, so the four migrations below are what make a *changed* declaration take
    effect on a database that already exists.

    **`SCHEMA` is applied in two halves, and the split is load-bearing.** `tables_by_ordinal` and
    `opening_hours_by_ordinal` index columns that a database written before this change does not
    have, so applying the whole declaration first would fail with `no such column: ordinal` on exactly
    the legacy database the migration exists to repair. The indexes therefore run *after*
    `_add_table_fixture_ordinals` and `_add_opening_hours_fixture_ordinals` have added their columns,
    rather than being tolerated in a try/except: a swallowed failure here would leave the index missing
    on every future start, and nothing would report it.

    **The key widening and the column additions are separate migrations on purpose, and the order is
    what makes each one's guard sufficient.** A read that concludes "the key is already composite" is
    not a read that concludes "nothing is left to do", and collapsing the two into one guard is how a
    database that already passed the widening would skip the backfill forever -- the failure this
    file's own warning describes, arriving through a fix. Measured on all three shapes a legacy file
    can have: global `id` key goes through the widen, whose `ROW_NUMBER() OVER (...)` already writes
    sequential ordinals, so the column migration then finds the column present and does nothing;
    composite key with no ordinal goes past the widen's guard untouched and is repaired by
    `_add_table_fixture_ordinals`, which tests for the *column*; composite key with the column is left
    alone entirely.
    """
    conn = connect()
    try:
        conn.executescript(SCHEMA_TABLES)
        _widen_tables_key_to_the_restaurant(conn)
        _widen_idempotency_for_path_scoping(conn)
        _add_table_fixture_ordinals(conn)
        _add_opening_hours_fixture_ordinals(conn)
        conn.executescript(SCHEMA_INDEXES)
    finally:
        conn.close()


def _widen_tables_key_to_the_restaurant(conn: sqlite3.Connection) -> None:
    """Carry `tables` across from the global-`id` shape, without losing a row.

    The same problem `_widen_idempotency_for_path_scoping` solves, and the same reason it is not
    solved by `ALTER TABLE`: SQLite cannot widen a primary key, so the table is rebuilt -- renamed
    aside, the current DDL reissued, the rows copied across, and only then the old table dropped.

    It matters here because `CREATE TABLE IF NOT EXISTS` is silent about a table that already
    exists in the *old* shape. Without this, the composite key in `SCHEMA` applies only to a
    database created after the change: a shipped database, or one on a volume that survives a
    restart, keeps `id` globally unique and every fixture the specification permits still dies at
    reset -- the fix would read as landed and do nothing.

    **Nothing is lost and nothing is deduplicated.** The old key forbade a table id appearing
    twice anywhere, so the copy cannot collide, and the rebuild is the one place a row could
    disappear -- hence copy, then drop.

    **The shape is read inside the write lock, not before it.** Same reasoning as the idempotency
    migration below, and for the same reason it was wrong there first: read outside the
    transaction and a second process blocks on `BEGIN IMMEDIATE` holding a decision it made about
    a table the first one has already rebuilt. The rebuild is idempotent, so the damage here is
    bounded to doing it twice -- but a migration that reads its own precondition under a lock is
    the invariant, and it is worth one uncontended `BEGIN`/`COMMIT` per start to keep.

    A process killed mid-migration rolls back to the old table intact and the next start tries
    again; there is no window in which `tables` does not exist.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        info = list(conn.execute("PRAGMA table_info(tables)"))
        if not info:
            conn.execute("COMMIT")
            return
        # `pk` is the 1-based position in the primary key, 0 for a plain column.
        primary_key = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
        if primary_key == ["restaurant_id", "id"]:
            conn.execute("COMMIT")
            return

        conn.execute("ALTER TABLE tables RENAME TO tables__pre_restaurant_key")
        conn.execute(TABLES_DDL)
        conn.execute(
            "INSERT INTO tables (restaurant_id, id, label, capacity, ordinal)"
            " SELECT restaurant_id, id, label, capacity,"
            " ROW_NUMBER() OVER (PARTITION BY restaurant_id ORDER BY id) - 1"
            " FROM tables__pre_restaurant_key")
        conn.execute("DROP TABLE tables__pre_restaurant_key")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _add_table_fixture_ordinals(conn: sqlite3.Connection) -> None:
    """Give `tables` an `ordinal` recording its position in its restaurant's fixture array.

    §8:112 asks for `available_table_ids` "in fixture order" in as many words, and sorting by `id`
    answers a different question -- alphabetical -- which coincides with fixture order only on a
    fixture whose ids are already sorted. `ALTER TABLE ... ADD COLUMN` is enough: the column is
    `NOT NULL DEFAULT 0`, which SQLite permits on a table that already has rows, so unlike the
    primary-key widenings above this needs no rebuild and cannot lose a row.

    **Only `tables`, and `opening_hours` is the twin -- but `restaurants` is not.** §8:112 asks for
    `available_table_ids` "in fixture order" in as many words and §8:101 returns `tables` and
    `opening_hours` "in the fixture's shape", which this repository reads as order and has two named
    red defects for. §8:99 asks for something else: `GET /restaurants` is pinned to
    `{"restaurants":[{id,name,timezone}]}` and **nothing else**. No order, no "fixture's shape".

    That is not only an absence in the prose, and the measurement is worth keeping because the
    argument has been made twice in this room on citation alone. The shipped
    `test_restaurants_list_envelope` has four legs -- envelope shape, two seeded restaurants listed,
    every entry carrying `id`/`name`/`timezone` -- and **not one of them mentions order**. The name is
    red for the envelope: the handler returns a bare list. Alphabetising it is therefore not a defect
    this suite can fail and not a requirement the specification makes.

    The cost is also concrete rather than theoretical: `tests/test_store_schema.py:156` writes
    `INSERT INTO restaurants VALUES ('r_one','One','Europe/Berlin',30,90,120)` positionally against
    six columns. A seventh column makes that raise `table restaurants has 7 columns but 6 values were
    supplied`, so the column would break a shipped test on the way in.

    If restaurant order is ever wanted it is one column and one migration, and it should be argued for
    on the specification rather than smuggled in beside a requirement that does need it.

    **The backfill is deterministic but it is not the truth, and that is fine.** A row's real ordinal
    lived only in the JSON array that produced it, and that array is gone -- so for a database
    written before this column the best available answer is *some* fixed order, and `(restaurant_id,
    id)` gives the same one on every machine. That looked like a hazard until `reset` was taken into
    account: `reset_database` deletes every row of all seven tables and rewrites them from the
    fixture in array order, so `tables` rows exist *only* as a product of a reset. A pre-ordinal
    database is one built by an older binary and never reset since, and no API can observe its
    ordinals, because the first reset converges them to true fixture order. Two databases with
    identical logical content returning different orderings is therefore not reachable through the
    service, and the backfill only has to be stable, not faithful.

    **The unique index is created after the backfill, not before.** Created first, it would fail:
    every row would still hold the `DEFAULT 0`, and `(restaurant_id, 0)` collides on the second table
    of a restaurant.

    **The shape is read inside the write lock, not before it.** Same invariant, and the same reason
    it was wrong twice already in this file: read outside the transaction and a second process blocks
    on `BEGIN IMMEDIATE` while holding a decision made about a table the first one has already
    altered. The rebuild is idempotent, so the damage is bounded to doing it twice -- but reading a
    migration's own precondition under the lock it holds is the invariant, and it is worth one
    uncontended `BEGIN`/`COMMIT` per start to keep.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        info = list(conn.execute("PRAGMA table_info(tables)"))
        if info and "ordinal" not in {row["name"] for row in info}:
            conn.execute("ALTER TABLE tables ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0")
            # Assigned in Python rather than with `ROW_NUMBER() OVER (...)`: SQLite rejects a window
            # function directly in an UPDATE's SET, and a correlated subquery would make this depend
            # on a window-function-capable SQLite to count the rows of a fixture-sized table.
            seen: dict[str, int] = {}
            for group, identifier in conn.execute(
                    "SELECT restaurant_id, id FROM tables ORDER BY restaurant_id, id"
            ):
                position = seen.get(group, 0)
                seen[group] = position + 1
                conn.execute(
                    "UPDATE tables SET ordinal = ? WHERE restaurant_id = ? AND id = ?",
                    (position, group, identifier),
                )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _add_opening_hours_fixture_ordinals(conn: sqlite3.Connection) -> None:
    """Give `opening_hours` an `ordinal` recording its position in its restaurant's fixture array.

    The twin of `_add_table_fixture_ordinals`, and it exists for the same reason: §8:101 returns
    `opening_hours` "in the fixture's shape", `ORDER BY weekday` answers a different question --
    alphabetical -- and on a fixture written `sat, mon, fri` the two disagree, which is exactly the
    shipped `opening_hours_in_fixture_order`. `ALTER TABLE ... ADD COLUMN` is enough, because the
    column is `NOT NULL DEFAULT 0` and SQLite permits that on a table that already has rows: no
    rebuild, and so no way to lose one.

    **The backfill is ordered by content, not by rowid.** `(restaurant_id, weekday, opens, closes)`
    gives the same answer for the same logical rows on every machine and in every file. Ordering by
    `rowid` would tie-break identically-identical rows by insertion accident, which is the property
    this column exists to stop depending on.

    **Two byte-identical rows are the one case the backfill cannot separate**, and it is left
    standing rather than repaired: they receive one shared ordinal, which is why
    `opening_hours_by_ordinal` is a plain index. Deduplicating would silently delete configuration a
    fixture asked for, and §10:172 requires import to preserve fixture configuration rather than
    renormalise it. The rows render identically, so nothing observable depends on which comes first.

    **The backfill is deterministic but it is not the truth, and that is fine** -- the same argument
    as `_add_table_fixture_ordinals`, and it rests on the same measured fact: `reset_database` deletes
    every row of all seven tables and rewrites them from the fixture in array order, so these rows
    exist *only* as a product of a reset. A pre-ordinal database is one written by an older binary and
    never reset since, and the first reset converges it to true fixture order, so no API can observe
    the backfilled values. Stable is the whole requirement; faithful is unavailable.

    **The shape is read inside the write lock**, for the same reason as the two migrations above it:
    a decision made outside `BEGIN IMMEDIATE` is stale by the time the lock is granted.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        info = list(conn.execute("PRAGMA table_info(opening_hours)"))
        if info and "ordinal" not in {row["name"] for row in info}:
            conn.execute("ALTER TABLE opening_hours ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0")
            # Assigned in Python rather than with `ROW_NUMBER() OVER (...)`: SQLite rejects a window
            # function directly in an UPDATE's SET, and this keeps the file dependent on nothing newer
            # than a plain cursor for a fixture-sized table.
            seen: dict[str, int] = {}
            for row in conn.execute(
                    "SELECT restaurant_id, weekday, opens, closes FROM opening_hours"
                    " ORDER BY restaurant_id, weekday, opens, closes"
            ):
                group = row["restaurant_id"]
                position = seen.get(group, 0)
                seen[group] = position + 1
                conn.execute(
                    "UPDATE opening_hours SET ordinal = ?"
                    " WHERE restaurant_id = ? AND weekday = ? AND opens = ? AND closes = ?",
                    (position, row["restaurant_id"], row["weekday"], row["opens"], row["closes"]),
                )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


#: The scope every receipt written before §7:79 must carry after the upgrade. `POST /reservations`
#: was the only idempotency-required path in the service when such a receipt was written --
#: `POST /reservation-moves` did not exist -- and `Request.idempotency_scope` builds
#: `"{METHOD} {path}"` from a path with its query string already stripped, so `/reservations` is the
#: only path a pre-moves create could have recorded. Anything else would answer 409
#: `idempotency_key_reuse` to a legitimate retry, or silently treat it as a first use.
_PRE_SCOPE_RESERVATIONS_SCOPE = "POST /reservations"


def _widen_idempotency_for_path_scoping(conn: sqlite3.Connection) -> None:
    """Carry `idempotency` across from the pre-§7:79 shape, without losing a receipt.

    `CREATE TABLE IF NOT EXISTS` cannot widen an existing table, so a database written before moves
    existed still carries the old `(key, user_id)` primary key and no `scope` column, and every
    insert against the new schema would fail for want of the column. Widening a primary key is the
    one thing SQLite cannot do with `ALTER TABLE`, so the table is rebuilt -- renamed aside, the
    current DDL reissued, the rows copied across, and only then the old table dropped.

    Two orders are load-bearing, and only one of them was right at first.

    **Copy, then drop.** The receipts are copied before anything is destroyed, so no failure between
    the two can lose one.

    **Read the shape inside the transaction, not before it.** The rebuild, the `PRAGMA table_info`
    that decides whether it is needed, and the backfill constant derived from that read all happen
    under the same `BEGIN IMMEDIATE`. Read the shape first and the decision is stale the moment it
    is made: a second process starting on the same database reads "no `scope` column", blocks on
    `BEGIN IMMEDIATE` until the first one commits, and then rebuilds the *already-migrated* table
    with the backfill constant it chose before the lock -- rewriting every `POST /reservation-moves`
    receipt to `POST /reservations`. The row survives, so nothing looks lost, but it can never be
    found again on its own path: a replay of that batch becomes a first use, and §11:201's batch
    applies a second time. Inside the transaction the second process reads the committed shape, sees
    nothing to do, and leaves every scope alone.

    The price is that `ensure_schema` now takes the write lock on every start, even on a database
    already in the new shape. That is one uncontended `BEGIN`/`COMMIT` per process start against a
    ten-second `busy_timeout`, which is cheaper than the receipt rewrite it prevents.

    A process killed mid-migration still rolls back to the old table intact and the next start tries
    again -- there is no window in which `idempotency` does not exist.

    Receipts are not derived state in the way the previous version of this function claimed. No
    domain row references them, true, but §7 makes a client depend on one directly: :86/:92 owe it
    the original response, and a key with no receipt is a first use, which for a create is a second
    booking. That is why nothing here drops a row.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        info = list(conn.execute("PRAGMA table_info(idempotency)"))
        if not info:
            conn.execute("COMMIT")
            return
        columns = {row["name"] for row in info}
        # `pk` is the 1-based position in the primary key, 0 for a plain column.
        primary_key = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
        if "scope" in columns and primary_key == ["key", "user_id", "scope"]:
            conn.execute("COMMIT")
            return

        # A table that already has `scope` keeps whatever it says; one that does not is backfilled,
        # because a receipt whose scope cannot be recovered is a receipt that will never replay.
        scope_source = "scope" if "scope" in columns else "?"
        params = () if "scope" in columns else (_PRE_SCOPE_RESERVATIONS_SCOPE,)
        conn.execute("ALTER TABLE idempotency RENAME TO idempotency__pre_scope")
        conn.execute(IDEMPOTENCY_DDL)
        conn.execute(
            "INSERT INTO idempotency (key, user_id, scope, request_hash, status_code,"
            " response_body) SELECT key, user_id, " + scope_source + ", request_hash, status_code,"
            " response_body FROM idempotency__pre_scope", params)
        conn.execute("DROP TABLE idempotency__pre_scope")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise