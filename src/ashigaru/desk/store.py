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
    """
    CREATE TABLE ci_checks (
        repo TEXT NOT NULL,
        sha TEXT NOT NULL,
        key TEXT NOT NULL,
        name TEXT NOT NULL,
        verdict TEXT NOT NULL,
        run TEXT NOT NULL,
        url TEXT,
        source TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY (repo, sha, key)
    );
    CREATE INDEX ci_checks_received ON ci_checks(received_at);

    CREATE TABLE deliveries (
        source TEXT NOT NULL,
        digest TEXT NOT NULL,
        event TEXT NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY (source, digest)
    );
    CREATE INDEX deliveries_received ON deliveries(received_at);
    """,
    """
    CREATE TABLE marks (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL
    );
    """,
    # How much of the session's running cost is already in spend: a row with billing booked all of
    # it, a row without booked none, and that backlog is booked once billing is known.
    """
    ALTER TABLE seats ADD COLUMN session_booked_usd REAL NOT NULL DEFAULT 0;
    UPDATE seats SET session_booked_usd = session_cost_usd WHERE billing IS NOT NULL;
    """,
    # What a seat last said before it went idle, for NEEDS YOU; a new turn or a /clear drops it.
    """
    ALTER TABLE seats ADD COLUMN waiting_on TEXT;
    """,
    # The forge's own state of each subject, and the PR that carries a story's work.
    """
    CREATE TABLE subjects (
        subject_key TEXT PRIMARY KEY,
        repo TEXT,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        head_sha TEXT,
        draft INTEGER,
        review TEXT,
        title TEXT,
        author TEXT,
        labels TEXT NOT NULL DEFAULT '[]',
        source TEXT NOT NULL,
        source_updated_at TEXT,
        seen_at TEXT NOT NULL
    );
    ALTER TABLE items ADD COLUMN pr TEXT;
    ALTER TABLE items ADD COLUMN landed_at TEXT;
    CREATE INDEX items_pr ON items(pr) WHERE pr IS NOT NULL;
    """,
    # Each run of the sweep, the timer that reads the forge back in case a webhook was missed.
    """
    CREATE TABLE sweeps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        subjects INTEGER NOT NULL DEFAULT 0,
        new INTEGER NOT NULL DEFAULT 0,
        closed INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        outsiders TEXT NOT NULL DEFAULT '[]',
        skipped TEXT
    );
    CREATE INDEX sweeps_finished ON sweeps(finished_at);
    """,
    # When seat reconcile, not the seat's own hook, last ended its turn.
    """
    ALTER TABLE seats ADD COLUMN turn_reconciled_at TEXT;
    """,
)

ITEM_DEFAULTS: dict[str, Any] = {
    "kind": "story", "parent": None, "subject_key": None, "task": "", "done_when": None, "ref": None,
    "repo": None, "lane": None, "seat": None, "mode": "auto", "after": [], "check_after": None,
    "result": None, "next": None, "learned": None, "note": None, "blocked_reason": None, "memorized": False,
    "verified_by": None, "priority": 0, "wrap": None, "filed_by": None, "filed_under": None,
    "assigned_at": None, "queued_at": None, "closed_at": None, "cleared_at": None, "expires_at": None,
    "extra": {}, "pr": None, "landed_at": None,
}  # fmt: skip
"""Every column but the four insert_item always sets: id, state, created_at, updated_at."""

INSERT_ITEM = """
    INSERT INTO items (
        id, kind, parent, subject_key, task, done_when, ref, repo, lane, seat, mode, state, after,
        check_after, result, next, learned, note, blocked_reason, memorized, verified_by, priority, wrap,
        filed_by, filed_under, created_at, updated_at, assigned_at, queued_at, closed_at, cleared_at,
        expires_at, extra, pr, landed_at
    ) VALUES (
        :id, :kind, :parent, :subject_key, :task, :done_when, :ref, :repo, :lane, :seat, :mode, :state,
        :after, :check_after, :result, :next, :learned, :note, :blocked_reason, :memorized, :verified_by,
        :priority, :wrap, :filed_by, :filed_under, :created_at, :updated_at, :assigned_at, :queued_at,
        :closed_at, :cleared_at, :expires_at, :extra, :pr, :landed_at
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
        expires_at = :expires_at, extra = :extra, pr = :pr, landed_at = :landed_at
    WHERE id = :id
"""

