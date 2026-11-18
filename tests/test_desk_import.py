"""The ledger import against a real ledger: every line lands, newest wins, a re-run changes nothing."""

import json
import sqlite3
from http.client import CONFLICT
from typing import Any

from conftest import LEDGER, Keys
from fastapi.testclient import TestClient

from ashigaru.desk import importer, store
from ashigaru.desk.model import CLOSED, Item, subject_key

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


def unkeyed(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [row[:3] + row[4:] for row in dump(conn)]


def keyed(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT id, subject_key FROM items WHERE subject_key IS NOT NULL").fetchall())


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
    assert unkeyed(conn) == unkeyed(whole)
    # A key given while an item was open stays when a later line closes it, as a Desk item's does.
    assert keyed(conn).items() >= keyed(whole).items()


def test_untimed_lines_take_the_ledgers_time_not_the_clock(conn: sqlite3.Connection) -> None:
    lines = ['{"id":"a1","state":"queued","at":"2026-11-15T02:40Z"}\n', '{"id":"a2","task":"no time"}\n']
    importer.import_ledger(conn, lines)
    assert (store.get_item(conn, "a2") or {})["created_at"] == "2026-11-15T02:40:00Z"


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


OPEN_ISSUE = "https://github.com/crunchtools/example/issues/7"


def test_open_items_get_the_key_their_ref_implies(conn: sqlite3.Connection) -> None:
    summary = importer.import_ledger(conn, LINES)
    keys = keyed(conn)
    assert keys and sorted(summary.keyed) == sorted(keys)
    for item_id, key in keys.items():
        item = store.get_item(conn, item_id) or {}
        assert item["state"] not in CLOSED and key == subject_key(item["ref"])
    closed_with_ref = [i for i in store.list_items(conn) if i["ref"] and i["state"] in CLOSED]
    assert closed_with_ref and all(item["subject_key"] is None for item in closed_with_ref)


def test_keying_leaves_the_items_history_and_order_alone(conn: sqlite3.Connection) -> None:
    importer.import_ledger(conn, LINES)
    for item_id in keyed(conn):
        events = conn.execute("SELECT at, source FROM events WHERE item_id = ? ORDER BY seq", (item_id,))
        *lines, backfill = events.fetchall()
        assert backfill["source"] == "backfill" and all(line["source"] == "ledger" for line in lines)
        assert backfill["at"] == (store.get_item(conn, item_id) or {})["updated_at"]


def test_a_reimport_backfills_keys_and_never_overwrites_one(conn: sqlite3.Connection) -> None:
    importer.import_ledger(conn, LINES)
    want = keyed(conn)
    first, second, *_ = want
    conn.execute("UPDATE items SET subject_key = NULL WHERE id != ?", (first,))
    conn.execute("UPDATE items SET subject_key = 'kept-by-hand' WHERE id = ?", (first,))
    summary = importer.import_ledger(conn, LINES)
    assert summary.applied == 0 and first not in summary.keyed and second in summary.keyed
    assert keyed(conn) == {**want, first: "kept-by-hand"}
    assert importer.import_ledger(conn, LINES).keyed == []


def test_one_ref_keys_only_its_newest_open_item(conn: sqlite3.Connection) -> None:
    lines = [
        f'{{"id":"a1","task":"old","ref":"{OPEN_ISSUE}","state":"done","at":"2026-11-15T01:00Z"}}\n',
        f'{{"id":"a2","task":"open","ref":"{OPEN_ISSUE}","state":"queued","at":"2026-11-15T02:00Z"}}\n',
        f'{{"id":"a3","task":"newer","ref":"{OPEN_ISSUE}/","state":"working","at":"2026-11-15T03:00Z"}}\n',
        '{"id":"a4","task":"rt","ref":"https://rt.example/Ticket/Display.html?id=1500","at":"2026-11-15T03:00Z"}\n',
    ]
    importer.import_ledger(conn, lines)
    assert keyed(conn) == {"a3": "crunchtools/example#7", "a4": "RT#1500"}


def test_a_key_another_item_holds_is_left_alone(conn: sqlite3.Connection) -> None:
    with store.transaction(conn):
        store.insert_item(conn, {"id": "a9", "subject_key": "crunchtools/example#7"}, "Josui Prime")
    importer.import_ledger(conn, [f'{{"id":"a1","task":"t","ref":"{OPEN_ISSUE}"}}\n'])
    assert keyed(conn) == {"a9": "crunchtools/example#7"}


def test_a_sweep_after_the_import_is_refused_as_a_duplicate(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    importer.import_ledger(conn, [f'{{"id":"a1","task":"t","ref":"{OPEN_ISSUE}"}}\n'])
    josui = keys["Josui Prime"]
    body = {"task": "again", "ref": OPEN_ISSUE, "subject_key": OPEN_ISSUE}
    assert client.post("/v1/items", headers=josui, json=body).status_code == CONFLICT
    for sent, stored in {
        " https://github.com/crunchtools/example/pull/8/ ": "crunchtools/example#8",
        "crunchtools/example#9": "crunchtools/example#9",
        "example:disk-full": "example:disk-full",
    }.items():
        created = client.post("/v1/items", headers=josui, json={"task": "t", "subject_key": sent})
        assert created.json()["subject_key"] == stored


def test_subject_keys_have_one_form() -> None:
    same = {
        "crunchtools/ashigaru#5": [
            "https://github.com/crunchtools/ashigaru/issues/5",
            "https://github.com/crunchtools/ashigaru/issues/5/",
            "https://github.com/crunchtools/ashigaru/pull/5",
            "https://github.com/crunchtools/ashigaru/pull/5/files",
            "http://www.github.com/crunchtools/ashigaru/issues/5#issuecomment-1",
            " crunchtools/ashigaru#5 ",
        ],
        "RT#1500": [
            "RT#1500",
            "rt #1500",
            "RT 1500",
            "https://rt.crunchtools.com/Ticket/Display.html?id=1500",
            "https://rt.crunchtools.com/Ticket/Display.html?id=1500#txn-9",
        ],
    }
    for want, forms in same.items():
        assert {subject_key(form) for form in forms} == {want}
    for not_a_ref in ("example:disk-full", "https://example.com/issues/5", "a12", "  ", None):
        assert subject_key(not_a_ref) is None
