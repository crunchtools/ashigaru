"""The ledger import against a real ledger: every line lands, newest wins, a re-run changes nothing."""

import json
import sqlite3
from typing import Any

from conftest import LEDGER, Keys
from fastapi.testclient import TestClient

from ashigaru.desk import importer, store
from ashigaru.desk.model import Item

LINES = LEDGER.read_text().splitlines(keepends=True)
LINE_COUNT, ITEM_COUNT = 816, 217
A1_LINES = 4


def fold() -> dict[str, dict[str, Any]]:
    """The ledger the naive way: newest value of each key per id; wrapping is not a state."""
    items: dict[str, dict[str, Any]] = {}
    for line in map(json.loads, filter(str.strip, LINES)):
        state = line.pop("state", None)
        merged = items.setdefault(line["id"], {})
        merged.update(line)
        if state and state != "wrapping":
            merged["state"] = state
    return items


def dump(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]


def test_every_line_applies_and_every_item_is_valid(conn: sqlite3.Connection) -> None:
    summary = importer.import_ledger(conn, LINES)
    expected = fold()
    assert (summary.lines, summary.applied, summary.skipped) == (LINE_COUNT, LINE_COUNT, 0)
    assert len(summary.created) == len(expected) == ITEM_COUNT
    assert conn.execute("SELECT count(*) FROM events WHERE source = 'ledger'").fetchone()[0] == LINE_COUNT
    for row in store.list_items(conn):
        Item.model_validate(row)


def test_newest_value_of_each_field_wins(conn: sqlite3.Connection) -> None:
    importer.import_ledger(conn, LINES)
    for item_id, want in fold().items():
        got = store.get_item(conn, item_id)
        assert got is not None
        assert got["state"] == want.get("state", "queued"), item_id
        assert got["task"] == want.get("task", ""), item_id
        for key in ("seat", "result", "learned", "done_when", "note"):
            assert got[key] == want.get(key), (item_id, key)
        assert got["memorized"] is bool(want.get("memorized")), item_id
        assert got["after"] == (want.get("after") or []), item_id


def test_rerun_is_a_no_op(conn: sqlite3.Connection) -> None:
    importer.import_ledger(conn, LINES)
    before = dump(conn)
    summary = importer.import_ledger(conn, LINES)
    assert (summary.applied, summary.skipped) == (0, LINE_COUNT)
    assert dump(conn) == before


def test_a_grown_ledger_applies_only_new_lines(conn: sqlite3.Connection, tmp_path: Any) -> None:
    importer.import_ledger(conn, LINES[:400])
    summary = importer.import_ledger(conn, LINES)
    assert (summary.applied, summary.skipped) == (LINE_COUNT - 400, 400)
    whole = store.connect(tmp_path / "whole.db")
    store.migrate(whole)
    importer.import_ledger(whole, LINES)
    assert dump(conn) == dump(whole)


def test_ledger_shapes_map_onto_columns(conn: sqlite3.Connection) -> None:
    importer.import_ledger(conn, LINES)

    def get(item_id: str) -> dict[str, Any]:
        return store.get_item(conn, item_id) or {}

    assert get("a1")["assigned_at"] == "2026-11-15T02:40:00Z"
    assert get("a1")["created_at"] == "2026-11-15T02:40:00Z"
    assert get("a106")["after"] == ["a88"] and get("a107")["lane"] == "exedore-max"
    assert get("a107")["after"] == []  # cleared by a later line, not kept from the first
    assert get("a121")["blocked_reason"]  # the ledger's `blocker`
    assert get("a64")["extra"]["ref_rt"]
    assert get("a198")["extra"]["closed"] == "rolled"
    assert get("a119")["wrap"] == "cleared" and get("a119")["cleared_at"] == "2026-11-17T04:57:00Z"
    assert get("a144")["wrap"] == "cleared"
    assert {item["extra"].get("lane") for item in store.list_items(conn)} >= {"desk"}
    assert not any(item["check_after"] == "" for item in store.list_items(conn))
    wrapping = [i for i in store.list_items(conn) if i["wrap"] == "wrapping"]
    assert all(item["state"] != "wrapping" for item in wrapping)


def test_imported_ledger_serves_the_check_view(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    importer.import_ledger(conn, LINES)
    rows = client.get("/v1/check", headers=keys["ci"]).json()
    assert rows and all(row["wrap"] != "cleared" for row in rows)
    states = [row["state"] for row in rows]
    assert states == sorted(states, key=lambda state: state != "blocked")
    assert len(client.get("/v1/check?all=true", headers=keys["ci"]).json()) == ITEM_COUNT
    assert len(client.get("/v1/items/a1/events", headers=keys["ci"]).json()) == A1_LINES
    first = client.get("/v1/items/a1/events?limit=1", headers=keys["ci"]).json()
    rest = client.get(f"/v1/items/a1/events?after={first[0]['seq']}", headers=keys["ci"]).json()
    assert len(first) == 1 and len(rest) == A1_LINES - 1
    created = client.post("/v1/items", headers=keys["Josui Prime"], json={"task": "next one"}).json()
    assert created["id"] == "a" + str(max(int(item_id[1:]) for item_id in fold()) + 1)
