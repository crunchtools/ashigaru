"""SQLite storage for the Desk: the schema, its migrations, and every read and write.

One connection per request. Writes that read-then-write (new ids, the one-child
rule) run under BEGIN IMMEDIATE so two callers cannot interleave. Every change
to an item also lands in `events`, so the ledger's history survives the import
and the API keeps adding to it.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .model import CLOSED, State, Wrap

PROPOSED_TTL = timedelta(days=14)

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE items (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'story',
        parent TEXT,
        subject_key TEXT,
        task TEXT NOT NULL DEFAULT '',
        done_when TEXT,
        ref TEXT,
        repo TEXT,
        lane TEXT,
        seat TEXT,
        mode TEXT NOT NULL DEFAULT 'auto',
        state TEXT NOT NULL,
        after TEXT NOT NULL DEFAULT '[]',
        check_after TEXT,
        result TEXT,
        next TEXT,
        learned TEXT,
        note TEXT,
        blocked_reason TEXT,
        memorized INTEGER NOT NULL DEFAULT 0,
        verified_by TEXT,
        priority INTEGER NOT NULL DEFAULT 0,
        wrap TEXT,
        filed_by TEXT,
        filed_under TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        assigned_at TEXT,
        queued_at TEXT,
        closed_at TEXT,
        cleared_at TEXT,
        expires_at TEXT,
        extra TEXT NOT NULL DEFAULT '{}'
    );
    CREATE UNIQUE INDEX items_subject_key ON items(subject_key) WHERE subject_key IS NOT NULL;
    CREATE INDEX items_state ON items(state, expires_at);
    CREATE INDEX items_seat ON items(seat);
    CREATE INDEX items_filed_under ON items(filed_under);
    CREATE INDEX items_number ON items(CAST(substr(id, 2) AS INTEGER))
        WHERE id GLOB 'a[0-9]*' AND id NOT GLOB 'a*[^0-9]*';

    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        source TEXT NOT NULL,
        patch TEXT NOT NULL
    );
    CREATE INDEX events_item ON events(item_id, seq);

    CREATE TABLE ledger_lines (
        line_no INTEGER NOT NULL,
        line_hash TEXT NOT NULL,
        PRIMARY KEY (line_no, line_hash)
    );

    CREATE TABLE seats (
        name TEXT PRIMARY KEY,
        lane TEXT,
        status TEXT NOT NULL DEFAULT 'free',
        owner TEXT,
        turn TEXT NOT NULL DEFAULT 'idle',
        task TEXT,
        item TEXT,
        since TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE identities (
        name TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        disabled_at TEXT
    );

    CREATE TABLE api_keys (
        key_id TEXT PRIMARY KEY,
        identity TEXT NOT NULL REFERENCES identities(name),
        secret_sha256 TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        last_used_at TEXT
    );
    """,
    """
    ALTER TABLE seats ADD COLUMN model TEXT;
    ALTER TABLE seats ADD COLUMN billing TEXT;
    ALTER TABLE seats ADD COLUMN context_used INTEGER;
    ALTER TABLE seats ADD COLUMN context_window INTEGER;
    ALTER TABLE seats ADD COLUMN session_id TEXT;
    ALTER TABLE seats ADD COLUMN session_tokens INTEGER;
    ALTER TABLE seats ADD COLUMN session_cost_usd REAL NOT NULL DEFAULT 0;
    ALTER TABLE seats ADD COLUMN turn_started_at TEXT;
    CREATE INDEX seats_pick ON seats(lane, status, since);
    CREATE INDEX items_parent ON items(parent);

    CREATE TABLE spend (
        billing TEXT NOT NULL,
        day TEXT NOT NULL,
        cost_usd REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (billing, day)
    );

    CREATE TABLE billing_limits (
        billing TEXT PRIMARY KEY,
        daily_usd REAL,
        monthly_usd REAL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE mcp_codes (
        code_id TEXT PRIMARY KEY,
        identity TEXT NOT NULL REFERENCES identities(name),
        secret_sha256 TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        last_used_at TEXT
    );
    CREATE INDEX mcp_codes_identity ON mcp_codes(identity);
    """,
)

