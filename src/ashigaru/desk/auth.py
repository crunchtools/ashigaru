"""Per-identity API keys: minted once, stored as a SHA-256 digest, sent as a bearer token.

A token is `ash_<key_id>_<secret>`. The key id is the lookup; only the secret's
digest is stored, so a copy of the database holds no usable key. The secret is
32 random bytes, so a plain digest is enough: there is nothing to brute-force
that a slow password hash would protect.
"""

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass

from . import store
from .model import Role

PREFIX = "ash"


@dataclass(frozen=True)
class Caller:
    name: str
    role: Role
    key_id: str

    @property
    def admin(self) -> bool:
        return self.role == Role.ADMIN


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint(conn: sqlite3.Connection, identity: str, label: str | None = None) -> tuple[str, str]:
    """Store a new key for `identity`. Returns (key_id, token); the token is never stored."""
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    store.insert_key(conn, key_id, identity, digest(secret), label)
    return key_id, f"{PREFIX}_{key_id}_{secret}"


def authenticate(conn: sqlite3.Connection, token: str) -> Caller | None:
    """The caller a token belongs to, or None for a malformed, unknown, revoked or disabled one."""
    prefix, _, rest = token.partition("_")
    key_id, _, secret = rest.partition("_")
    if prefix != PREFIX or not key_id or not secret:
        return None
    row = store.key_owner(conn, key_id)
    if row is None or not hmac.compare_digest(row["secret_sha256"], digest(secret)):
        return None
    store.touch_key(conn, key_id)
    return Caller(name=row["name"], role=Role(row["role"]), key_id=key_id)
