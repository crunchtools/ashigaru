"""One-time import of Josui's JSONL agents ledger.

The ledger is append-only. A line is a patch: the first line for an id carries
the brief, later ones carry only what changed ({"id", "memorized", "at"}). So
lines are folded in file order, field by field, and the newest value of each
field wins. Each line also lands in `events`, so the history comes across too.

Idempotent: a line is keyed by its number and its SHA-256, and a line already
imported is skipped. Re-running on the same file changes nothing; running on a
ledger that has grown applies only the new lines.

Ledger fields with no column of their own are kept, not dropped: `assigned`,
`queued`, `wrapped`, `closed` and `blocker` become timestamps, wrap-up and
blocked_reason; anything else, and any value outside the Desk's enums (a lane
of "desk", say), goes to `extra`.
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from . import store
from .model import Lane, Mode, State, Wrap

ACTOR = "ledger-import"
MINUTES = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\dZ$")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z$")
TEXT = ("task", "done_when", "ref", "seat", "result", "next", "learned", "note", "verified_by")
ENUMS: dict[str, type[Lane] | type[Mode] | type[State]] = {"lane": Lane, "mode": Mode, "state": State}
RENAMED = {"assigned": "assigned_at", "queued": "queued_at", "blocker": "blocked_reason"}
WHEN = ("at", "assigned", "queued", "wrapped", "closed")


@dataclass
class Summary:
    lines: int = 0
    applied: int = 0
    skipped: int = 0
    created: list[str] = field(default_factory=list)
    updated: set[str] = field(default_factory=set)


def stamp(value: Any) -> Any:
    """Ledger times are to the minute; the Desk's are to the second, so they sort together."""
    if isinstance(value, str) and MINUTES.match(value):
        return value[:-1] + ":00Z"
    return value


def _wrap_up(line: dict[str, Any], patch: dict[str, Any], extra: dict[str, Any]) -> None:
    if line.get("state") == "wrapping":
        patch["wrap"] = Wrap.WRAPPING
    if line.get("cleared") is True:
        patch["wrap"] = Wrap.CLEARED
    if "wrapped" in line:
        patch["wrap"] = Wrap.CLEARED
        patch["cleared_at"] = stamp(line["wrapped"])
    closed = line.get("closed")
    if isinstance(closed, str) and TIMESTAMP.match(closed):
        patch["closed_at"] = stamp(closed)
    elif closed == "cleared":
        patch["wrap"] = Wrap.CLEARED
    elif closed is not None:
        extra["closed"] = closed


def _enum(key: str, value: Any, patch: dict[str, Any], extra: dict[str, Any]) -> None:
    try:
        patch[key] = ENUMS[key](value)
    except ValueError:
        if value != "wrapping":
            extra[key] = value


def to_patch(line: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """One ledger line as (item fields, extra fields)."""
    patch: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in line.items():
        match key:
            case "id" | "at" | "cleared" | "wrapped" | "closed":
                pass  # the item's key, the event's time, and wrap-up, which _wrap_up reads
            case _ if key in TEXT:
                patch[key] = value
            case _ if key in ENUMS:
                _enum(key, value, patch, extra)
            case _ if key in RENAMED:
                patch[RENAMED[key]] = stamp(value)
            case "check_after":
                patch[key] = stamp(value) or None
            case "after":
                patch[key] = list(value or [])
            case "memorized":
                patch[key] = bool(value)
            case _:
                extra[key] = value
    _wrap_up(line, patch, extra)
    return patch, extra


def line_time(line: dict[str, Any]) -> str | None:
    for key in WHEN:
        value = line.get(key)
        if isinstance(value, str) and TIMESTAMP.match(value):
            return str(stamp(value))
    return None


def apply(conn: sqlite3.Connection, number: int, raw: str, summary: Summary) -> None:
    key = (number, hashlib.sha256(raw.encode()).hexdigest())
    if conn.execute("SELECT 1 FROM ledger_lines WHERE line_no = ? AND line_hash = ?", key).fetchone():
        summary.skipped += 1
        return
    line = json.loads(raw)
    patch, extra = to_patch(line)
    item = store.get_item(conn, line["id"])
    at = line_time(line) or (item["updated_at"] if item else None)
    if item is None:
        store.insert_item(conn, {"id": line["id"], **patch, "extra": extra, "at": at}, ACTOR, "ledger")
        summary.created.append(line["id"])
    else:
        if extra:
            patch["extra"] = {**item["extra"], **extra}
        store.update_item(conn, line["id"], {**patch, "at": at}, ACTOR, "ledger")
        summary.updated.add(line["id"])
    conn.execute("INSERT INTO ledger_lines (line_no, line_hash) VALUES (?, ?)", key)
    summary.applied += 1


def import_ledger(conn: sqlite3.Connection, lines: Iterable[str]) -> Summary:
    """Fold the ledger into the Desk in one transaction: all of it lands, or none of it."""
    summary = Summary()
    with store.transaction(conn):
        for number, raw in enumerate(lines, start=1):
            if raw.strip():
                summary.lines += 1
                apply(conn, number, raw.strip(), summary)
    return summary
