"""Authentication for Tablekeeper Stage 1 (spec §6).

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user random salt and compared in constant
time. `secrets.compare_digest` is used rather than `==` so that a wrong password and a
nearly-right password are not distinguishable by how long the answer takes.

Validation is split deliberately, because the spec splits it: a field of the wrong JSON *type* is a
malformed request (400), while a field of the right type holding an unacceptable *value* is a
validation failure (422). Collapsing the two would make `{"email": 17}` and `{"email": "nope"}`
the same response, and the harness asserts they are not.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

__all__ = ["hash_password", "verify_password", "issue_token", "signup", "authenticate",
           "ValidationFailure", "MalformedRequest", "CredentialsTaken", "BadCredentials"]

_PBKDF2_ROUNDS = 120_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8


class MalformedRequest(Exception):
    """A field arrived with the wrong JSON type. §5 maps this to 400 malformed_request."""


class ValidationFailure(Exception):
    """A field had an acceptable type but an unacceptable value. §5 maps this to 422."""


class CredentialsTaken(Exception):
    """Signup for an address that already has an account. §6 maps this to 409 email_taken."""


class BadCredentials(Exception):
    """Login failed. Deliberately does not say whether it was the address or the password."""


def hash_password(password: str) -> str:
    """`pbkdf2_sha256$rounds$salt$digest`, all hex, so one column holds everything needed."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash. False on any malformed stored value."""
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                        bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


def _require_str(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str):
        raise MalformedRequest(f"{field} must be a string")
    return value


def validate_signup(body: object) -> tuple[str, str, str]:
    """Type-check, then value-check. Returns `(email, password, display_name)`."""
    if not isinstance(body, dict):
        raise MalformedRequest("request body must be a JSON object")
    email = _require_str(body, "email")
    password = _require_str(body, "password")
    display_name = _require_str(body, "display_name")

    if not _EMAIL_RE.match(email):
        raise ValidationFailure("email must look like local@domain")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailure("password must be at least 8 characters")
    return email, password, display_name


def signup(body: object, user_id: str) -> dict:
    """Create an account and return its token. Raises per the exceptions above."""
    from . import store

    email, password, display_name = validate_signup(body)
    try:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
                (user_id, email, hash_password(password), display_name),
            )
            token = issue_token()
            conn.execute("INSERT INTO tokens (token, user_id) VALUES (?, ?)", (token, user_id))
    except Exception as exc:  # sqlite3.IntegrityError, without importing sqlite3 for one name
        if "email" in str(exc).lower() or "unique" in str(exc).lower():
            raise CredentialsTaken(email) from exc
        raise
    # §6 answers 201 with {user_id, display_name, token}. The name is echoed from the value
    # `validate_signup` already checked rather than re-derived, so the response cannot disagree with
    # the column that was just written.
    return {"user_id": user_id, "email": email, "display_name": display_name, "token": token}


def authenticate(body: object) -> dict:
    """Exchange credentials for a token. Raises `BadCredentials` on any failure."""
    from . import store

    if not isinstance(body, dict):
        raise MalformedRequest("request body must be a JSON object")
    email = _require_str(body, "email")
    password = _require_str(body, "password")

    conn = store.connect()
    try:
        row = conn.execute("SELECT id, password_hash, display_name FROM users WHERE email = ?",
                           (email,)).fetchone()
    finally:
        conn.close()
    # One message for a missing account and for a wrong password, so the response cannot be used
    # to learn whether an address is registered. The email is deliberately not echoed back.
    if row is None or not verify_password(password, row["password_hash"]):
        raise BadCredentials("invalid email or password")
    token = issue_token()
    conn = store.connect()
    try:
        conn.execute("INSERT INTO tokens (token, user_id) VALUES (?, ?)", (token, row["id"]))
    finally:
        conn.close()
    # §6 answers 200 with the same three fields as signup. Login carries no display_name of its own,
    # so the name has to come off the stored row -- the same column `user_for_token` already selects.
    return {"user_id": row["id"], "email": email, "display_name": row["display_name"],
            "token": token}


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def user_for_token(token: str | None):
    """The user row for a bearer token, or `None` if the token is absent or unknown.

    §6 requires both cases to be indistinguishable to the caller, so a missing header and a
    fabricated token both simply return `None` and the endpoint answers 401 either way.
    """
    from . import store

    if not token:
        return None
    conn = store.connect()
    try:
        return conn.execute(
            "SELECT users.id AS id, users.email AS email, users.display_name AS display_name"
            " FROM tokens JOIN users ON users.id = tokens.user_id WHERE tokens.token = ?",
            (token,),
        ).fetchone()
    finally:
        conn.close()