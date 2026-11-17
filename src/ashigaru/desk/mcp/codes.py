"""MCP codes: the secret that ends a hosted MCP URL and names the identity calling through it.

A code is `<code_id>_<secret>`, stored the way API keys are: the code id is the
lookup, only the secret's SHA-256 digest is kept, and the comparison is
timing-safe. Whoever holds the URL is that identity, so a code is minted by an
admin, like a key, and revoked by an admin or its owner.
"""

import hmac
import secrets
import sqlite3
from typing import Any

from .. import auth, store
from ..model import Role

INSERT = """
INSERT INTO mcp_codes (code_id, identity, secret_sha256, label, created_at)
VALUES (:id, :identity, :digest, :label, :at)
"""
GET = "SELECT code_id, identity, label, created_at, revoked_at, last_used_at FROM mcp_codes WHERE code_id = ?"
LIST = """
SELECT code_id, identity, label, created_at, revoked_at, last_used_at FROM mcp_codes
WHERE identity = ? ORDER BY created_at, code_id
"""
UNKNOWN = auth.digest("")
"""Compared against when the code id is unknown, so a miss costs what a wrong secret does."""


def mint(conn: sqlite3.Connection, identity: str, label: str | None = None) -> tuple[str, str]:
    """Store a new code for `identity`. Returns (code_id, code); the code is never stored."""
    code_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    row = {
        "id": code_id,
        "identity": identity,
        "digest": auth.digest(secret),
        "label": label,
        "at": store.now(),
    }
    conn.execute(INSERT, row)
    return code_id, f"{code_id}_{secret}"


def get(conn: sqlite3.Connection, code_id: str) -> dict[str, Any] | None:
    row = conn.execute(GET, (code_id,)).fetchone()
    return dict(row) if row else None


def list_codes(conn: sqlite3.Connection, identity: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(LIST, (identity,))]


def revoke(conn: sqlite3.Connection, code_id: str) -> None:
    conn.execute(
        "UPDATE mcp_codes SET revoked_at = ? WHERE code_id = ? AND revoked_at IS NULL", (store.now(), code_id)
    )


def authenticate(conn: sqlite3.Connection, code: str) -> auth.Caller | None:
    """The caller a code belongs to, or None for a malformed, unknown, revoked or disabled one."""
    code_id, _, secret = code.partition("_")
    if not code_id or not secret:
        return None
    row = conn.execute(
        "SELECT c.secret_sha256, c.last_used_at, i.name, i.role"
        " FROM mcp_codes c JOIN identities i ON i.name = c.identity"
        " WHERE c.code_id = ? AND c.revoked_at IS NULL AND i.disabled_at IS NULL",
        (code_id,),
    ).fetchone()
    matched = hmac.compare_digest(row["secret_sha256"] if row else UNKNOWN, auth.digest(secret))
    if row is None or not matched:
        return None
    if row["last_used_at"] is None or row["last_used_at"] < auth.stale_before():
        conn.execute("UPDATE mcp_codes SET last_used_at = ? WHERE code_id = ?", (store.now(), code_id))
    return auth.Caller(name=row["name"], role=Role(row["role"]), key_id=f"mcp:{code_id}")
