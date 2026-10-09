"""Unit 3 guard: the `tables` primary key is per-restaurant, and an older database is carried
forward rather than quietly left in the old shape.

§4 constrains a table id by nothing -- not by length, not by format, and not by belonging to one
restaurant only -- so `t_2` is a legal id for every restaurant in the model and two of them may own
it at once. That makes the key `(restaurant_id, id)` a requirement of the model rather than a
storage detail.

Which is why this guard exists. `ensure_schema()` runs on every process start, and
`CREATE TABLE IF NOT EXISTS` says nothing at all about a table that already exists, so the composite
key in `store.SCHEMA` only ever reaches a database *created* after the change. A shipped database,
or one on a volume that survives a restart, keeps `id` globally unique: every fixture the
specification permits still dies at reset on a uniqueness constraint, and the fix reads as landed
while doing nothing. That is a defect that can only be caught by asking a database written by the
*previous* version to start under this one.

A guard that has never been seen red is the same shape as the packaging guard's original bug, so
the first test builds the old shape by hand and asserts the carry-forward keeps its rows; the
second is the negative control, because `ensure_schema` is called on every start and the migration
must be a no-op once the table is already in the new shape.

Stdlib only, no server, no network. `store.connect()` resolves its path from `TABLEKEEPER_DB` at
call time, so every test here points that at its own throwaway file.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
import unittest

from app import store

#: The declaration this unit replaced: `id` a PRIMARY KEY on its own, so two restaurants cannot both
#: own `t_1`. Reproduced here rather than imported, because the point of the guard is the shape a
#: database written by the *previous* version carries.
PRE_RESTAURANT_KEY_DDL = """
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
"""


class RestaurantScopedTableKey(unittest.TestCase):
    """`tables` keyed `(restaurant_id, id)`, on a fresh database and on an upgraded one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "upgrade.sqlite"
        previous = os.environ.get("TABLEKEEPER_DB")
        os.environ["TABLEKEEPER_DB"] = str(self.db)

        def restore():
            if previous is None:
                os.environ.pop("TABLEKEEPER_DB", None)
            else:
                os.environ["TABLEKEEPER_DB"] = previous

        self.addCleanup(restore)

    def primary_key(self) -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            info = list(conn.execute("PRAGMA table_info(tables)"))
        finally:
            conn.close()
        # `pk` is the 1-based position in the primary key, 0 for a plain column.
        return [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]

    def tables_in(self) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [tuple(row) for row in conn.execute(
                "SELECT restaurant_id, id, label, capacity FROM tables ORDER BY rowid")]
        finally:
            conn.close()

    def names_of_every_table(self) -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            return [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        finally:
            conn.close()

    def write_the_old_shape(self) -> None:
        """A database as the previous version left it: one restaurant, two tables."""
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(PRE_RESTAURANT_KEY_DDL)
            conn.execute("INSERT INTO restaurants VALUES ('r_one', 'One', 'Europe/Berlin',"
                         " 30, 90, 120)")
            conn.execute("INSERT INTO restaurants VALUES ('r_two', 'Two', 'Europe/Berlin',"
                         " 30, 90, 120)")
            conn.execute("INSERT INTO tables VALUES ('t_1', 'r_one', '1', 2)")
            conn.execute("INSERT INTO tables VALUES ('t_2', 'r_one', '2', 4)")
            conn.commit()
        finally:
            conn.close()

    def test_an_old_shape_is_rebuilt_and_keeps_every_row(self):
        """The two halves of the carry-forward: the key widens, and nothing is lost doing it.

        Widening a primary key means rebuilding the table, and a rebuild is the one place a row can
        disappear. So the assertion is both -- the key is `(restaurant_id, id)`, *and* the two rows
        are still there with the columns they had.

        The last leg is the reason the rebuild exists at all: after it, the second restaurant can
        own `t_1`, which is the fixture §4 permits and which the old key made impossible. Without
        that leg this test would pass on a rebuild that quietly dropped every table.
        """
        self.write_the_old_shape()

        store.ensure_schema()

        self.assertEqual(self.primary_key(), ["restaurant_id", "id"])
        self.assertEqual(self.tables_in(),
                         [("r_one", "t_1", "1", 2), ("r_one", "t_2", "2", 4)])

        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO tables (restaurant_id, id, label, capacity)"
                         " VALUES ('r_two', 't_1', 'also 1', 6)")
            conn.commit()
        finally:
            conn.close()
        self.assertIn(("r_two", "t_1", "also 1", 6), self.tables_in(),
                      "a table id the model allows is still being refused as globally unique")

    def test_a_database_in_the_new_shape_is_left_alone(self):
        """The negative control, and the reason the migration reads its own shape first.

        `ensure_schema()` is called on every start and on every test, so the rebuild runs against
        databases that were created seconds earlier and are already correct. `rowid` is the
        observable: a rebuild renumbers rows, so the ids surviving two further starts is proof the
        table was not rebuilt again -- and the temporary table the rebuild works through is gone
        either way, which is the second half of "no window in which `tables` does not exist".
        """
        store.ensure_schema()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO restaurants (id, name, timezone, slot_minutes,"
                         " reservation_duration_minutes, cancellation_cutoff_minutes)"
                         " VALUES ('r_one', 'One', 'Europe/Berlin', 30, 90, 120)")
            conn.execute("INSERT INTO tables (restaurant_id, id, label, capacity)"
                         " VALUES ('r_one', 't_1', '1', 2)")
            conn.commit()
            before = [row[0] for row in conn.execute("SELECT rowid FROM tables")]
        finally:
            conn.close()
        self.assertEqual(self.primary_key(), ["restaurant_id", "id"])

        store.ensure_schema()
        store.ensure_schema()

        conn = sqlite3.connect(self.db)
        try:
            after = [row[0] for row in conn.execute("SELECT rowid FROM tables")]
        finally:
            conn.close()
        self.assertEqual(before, after, "the table was rebuilt although it was already in the "
                                        "new shape")
        self.assertEqual([name for name in self.names_of_every_table()
                          if name.startswith("tables__")], [])