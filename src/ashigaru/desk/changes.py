"""Changed since you looked: each item someone else's write touched after the caller's own seen mark.

Every identity keeps its own cursor, the mark `seen:<identity>`, an event seq like the sweep mark.
GET /v1/changes reads after it and leaves it; POST /v1/changes (`ashigaru changes --ack`) then moves
it to the window's `until`, only forward, so nothing is lost or repeated between two looks."""

import json
import sqlite3
from typing import Any

from . import dispatch, store
from . import outcomes as served

SEEN = "seen:"
QUIET = frozenset({"updated_at", "version", "extra"})
"""Patch keys that say nothing a reader acts on."""
CHANGED = """
    SELECT e.seq AS event_seq, e.item_id, e.at, e.actor, e.patch,
           e.seq = (SELECT min(seq) FROM events f WHERE f.item_id = e.item_id) AS first
    FROM events e
    WHERE e.seq > :after AND e.seq <= :until AND e.actor != :me
    ORDER BY e.seq
    LIMIT :limit
"""
"""Events in the window that the reader did not write: their own writes are not news to them."""
ITEMS = "SELECT * FROM items WHERE id IN (SELECT value FROM json_each(:ids))"


def mark(identity: str) -> str:
    return f"{SEEN}{identity}"


def _touched(rows: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    """Each item's events folded into one: new when its first event is among them, the fields they
    set, their writers in order, how many, and when the last was."""
    touched: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = touched.setdefault(
            row["item_id"], {"new": bool(row["first"]), "fields": set(), "by": [], "writes": 0}
        )
        entry["fields"] |= json.loads(row["patch"]).keys() - QUIET
        if row["actor"] not in entry["by"]:
            entry["by"].append(row["actor"])
        entry["writes"] += 1
        entry["at"] = row["at"]
    return touched


def read(conn: sqlite3.Connection, identity: str, since: str | None = None) -> dict[str, Any]:
    """The items others changed after the reader's mark (else a day back, or from `since`), each
    once, last changed first: whether it is new, which fields moved, who moved them, how many writes,
    and its NEXT now. Like the catch-up, it reads at most QUEUE_LIMIT events (dispatch.window).
    Raises ValueError for a `since` that is not a time."""
    after, read_from = dispatch.cursor(conn, since, mark(identity))
    rows, truncated, until = dispatch.window(conn, CHANGED, after, me=identity)
    touched = _touched(rows)
    items = [store.decode(row) for row in conn.execute(ITEMS, {"ids": json.dumps(list(touched))})]
    served.attach(conn, items)
    now = store.now()
    found = [
        {
            **item,
            **touched[item["id"]],
            "fields": sorted(touched[item["id"]]["fields"]),
            "step": served.step(item, now),
        }
        for item in items
    ]
    found.sort(key=lambda row: (row["at"], row["id"]), reverse=True)
    seen = store.get_mark(conn, mark(identity))
    return {
        "since": read_from,
        "seen_at": seen["updated_at"] if seen and since is None else None,
        "after": after,
        "until": until,
        "items": found,
        "truncated": truncated,
    }


def ack(conn: sqlite3.Connection, identity: str) -> dict[str, Any]:
    """A look, then the reader's mark moves to its `until`; `acked` is false when it was already
    there or a concurrent ack moved it further."""
    found = read(conn, identity)
    return {**found, "acked": store.advance_mark(conn, mark(identity), found["until"], identity)}