ITEM_DEFAULTS: dict[str, Any] = {
    "kind": "story", "parent": None, "subject_key": None, "task": "", "done_when": None, "ref": None,
    "repo": None, "lane": None, "seat": None, "mode": "auto", "after": [], "check_after": None,
    "result": None, "next": None, "learned": None, "note": None, "blocked_reason": None, "memorized": False,
    "verified_by": None, "priority": 0, "wrap": None, "filed_by": None, "filed_under": None,
    "assigned_at": None, "queued_at": None, "closed_at": None, "cleared_at": None, "expires_at": None,
    "extra": {},
}  # fmt: skip
"""Every column but the four insert_item always sets: id, state, created_at, updated_at."""

INSERT_ITEM = """
    INSERT INTO items (
        id, kind, parent, subject_key, task, done_when, ref, repo, lane, seat, mode, state, after,
        check_after, result, next, learned, note, blocked_reason, memorized, verified_by, priority, wrap,
        filed_by, filed_under, created_at, updated_at, assigned_at, queued_at, closed_at, cleared_at,
        expires_at, extra
    ) VALUES (
        :id, :kind, :parent, :subject_key, :task, :done_when, :ref, :repo, :lane, :seat, :mode, :state,
        :after, :check_after, :result, :next, :learned, :note, :blocked_reason, :memorized, :verified_by,
        :priority, :wrap, :filed_by, :filed_under, :created_at, :updated_at, :assigned_at, :queued_at,
        :closed_at, :cleared_at, :expires_at, :extra
    )
"""

UPDATE_ITEM = """
    UPDATE items SET
        kind = :kind, parent = :parent, subject_key = :subject_key, task = :task, done_when = :done_when,
        ref = :ref, repo = :repo, lane = :lane, seat = :seat, mode = :mode, state = :state, after = :after,
        check_after = :check_after, result = :result, next = :next, learned = :learned, note = :note,
        blocked_reason = :blocked_reason, memorized = :memorized, verified_by = :verified_by,
        priority = :priority, wrap = :wrap, filed_by = :filed_by, filed_under = :filed_under,
        created_at = :created_at, updated_at = :updated_at, assigned_at = :assigned_at,
        queued_at = :queued_at, closed_at = :closed_at, cleared_at = :cleared_at,
        expires_at = :expires_at, extra = :extra
    WHERE id = :id
"""

LIST_ITEMS = """
    SELECT * FROM items
    WHERE (:state IS NULL OR state = :state) AND (:seat IS NULL OR seat = :seat)
      AND (:lane IS NULL OR lane = :lane) AND (:kind IS NULL OR kind = :kind)
      AND (:mode IS NULL OR mode = :mode) AND (:parent IS NULL OR parent = :parent)
    ORDER BY updated_at DESC, id DESC
    LIMIT :limit OFFSET :offset
"""

CHECK_ACTIVE = """
    SELECT * FROM items
    WHERE coalesce(wrap, '') != 'cleared' AND state NOT IN ('wontdo', 'abandoned')
    ORDER BY state = 'blocked' DESC, updated_at DESC, id DESC
"""

CHECK_ALL = "SELECT * FROM items ORDER BY state = 'blocked' DESC, updated_at DESC, id DESC"

UPSERT_SEAT = """
    INSERT INTO seats (
        name, lane, status, owner, turn, task, item, since, updated_at, model, billing, context_used,
        context_window, session_id, session_tokens, session_cost_usd, turn_started_at
    ) VALUES (
        :name, :lane, :status, :owner, :turn, :task, :item, :since, :updated_at, :model, :billing,
        :context_used, :context_window, :session_id, :session_tokens, :session_cost_usd, :turn_started_at
    )
    ON CONFLICT (name) DO UPDATE SET
        lane = excluded.lane, status = excluded.status, owner = excluded.owner, turn = excluded.turn,
        task = excluded.task, item = excluded.item, since = excluded.since, updated_at = excluded.updated_at,
        model = excluded.model, billing = excluded.billing, context_used = excluded.context_used,
        context_window = excluded.context_window, session_id = excluded.session_id,
        session_tokens = excluded.session_tokens, session_cost_usd = excluded.session_cost_usd,
        turn_started_at = excluded.turn_started_at
"""

