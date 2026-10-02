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
import sqlite3
from typing import Iterator

__all__ = ["database_path", "connect", "transaction", "SCHEMA"]

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
CREATE TABLE IF NOT EXISTS tables (
    id           TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
    label        TEXT NOT NULL,
    capacity     INTEGER NOT NULL
);
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
CREATE TABLE IF NOT EXISTS idempotency (
    key           TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    PRIMARY KEY (key, user_id)
);
"""


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


def reset_database(fixture: dict) -> None:
    """Replace the entire contents with `fixture`, atomically.

    Everything observable after this call comes from `fixture` and nothing else, which is what
    makes a test's starting state predictable. One transaction: a partially applied reset would
    leave a world that no fixture describes.
    """
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
                    "INSERT INTO tables (id, restaurant_id, label, capacity) VALUES (?, ?, ?, ?)",
                    (table["id"], restaurant["id"], table["label"], table["capacity"]),
                )

        for reservation in fixture.get("reservations", []):
            conn.execute(
                "INSERT INTO reservations (reference, restaurant_id, table_id, user_id,"
                " starts_at_utc, starts_at_local, party_size, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reservation["reference"], reservation["restaurant_id"],
                 reservation["table_id"], reservation["user_id"],
                 reservation["starts_at_utc"], reservation["starts_at_local"],
                 reservation["party_size"], reservation.get("status", "confirmed"),
                 reservation.get("created_at", dt.datetime.now(dt.timezone.utc).isoformat())),
            )


def ensure_schema() -> None:
    """Create tables if absent. Idempotent, and safe to call on every start."""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()