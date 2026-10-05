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

__all__ = ["database_path", "connect", "transaction", "reset_database", "SCHEMA",
           "InvalidFixture"]

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
TABLES_DDL = """
CREATE TABLE IF NOT EXISTS tables (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
    id           TEXT NOT NULL,
    label        TEXT NOT NULL,
    capacity     INTEGER NOT NULL,
    PRIMARY KEY (restaurant_id, id)
);
"""

SCHEMA = """
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
""" + TABLES_DDL + """
CREATE TABLE IF NOT EXISTS opening_hours (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
    weekday       TEXT NOT NULL,
    opens         TEXT NOT NULL,
    closes        TEXT NOT NULL
);
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
    """A reset fixture breaks a rule REQUIREMENTS states about it.

    One message, naming the path that failed, and only the first failure. §5:47 gives every 4xx and
    5xx body exactly two keys, so there is nowhere to put a list of everything else that is wrong;
    a caller who sends four bad fields is told about one, fixes it, sends it again, and is told
    about the next.
    """


def _entries(fixture: dict, key: str) -> list[tuple[int, dict]]:
    """`[(index, row)]` for one top-level array, every element already known to be an object."""
    rows = fixture.get(key, [])
    if not isinstance(rows, list):
        raise InvalidFixture(f"{key} must be an array")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InvalidFixture(f"{key}[{index}] must be an object")
    return list(enumerate(rows))


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidFixture(f"{path} must be a non-empty string")
    return value


def _identifier(value: object, path: str) -> str:
    value = _text(value, path)
    if len(value) > MAX_ID_LENGTH:
        raise InvalidFixture(f"{path} must be at most {MAX_ID_LENGTH} characters")
    return value


def _integer(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidFixture(f"{path} must be an integer")
    return value


def _positive(value: object, path: str) -> int:
    value = _integer(value, path)
    if value < 1:
        raise InvalidFixture(f"{path} must be a positive integer")
    return value


def _nested(restaurant: dict, key: str, path: str) -> list[tuple[int, dict]]:
    rows = restaurant.get(key, [])
    if not isinstance(rows, list):
        raise InvalidFixture(f"{path}.{key} must be an array")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InvalidFixture(f"{path}.{key}[{index}] must be an object")
    return list(enumerate(rows))


def _instant(reservation: dict, zones: dict[str, str], path: str) -> str:
    """The instant a seeded booking occupies, as `YYYY-MM-DDTHH:MM:SS±HH:MM`.

    §4 gives a fixture reservation "the same fields as a create body plus `id`, `reference`,
    `user_id`", and a create body has no `starts_at_utc` in it -- so a fixture author who follows
    the specification cannot supply one, and inserting the row as written dies on a `KeyError`
    that reaches the caller as a 500. It is derived here instead, from the same two facts a create
    derives it from: the wall clock the guest means, and the restaurant's zone.

    A fixture that *does* carry `starts_at_utc` keeps it. A test pinning an absolute instant is
    isolating something other than this rule, and §9's absolute-time arithmetic is only checkable
    when the instant is one the test chose.
    """
    if "starts_at_utc" in reservation:
        return reservation["starts_at_utc"]
    try:
        naive = parse_local(reservation["starts_at_local"])
    except ValueError as exc:
        raise InvalidFixture(f"{path}.starts_at_local must look like YYYY-MM-DDTHH:MM") from exc
    try:
        zone = ZoneInfo(zones[reservation["restaurant_id"]])
    except LookupError as exc:
        raise InvalidFixture(f"{path}.restaurant_id names a restaurant whose timezone is not"
                             " known to this platform") from exc
    try:
        return resolve(naive, zone).isoformat()
    except ValueError as exc:
        raise InvalidFixture(f"{path}.starts_at_local falls inside a clock change, so that local"
                             " time never happened") from exc


def _validated_fixture(fixture: dict) -> tuple[dict[str, str], dict[int, str]]:
    """Every rule §3 and §4 state about a fixture, checked before any transaction is opened.

    Returns `(zones, instants)`: each restaurant's timezone keyed by id, and the derived instant of
    each seeded reservation that did not carry one, keyed by its index in `reservations`.

    **Whole fixture, in fixture order, before the transaction.** Two reasons, and the ordering is
    the only thing that delivers either. First, a failure halfway through a transaction would be a
    rollback, not a rejection: `transaction()` restores the *previous* fixture, so a 422 raised
    mid-insert would hand the caller an error and leave the world it asked to replace still in
    place. Second, first-error-wins is only stable if the first error is decided by the fixture's
    own order rather than by which constraint SQLite happened to reach first.

    What is deliberately *not* checked: the timezone against the IANA database, and a seeded
    reservation against its table's capacity or opening hours. §4 constrains neither, a restaurant
    is allowed a zone this platform cannot resolve (§9 resolves zones when a zone is *read*, not
    when it is stored), and §4 says a booking is not rejected for being in the past -- so the
    rules that do exist are the ones checked here.
    """
    for index, user in _entries(fixture, "users"):
        path = f"users[{index}]"
        _identifier(user.get("id"), f"{path}.id")
        _text(user.get("email"), f"{path}.email")
        _text(user.get("password"), f"{path}.password")
        if "display_name" in user:
            _text(user["display_name"], f"{path}.display_name")

    zones: dict[str, str] = {}
    for index, restaurant_row in _entries(fixture, "restaurants"):
        path = f"restaurants[{index}]"
        restaurant_id = _identifier(restaurant_row.get("id"), f"{path}.id")
        _text(restaurant_row.get("name"), f"{path}.name")
        zones[restaurant_id] = _text(restaurant_row.get("timezone"), f"{path}.timezone")
        _positive(restaurant_row.get("slot_minutes"), f"{path}.slot_minutes")
        _positive(restaurant_row.get("reservation_duration_minutes"),
                  f"{path}.reservation_duration_minutes")
        cutoff = _integer(restaurant_row.get("cancellation_cutoff_minutes"),
                          f"{path}.cancellation_cutoff_minutes")
        if cutoff < 0:
            raise InvalidFixture(f"{path}.cancellation_cutoff_minutes must not be negative")

        for hours_index, hours in _nested(restaurant_row, "opening_hours", path):
            hours_path = f"{path}.opening_hours[{hours_index}]"
            if hours.get("weekday") not in _WEEKDAYS:
                raise InvalidFixture(f"{hours_path}.weekday must be one of "
                                     f"{' '.join(_WEEKDAYS)}")
            opens = _text(hours.get("opens"), f"{hours_path}.opens")
            closes = _text(hours.get("closes"), f"{hours_path}.closes")
            if not _HHMM_RE.match(opens) or not _HHMM_RE.match(closes):
                raise InvalidFixture(f"{hours_path}.opens and .closes must be local HH:MM "
                                     "on a 24-hour clock")
            if int(closes[:2]) * 60 + int(closes[3:]) <= int(opens[:2]) * 60 + int(opens[3:]):
                raise InvalidFixture(f"{hours_path}.closes must be later than .opens on the same "
                                     "local day, because hours never cross midnight")

        for table_index, table_row in _nested(restaurant_row, "tables", path):
            table_path = f"{path}.tables[{table_index}]"
            _identifier(table_row.get("id"), f"{table_path}.id")
            _text(table_row.get("label"), f"{table_path}.label")
            _positive(table_row.get("capacity"), f"{table_path}.capacity")

    instants: dict[int, str] = {}
    for index, reservation in _entries(fixture, "reservations"):
        path = f"reservations[{index}]"
        _identifier(reservation.get("reference"), f"{path}.reference")
        restaurant_id = _identifier(reservation.get("restaurant_id"), f"{path}.restaurant_id")
        if restaurant_id not in zones:
            raise InvalidFixture(f"{path}.restaurant_id must name a restaurant in this fixture")
        _text(reservation.get("table_id"), f"{path}.table_id")
        _identifier(reservation.get("user_id"), f"{path}.user_id")
        _positive(reservation.get("party_size"), f"{path}.party_size")
        instants[index] = _instant(reservation, zones, path)

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
        for table in ("idempotency", "reservations", "tokens", "opening_hours",
                      "tables", "restaurants", "users"):
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
            for hours in restaurant.get("opening_hours", []):
                conn.execute(
                    "INSERT INTO opening_hours (restaurant_id, weekday, opens, closes)"
                    " VALUES (?, ?, ?, ?)",
                    (restaurant["id"], hours["weekday"], hours["opens"], hours["closes"]),
                )
            for table in restaurant.get("tables", []):
                conn.execute(
                    "INSERT INTO tables (restaurant_id, id, label, capacity)"
                    " VALUES (?, ?, ?, ?)",
                    (restaurant["id"], table["id"], table["label"], table["capacity"]),
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


def ensure_schema() -> None:
    """Create tables if absent, and carry an older database forward. Idempotent.

    Safe to call on every start, which is what `serve()` does: `CREATE TABLE IF NOT EXISTS` only
    creates what is missing, so the two migrations below are what make a *changed* declaration take
    effect on a database that already exists.
    """
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _widen_tables_key_to_the_restaurant(conn)
        _widen_idempotency_for_path_scoping(conn)
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
            "INSERT INTO tables (restaurant_id, id, label, capacity)"
            " SELECT restaurant_id, id, label, capacity FROM tables__pre_restaurant_key")
        conn.execute("DROP TABLE tables__pre_restaurant_key")
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