SEAT_DEFAULTS: dict[str, Any] = {
    "lane": None, "status": "free", "owner": None, "turn": "idle", "task": None, "item": None,
    "model": None, "billing": None, "context_used": None, "context_window": None, "session_id": None,
    "session_tokens": None, "session_cost_usd": 0.0, "turn_started_at": None,
}  # fmt: skip

RELEASE_SEAT = """
    UPDATE seats SET status = 'free', owner = NULL, item = NULL, since = :at, updated_at = :at
    WHERE item = :item
"""

# Scott's hold follows the mode of the item a seat holds: `manual` hands it to him, `auto` to Josui.
MANUAL_OWNER = "Scott"
HOLD_SEAT = "UPDATE seats SET owner = :owner, updated_at = :at WHERE item = :item"

ITEM_FILTERS = ("state", "seat", "lane", "kind", "mode", "parent")
ITEM_JSON = ("after", "extra")
ITEM_BOOL = ("memorized",)
TIMESTAMP_FOR_STATE = {State.ASSIGNED: "assigned_at", State.QUEUED: "queued_at"}


class Conflict(Exception):
    """A write that would break a uniqueness rule (id, subject_key, one child)."""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'")
    if exists.fetchone() is None:
        return 0
    return int(conn.execute("SELECT coalesce(max(version), 0) FROM schema_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration past the recorded version, each in its own transaction."""
    version = schema_version(conn)
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with transaction(conn):
            for statement in filter(str.strip, script.split(";")):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (number, now()))
    return len(MIGRATIONS)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# Items


def decode(row: sqlite3.Row) -> dict[str, Any]:
    """An items row as the API's Item: JSON columns parsed, flags as bool."""
    item = dict(row)
    for key in ITEM_JSON:
        item[key] = json.loads(item[key])
    for key in ITEM_BOOL:
        item[key] = bool(item[key])
    return item


def _encode(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    for key in ITEM_JSON:
        out[key] = json.dumps(out[key] if out[key] is not None else ITEM_DEFAULTS[key])
    for key in ITEM_BOOL:
        out[key] = int(bool(out[key]))
    return out


def _unknown(fields: dict[str, Any]) -> None:
    unknown = set(fields) - set(ITEM_DEFAULTS) - {"id", "state", "created_at", "updated_at"}
    if unknown:
        raise ValueError(f"not item columns: {', '.join(sorted(unknown))}")


def get_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return decode(row) if row else None


def list_items(
    conn: sqlite3.Connection, limit: int = -1, offset: int = 0, **filters: str | None
) -> list[dict[str, Any]]:
    """Items equal to every filter given (state, seat, lane, kind, mode, parent); newest first.
    A limit of -1 is SQLite's no limit."""
    params = {key: filters.get(key) for key in ITEM_FILTERS}
    rows = conn.execute(LIST_ITEMS, {**params, "limit": limit, "offset": offset})
    return [decode(row) for row in rows]


def next_id(conn: sqlite3.Connection) -> str:
    """a<N+1> over the highest a<N> whose N is all digits (a12x is not a12), from the items_number index."""
    highest = conn.execute(
        "SELECT max(CAST(substr(id, 2) AS INTEGER)) FROM items INDEXED BY items_number"
        " WHERE id GLOB 'a[0-9]*' AND id NOT GLOB 'a*[^0-9]*'"
    ).fetchone()[0]
    return f"a{(highest or 0) + 1}"


def _stamp(fields: dict[str, Any], at: str) -> dict[str, Any]:
    """The timestamps a state or wrap change implies, unless the caller set them."""
    stamped = {"updated_at": at}
    state = fields.get("state")
    if state in TIMESTAMP_FOR_STATE:
        stamped[TIMESTAMP_FOR_STATE[state]] = at
    if state in CLOSED:
        stamped["closed_at"] = at
    if fields.get("wrap") == Wrap.CLEARED:
        stamped["cleared_at"] = at
    return {**stamped, **fields}


def _record(conn: sqlite3.Connection, item_id: str, actor: str, source: str, patch: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events (item_id, at, actor, source, patch) VALUES (?, ?, ?, ?, ?)",
        (item_id, patch.get("updated_at") or now(), actor, source, json.dumps(patch, default=str)),
    )


def insert_item(conn: sqlite3.Connection, fields: dict[str, Any], actor: str, source: str = "api") -> str:
    """Insert one item; the caller holds the transaction. Raises Conflict on a taken id or subject_key."""
    fields = dict(fields)
    at = fields.pop("at", None) or now()
    _unknown(fields)
    fields.setdefault("id", next_id(conn))
    fields.setdefault("state", State.QUEUED)
    fields.setdefault("created_at", at)
    fields = _stamp(fields, at)
    try:
        conn.execute(INSERT_ITEM, _encode({**ITEM_DEFAULTS, **fields}))
    except sqlite3.IntegrityError as error:
        raise Conflict(str(error)) from error
    _record(conn, fields["id"], actor, source, fields)
    return str(fields["id"])


def update_item(
    conn: sqlite3.Connection, item_id: str, fields: dict[str, Any], actor: str, source: str = "api"
) -> None:
    """Apply a patch to the stored row; the caller holds the transaction. KeyError if no such item."""
    fields = dict(fields)
    at = fields.pop("at", None) or now()
    _unknown(fields)
    current = get_item(conn, item_id)
    if current is None:
        raise KeyError(item_id)
    fields = _stamp(fields, at)
    try:
        conn.execute(UPDATE_ITEM, _encode({**current, **fields, "id": item_id}))
    except sqlite3.IntegrityError as error:
        raise Conflict(str(error)) from error
    _record(conn, item_id, actor, source, fields)
    closed = fields.get("state") in CLOSED or fields.get("wrap") == Wrap.CLEARED
    reassigned = "seat" in fields and fields["seat"] != current["seat"]
    if closed or reassigned:
        conn.execute(RELEASE_SEAT, {"at": at, "item": item_id})
    elif fields.get("mode", current["mode"]) != current["mode"]:
        owner = MANUAL_OWNER if fields["mode"] == "manual" else None
        conn.execute(HOLD_SEAT, {"owner": owner, "at": at, "item": item_id})


def file_child(conn: sqlite3.Connection, story: dict[str, Any], fields: dict[str, Any], actor: str) -> str:
    """desk_file: one proposed child per assignment of `story`, under the story's outcome."""
    since = story["assigned_at"] or story["created_at"]
    taken = conn.execute(
        "SELECT id FROM items WHERE filed_under = ? AND created_at >= ?", (story["id"], since)
    ).fetchone()
    if taken:
        raise Conflict(f"{story['id']} already filed {taken[0]} in this assignment")
    at = now()
    expires = (datetime.now(UTC) + PROPOSED_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
    child = {
        **fields,
        "state": State.PROPOSED,
        "parent": story["parent"],
        "repo": story["repo"],
        "filed_by": actor,
        "filed_under": story["id"],
        "expires_at": expires,
        "at": at,
    }
    return insert_item(conn, child, actor)


def expire_proposed(conn: sqlite3.Connection) -> list[str]:
    """Abandon proposed children past expires_at that Josui never promoted.

    Reads call this, so it writes only when something is due: the check is one
    lookup on the items_state index, outside any transaction.
    """
    at = now()
    due = "SELECT 1 FROM items WHERE state = 'proposed' AND expires_at < ? LIMIT 1"
    if conn.execute(due, (at,)).fetchone() is None:
        return []
    with transaction(conn):
        rows = conn.execute(
            "SELECT id FROM items WHERE state = ? AND expires_at IS NOT NULL AND expires_at < ?",
            (State.PROPOSED, at),
        ).fetchall()
        for row in rows:
            update_item(conn, row[0], {"state": State.ABANDONED, "note": "expired unpromoted"}, "desk")
    return [row[0] for row in rows]


def events(conn: sqlite3.Connection, item_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM events WHERE item_id = ? AND seq > ? ORDER BY seq LIMIT ?", (item_id, after, limit)
    )
    return [{**dict(row), "patch": json.loads(row["patch"])} for row in rows]


def check(conn: sqlite3.Connection, show_all: bool = False) -> list[dict[str, Any]]:
    """The /agents check table: blocked first, then newest. Hides wrapped and dead items by default."""
    rows = conn.execute(CHECK_ALL if show_all else CHECK_ACTIVE)
    return [
        {
            **{key: item[key] for key in ("id", "seat", "mode", "task", "state", "wrap", "next")},
            "result_or_blocker": item["blocked_reason"] if item["state"] == State.BLOCKED else item["result"],
        }
        for item in (decode(row) for row in rows)
    ]


# Seats


def list_seats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM seats ORDER BY name")]


def get_seat(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM seats WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def write_seat(conn: sqlite3.Connection, name: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Create or patch a seat row; the caller holds the transaction.

    `since` moves when `status` does, `turn_started_at` when the turn goes active; callers set neither.
    """
    at = now()
    current = get_seat(conn, name) or {**SEAT_DEFAULTS, "status": None, "since": at}
    stamps: dict[str, Any] = {}
    if "status" in fields and fields["status"] != current["status"]:
        stamps["since"] = at
    if fields.get("turn") == "active" and current["turn"] != "active":
        stamps["turn_started_at"] = at
    seat = {**SEAT_DEFAULTS, **current, **fields, **stamps, "name": name, "updated_at": at}
    seat["status"] = seat["status"] or SEAT_DEFAULTS["status"]
    conn.execute(UPSERT_SEAT, seat)
    return seat


# Identities and keys


def get_identity(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM identities WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def list_identities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM identities ORDER BY name")]


def set_role(conn: sqlite3.Connection, name: str, role: str) -> None:
    conn.execute("UPDATE identities SET role = ? WHERE name = ?", (role, name))


def create_identity(conn: sqlite3.Connection, name: str, role: str) -> dict[str, Any]:
    identity = {"name": name, "role": role, "created_at": now(), "disabled_at": None}
    try:
        conn.execute(
            "INSERT INTO identities (name, role, created_at) VALUES (:name, :role, :created_at)", identity
        )
    except sqlite3.IntegrityError as error:
        raise Conflict(f"identity {name!r} exists") from error
    return identity


def insert_key(conn: sqlite3.Connection, key_id: str, identity: str, digest: str, label: str | None) -> None:
    conn.execute(
        "INSERT INTO api_keys (key_id, identity, secret_sha256, label, created_at) VALUES (?, ?, ?, ?, ?)",
        (key_id, identity, digest, label, now()),
    )


def get_key(conn: sqlite3.Connection, key_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT key_id, identity, label, created_at, revoked_at, last_used_at FROM api_keys WHERE key_id = ?",
        (key_id,),
    ).fetchone()
    return dict(row) if row else None


def list_keys(conn: sqlite3.Connection, identity: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key_id, identity, label, created_at, revoked_at, last_used_at FROM api_keys"
        " WHERE identity = ? ORDER BY created_at",
        (identity,),
    )
    return [dict(row) for row in rows]


def revoke_key(conn: sqlite3.Connection, key_id: str) -> None:
    conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL", (now(), key_id)
    )


def key_owner(conn: sqlite3.Connection, key_id: str) -> sqlite3.Row | None:
    """The live key's digest and its identity, or None for an unknown, revoked or disabled one."""
    return conn.execute(
        "SELECT k.secret_sha256, k.last_used_at, i.name, i.role"
        " FROM api_keys k JOIN identities i ON i.name = k.identity"
        " WHERE k.key_id = ? AND k.revoked_at IS NULL AND i.disabled_at IS NULL",
        (key_id,),
    ).fetchone()


def touch_key(conn: sqlite3.Connection, key_id: str) -> None:
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE key_id = ?", (now(), key_id))