LIST_ITEMS = """
    SELECT * FROM items
    WHERE (:state IS NULL OR state IN (SELECT value FROM json_each(:state)))
      AND (:seat IS NULL OR seat = :seat)
      AND (:lane IS NULL OR lane = :lane) AND (:kind IS NULL OR kind = :kind)
      AND (:mode IS NULL OR mode = :mode) AND (:parent IS NULL OR parent = :parent)
      AND (:subject_key IS NULL OR subject_key = :subject_key) AND (:ref IS NULL OR ref = :ref)
      AND (:learned IS NULL OR (lower(trim(coalesce(learned, ''))) NOT IN ('', 'none')) = :learned)
      AND (:memorized IS NULL OR memorized = :memorized)
    ORDER BY updated_at DESC, id DESC
    LIMIT :limit OFFSET :offset
"""

OPEN = "coalesce(wrap, '') != 'cleared' AND state NOT IN ('done', 'wontdo', 'abandoned')"
CHECK_ORDER = "ORDER BY state = 'blocked' DESC, updated_at DESC, id DESC LIMIT :limit"
STATE_SINCE = """(
    SELECT max(e.at) FROM events e WHERE e.item_id = items.id AND json_type(e.patch, '$.state') IS NOT NULL
) AS since"""
"""When the item entered its state: the last event that set one."""
CHECK_ACTIVE = f"SELECT *, {STATE_SINCE} FROM items WHERE kind != 'outcome' AND {OPEN} {CHECK_ORDER}"
CHECK_ALL = f"SELECT *, {STATE_SINCE} FROM items WHERE kind != 'outcome' {CHECK_ORDER}"
"""The check table: work, so never an outcome, which `outcomes` shows."""
COUNT_OPEN = f"SELECT count(*) FROM items WHERE kind != 'outcome' AND {OPEN}"
"""Open work: an outcome is what work serves, not work."""

UPSERT_SEAT = """
    INSERT INTO seats (
        name, lane, status, owner, turn, task, item, since, updated_at, model, billing, context_used,
        context_window, session_id, session_tokens, session_cost_usd, session_booked_usd, turn_started_at,
        waiting_on, turn_reconciled_at
    ) VALUES (
        :name, :lane, :status, :owner, :turn, :task, :item, :since, :updated_at, :model, :billing,
        :context_used, :context_window, :session_id, :session_tokens, :session_cost_usd, :session_booked_usd,
        :turn_started_at, :waiting_on, :turn_reconciled_at
    )
    ON CONFLICT (name) DO UPDATE SET
        lane = excluded.lane, status = excluded.status, owner = excluded.owner, turn = excluded.turn,
        task = excluded.task, item = excluded.item, since = excluded.since, updated_at = excluded.updated_at,
        model = excluded.model, billing = excluded.billing, context_used = excluded.context_used,
        context_window = excluded.context_window, session_id = excluded.session_id,
        session_tokens = excluded.session_tokens, session_cost_usd = excluded.session_cost_usd,
        session_booked_usd = excluded.session_booked_usd, turn_started_at = excluded.turn_started_at,
        waiting_on = excluded.waiting_on, turn_reconciled_at = excluded.turn_reconciled_at
"""

SEAT_DEFAULTS: dict[str, Any] = {
    "lane": None, "status": "free", "owner": None, "turn": "idle", "task": None, "item": None,
    "model": None, "billing": None, "context_used": None, "context_window": None, "session_id": None,
    "session_tokens": None, "session_cost_usd": 0.0, "session_booked_usd": 0.0, "turn_started_at": None,
    "waiting_on": None, "turn_reconciled_at": None,
}  # fmt: skip

RELEASE_SEAT = """
    UPDATE seats SET status = 'free', owner = NULL, item = NULL, since = :at, updated_at = :at
    WHERE item = :item
"""

