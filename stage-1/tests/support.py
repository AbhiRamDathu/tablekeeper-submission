"""Shared test scaffolding: a real HTTP server on an ephemeral port and a tiny client.

The service is started in a thread inside this process, against a throwaway database file. Nothing
here imports pytest, httpx or anything else outside the standard library, which is what lets the
whole suite run on a bare Python install.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}
BOB = {"id": "u_bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob"}

BOOKING_LEAD_DAYS = 7


def all_week(opens: str = "18:00", closes: str = "23:00") -> list[dict]:
    return [{"weekday": d, "opens": opens, "closes": closes} for d in WEEKDAYS]


def restaurant(rid: str = "r_anker", *, name: str = "Zum Anker",
               timezone: str = "Europe/Berlin", slot_minutes: int = 30,
               reservation_duration_minutes: int = 90,
               cancellation_cutoff_minutes: int = 120,
               opening_hours: list[dict] | None = None,
               tables: list[dict] | None = None) -> dict:
    return {
        "id": rid, "name": name, "timezone": timezone, "slot_minutes": slot_minutes,
        "reservation_duration_minutes": reservation_duration_minutes,
        "cancellation_cutoff_minutes": cancellation_cutoff_minutes,
        "opening_hours": all_week() if opening_hours is None else opening_hours,
        "tables": tables if tables is not None else [
            {"id": "t_1", "label": "1", "capacity": 2},
            {"id": "t_2", "label": "2", "capacity": 4},
            {"id": "t_3", "label": "3", "capacity": 6},
        ],
    }


def fixture(*, users=None, restaurants=None, reservations=None) -> dict:
    return {
        "users": [ADA, BOB] if users is None else users,
        "restaurants": [restaurant()] if restaurants is None else restaurants,
        "reservations": reservations or [],
    }


def booking_date(timezone: str = "Europe/Berlin", lead: int = BOOKING_LEAD_DAYS) -> str:
    """A local calendar date `lead` days from now, at the restaurant.

    Relative to today on purpose: the service must never assume a particular calendar date, and a
    hard-coded date would keep passing after the fixtures moved past it.
    """
    return (dt.datetime.now(ZoneInfo(timezone)).date() + dt.timedelta(days=lead)).isoformat()


def weekday_of(date_str: str) -> str:
    return WEEKDAYS[dt.date.fromisoformat(date_str).weekday()]


def local(date_str: str, hhmm: str = "19:00") -> str:
    return f"{date_str}T{hhmm}"


def expected_slots(opens="18:00", closes="23:00", slot_minutes=30, duration=90) -> list[str]:
    """Every HH:MM where slot + duration <= closes, stepping from opens."""
    def mins(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    out, cursor, end = [], mins(opens), mins(closes)
    while cursor + duration <= end:
        out.append(f"{cursor // 60:02d}:{cursor % 60:02d}")
        cursor += slot_minutes
    return out


class Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.raw = body
        try:
            self.json = json.loads(body) if body else None
        except ValueError:
            self.json = None

    @property
    def code(self):
        try:
            return self.json["error"]["code"]
        except (TypeError, KeyError):
            return None

    def __repr__(self):
        return f"<Response {self.status} {self.raw[:200]!r}>"


class Client:
    """Minimal HTTP client. `token=None` means send no Authorization header."""

    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, *, json_body=None, params=None,
                headers=None, token=...) -> Response:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        hdrs = dict(headers or {})
        effective = self.token if token is ... else token
        if effective is not None:
            hdrs.setdefault("Authorization", f"Bearer {effective}")
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return Response(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read())

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def login(self, email: str, password: str) -> Response:
        return self.post("/auth/login", json_body={"email": email, "password": password},
                         token=None)

    def authenticate(self, email: str, password: str) -> "Client":
        resp = self.login(email, password)
        if resp.status != 200:
            raise AssertionError(f"login failed: {resp!r}")
        return Client(self.base_url, resp.json["token"])


@contextlib.contextmanager
def service():
    """Run the real server on an ephemeral port against a throwaway database."""
    from app import main as app_main
    from app import store

    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("TABLEKEEPER_DB")
        os.environ["TABLEKEEPER_DB"] = str(pathlib.Path(tmp) / "test.sqlite")
        try:
            store.ensure_schema()
            server = app_main.ThreadingHTTPServer(("127.0.0.1", 0), app_main.Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_address[1]}"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        finally:
            if previous is None:
                os.environ.pop("TABLEKEEPER_DB", None)
            else:
                os.environ["TABLEKEEPER_DB"] = previous


@contextlib.contextmanager
def world():
    """A reset fixture plus two authenticated clients, the booking date and the table map."""
    with service() as base_url:
        anon = Client(base_url)
        seeded = fixture()
        reset = anon.post("/_test/reset", json_body=seeded)
        if reset.status != 204:
            raise AssertionError(f"reset failed: {reset!r}")
        rid = seeded["restaurants"][0]["id"]
        node = type("World", (), {})()
        node.base_url = base_url
        node.fixture = seeded
        node.anon = anon
        node.rid = rid
        node.timezone = seeded["restaurants"][0]["timezone"]
        node.tables = {t["id"]: t for t in seeded["restaurants"][0]["tables"]}
        node.date = booking_date(node.timezone)
        node.ada = anon.authenticate(ADA["email"], ADA["password"])
        node.bob = anon.authenticate(BOB["email"], BOB["password"])
        yield node


def book(node, client=None, *, table_id="t_2", at="19:00", party_size=4, key=None, **extra):
    """Create a reservation, defaulting to a free table and a valid slot."""
    body = {
        "restaurant_id": node.rid,
        "table_id": table_id,
        "starts_at_local": local(node.date, at),
        "party_size": party_size,
    }
    body.update(extra)
    return (client or node.ada).post(
        "/reservations", json_body=body,
        headers={"Idempotency-Key": key or f"k-{at}-{table_id}-{id(body)}"},
    )