# Scott's hold follows the mode of the item a seat holds: `manual` hands it to him, `auto` to Josui.
MANUAL_OWNER = "Scott"
HOLD_SEAT = "UPDATE seats SET owner = :owner, updated_at = :at WHERE item = :item"

WRITE_CHECK = """
    INSERT INTO ci_checks (repo, sha, key, name, verdict, run, url, source, updated_at, received_at)
    VALUES (:repo, :sha, :key, :name, :verdict, :run, :url, :source, :updated_at, :received_at)
    ON CONFLICT (repo, sha, key) DO UPDATE SET
        name = excluded.name, verdict = excluded.verdict, run = excluded.run, url = excluded.url,
        source = excluded.source, updated_at = excluded.updated_at, received_at = excluded.received_at
    WHERE excluded.run > ci_checks.run
       OR (excluded.run = ci_checks.run AND (
           excluded.updated_at > ci_checks.updated_at
           OR (excluded.updated_at = ci_checks.updated_at AND ci_checks.verdict = 'pending')))
"""
"""A newer run (or re-run) always wins and an older one never does. Within one run the newest forge
time wins, so a late `in_progress` never undoes a `completed`; at the same second a verdict beats
pending."""

DELIVERY_TTL = timedelta(days=7)
"""How long a delivery id is remembered. Processing is idempotent, so an older redelivery is harmless."""
CI_TTL = timedelta(days=30)
"""How long a commit's checks are kept after their last delivery. Nobody waits on a month-old commit."""

ITEM_FILTERS = (
    "state", "seat", "lane", "kind", "mode", "parent", "subject_key", "ref", "learned", "memorized"
)  # fmt: skip
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
    conn: sqlite3.Connection, limit: int = -1, offset: int = 0, **filters: Any
) -> list[dict[str, Any]]:
    """Items matching every filter given (ITEM_FILTERS); newest first. `state` is one state or a list
    of them, any of which matches; `learned` true means a Learned line is recorded. A limit of -1 is
    SQLite's no limit."""
    params = {key: filters.get(key) for key in ITEM_FILTERS}
    state = params["state"]
    if state is not None:
        params["state"] = json.dumps([state] if isinstance(state, str) else list(state))
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


def _taken(conn: sqlite3.Connection, fields: dict[str, Any]) -> str | None:
    """Which item already holds the new item's id or subject_key, said so a client can find it."""
    if get_item(conn, fields["id"]) is not None:
        return f"id {fields['id']} is taken"
    key = fields.get("subject_key")
    row = conn.execute("SELECT id FROM items WHERE subject_key = ?", (key,)).fetchone() if key else None
    return f"subject_key {key} is taken by {row[0]}" if row else None


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
        raise Conflict(_taken(conn, fields) or str(error)) from error
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


STEERED_BY_OTHER = """
SELECT 1 FROM events WHERE item_id = ? AND actor != ?
AND (json_type(patch, '$.lane') IS NOT NULL OR json_type(patch, '$.mode') IS NOT NULL) LIMIT 1
"""


def last_event(conn: sqlite3.Connection, item_id: str) -> int:
    """The item's newest event seq: a version that moves with every write to the row."""
    row = conn.execute("SELECT MAX(seq) FROM events WHERE item_id = ?", (item_id,)).fetchone()
    return int(row[0] or 0)


def events(conn: sqlite3.Connection, item_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM events WHERE item_id = ? AND seq > ? ORDER BY seq LIMIT ?", (item_id, after, limit)
    )
    return [{**dict(row), "patch": json.loads(row["patch"])} for row in rows]


def steered_by_other(conn: sqlite3.Connection, item_id: str, actor: str) -> bool:
    """Whether anyone but `actor` ever set the item's lane or mode, null included."""
    row = conn.execute(STEERED_BY_OTHER, (item_id, actor)).fetchone()
    return row is not None


def count_open(conn: sqlite3.Connection) -> int:
    """How many items the check table shows without `show_all`."""
    return int(conn.execute(COUNT_OPEN).fetchone()[0])


def check(conn: sqlite3.Connection, show_all: bool = False, limit: int = -1) -> list[dict[str, Any]]:
    """The check table's items, whole, with `since` (when each entered its state) and
    `result_or_blocker`: blocked first, then newest, at most `limit` rows (-1: all). Only open items
    unless `show_all`: a closed item, or a cleared one, is history."""
    rows = conn.execute(CHECK_ALL if show_all else CHECK_ACTIVE, {"limit": limit})
    return [
        {
            **item,
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
    A new turn or a /clear (task back to null) drops `waiting_on`, unless the same write sets it.
    """
    at = now()
    current = get_seat(conn, name) or {**SEAT_DEFAULTS, "status": None, "since": at}
    stamps: dict[str, Any] = {}
    if "status" in fields and fields["status"] != current["status"]:
        stamps["since"] = at
    if fields.get("turn") == "active" and current["turn"] != "active":
        stamps["turn_started_at"] = at
    active = fields.get("turn") == "active"
    fresh = active or ("task" in fields and fields["task"] is None)
    seat = {
        **SEAT_DEFAULTS, **current, **({"waiting_on": None} if fresh else {}),
        **({"turn_reconciled_at": None} if active else {}), **fields, **stamps,
        "name": name, "updated_at": at,
    }  # fmt: skip
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


# Webhooks and CI


def record_delivery(conn: sqlite3.Connection, source: str, digest: str, event: str) -> bool:
    """Remember a delivery; the caller holds the transaction. False when it was already processed."""
    at = now()
    forget = (datetime.now(UTC) - DELIVERY_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("DELETE FROM deliveries WHERE received_at < ?", (forget,))
    cursor = conn.execute(
        "INSERT OR IGNORE INTO deliveries (source, digest, event, received_at) VALUES (?, ?, ?, ?)",
        (source, digest, event, at),
    )
    return cursor.rowcount == 1


def delivery_seen(conn: sqlite3.Connection, source: str, digest: str) -> bool:
    """Whether this signed delivery was already processed, read before any write to skip the forge's
    API for a replay; record_delivery stays the transactional guard."""
    found = conn.execute("SELECT 1 FROM deliveries WHERE source = ? AND digest = ?", (source, digest))
    return found.fetchone() is not None


def write_check(conn: sqlite3.Connection, check: dict[str, Any]) -> None:
    """Store one check and forget checks past CI_TTL; the caller holds the transaction."""
    forget = (datetime.now(UTC) - CI_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("DELETE FROM ci_checks WHERE received_at < ?", (forget,))
    conn.execute(WRITE_CHECK, {**check, "received_at": now()})


def ci_checks(conn: sqlite3.Connection, repo: str, sha: str) -> list[dict[str, Any]]:
    """The commit's checks within CI_TTL; an expired one reads as gone before a write deletes it."""
    since = (datetime.now(UTC) - CI_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT name, verdict, url, updated_at FROM ci_checks"
        " WHERE repo = ? AND sha = ? AND received_at >= ? ORDER BY name, key",
        (repo, sha, since),
    )
    return [dict(row) for row in rows]


def item_by_subject(conn: sqlite3.Connection, subject_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM items WHERE subject_key = ?", (subject_key,)).fetchone()
    return decode(row) if row else None


# Marks


def get_mark(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM marks WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def set_mark(conn: sqlite3.Connection, name: str, value: str, actor: str) -> None:
    conn.execute(
        "INSERT INTO marks (name, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,"
        " updated_by = excluded.updated_by",
        (name, value, now(), actor),
    )


def advance_mark(conn: sqlite3.Connection, name: str, seq: int, actor: str) -> bool:
    """Set the mark to an event seq unless it already holds a later one, in one statement, so two
    rounds racing can never move it back. A mark that is a time is replaced. True when it moved."""
    moved = conn.execute(
        "INSERT INTO marks (name, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,"
        " updated_by = excluded.updated_by"
        " WHERE marks.value GLOB '*[^0-9]*' OR marks.value = '' OR CAST(marks.value AS INTEGER) < ?",
        (name, str(seq), now(), actor, seq),
    )
    return moved.rowcount > 0
