"""`GET /v1/work` and `POST /v1/work` against the real Desk: the check round and the forge catch-up."""

import sqlite3
from datetime import UTC, datetime, timedelta
from http.client import CREATED, FORBIDDEN, NOT_FOUND, OK, UNPROCESSABLE_ENTITY
from typing import Any

import pytest
from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import dispatch, intake, store
from ashigaru.desk.forges import IntakeItem
from ashigaru.desk.model import Kind

JOSUI = "Josui Prime"
SEAT = "Exedore 1 - Max"
LONG_AGO = "2020-01-01T00:00:00Z"
SIX = 6
TWO = 2


def ago(**span: float) -> str:
    return dispatch.stamp(datetime.now(UTC) - timedelta(**span))


def seen(conn: sqlite3.Connection, number: int, at: str, **fields: Any) -> str:
    """An item the org webhook created at `at`, as intake writes it."""
    subject = IntakeItem(
        subject_key=f"crunchtools/x#{number}",
        title=f"title {number}",
        ref=f"https://github.com/crunchtools/x/issues/{number}",
        source="webhook",
        repo="crunchtools/x",
        kind=Kind.STORY,
        author="fatherlinux",
        **fields,
    )
    with store.transaction(conn):
        item_id = intake.upsert(conn, subject)
        assert item_id
        conn.execute("UPDATE items SET created_at = ?, updated_at = ? WHERE id = ?", (at, at, item_id))
    return item_id


def create(client: TestClient, keys: Keys, **body: Any) -> dict[str, Any]:
    made = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t"} | body)
    assert made.status_code == CREATED, made.text
    result: dict[str, Any] = made.json()
    return result


def work(client: TestClient, keys: Keys, query: str = "") -> dict[str, Any]:
    got = client.get(f"/v1/work{query}", headers=keys[SEAT])
    assert got.status_code == OK, got.text
    result: dict[str, Any] = got.json()
    return result


def test_work_hides_closed_and_cleared_items_unless_all(client: TestClient, keys: Keys) -> None:
    for state in ("done", "wontdo", "abandoned"):
        create(client, keys, task=state, state=state)
    cleared = create(client, keys, task="cleared", state="review")
    client.patch(f"/v1/items/{cleared['id']}", headers=keys[JOSUI], json={"wrap": "cleared"})
    create(client, keys, task="queued")
    blocked = create(client, keys, task="blocked", state="blocked", blocked_reason="why")
    assert [row["task"] for row in work(client, keys)["items"]] == ["blocked", "queued"]
    everything = work(client, keys, "?all=true")
    assert len(everything["items"]) == SIX and everything["items"][0]["id"] == blocked["id"]


def test_open_counts_open_rows_even_with_all(client: TestClient, keys: Keys) -> None:
    create(client, keys, state="done")
    create(client, keys, task="open")
    assert work(client, keys)["summary"]["open"] == 1
    everything = work(client, keys, "?all=true")
    assert len(everything["items"]) == TWO and everything["summary"]["open"] == 1


def test_up_counts_the_seats_that_are_not_missing(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    with store.transaction(conn):
        store.write_seat(conn, SEAT, {"status": "free"})
        store.write_seat(conn, "Old Seat", {"status": "free"})
        conn.execute("UPDATE seats SET updated_at = ? WHERE name = 'Old Seat'", (LONG_AGO,))
    summary = work(client, keys)["summary"]
    rows = {row["name"]: row["missing"] for row in work(client, keys)["seats"]}
    assert rows[SEAT] is False and rows["Old Seat"] is True
    assert summary["seats"] == len(rows) and summary["up"] == sum(not gone for gone in rows.values())


def test_check_hides_done_items_too(client: TestClient, keys: Keys) -> None:
    create(client, keys, task="finished", state="done")
    create(client, keys, task="open")
    shown = client.get("/v1/check", headers=keys[SEAT]).json()
    assert [row["task"] for row in shown] == ["open"]
    assert len(client.get("/v1/check?all=true", headers=keys[SEAT]).json()) == TWO


def test_catch_up_new_lists_intake_items_in_the_window(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    trusted = seen(conn, 1, ago(hours=2), author_writes=True, lane_hint="vertex")
    held = seen(conn, 2, ago(hours=1))
    seen(conn, 3, ago(days=3), author_writes=True)
    create(client, keys, task="by the api", subject_key="crunchtools/x#4")
    new = {row["id"]: row for row in work(client, keys)["catch_up"]["new"]}
    assert set(new) == {trusted, held}
    assert (new[trusted]["title"], new[trusted]["filed_by"], new[trusted]["held"]) == (
        "title 1",
        "fatherlinux",
        False,
    )
    assert new[trusted]["lane"] == "vertex"
    assert new[held]["held"] is True and new[held]["lane"] is None


def test_catch_up_since_moves_the_window(client: TestClient, keys: Keys, conn: sqlite3.Connection) -> None:
    old = seen(conn, 1, ago(days=3))
    recent = seen(conn, 2, ago(hours=1))
    assert [row["id"] for row in work(client, keys)["catch_up"]["new"]] == [recent]
    wide = work(client, keys, f"?since={ago(days=4)}")["catch_up"]
    assert [row["id"] for row in wide["new"]] == [old, recent]


def test_catch_up_stops_at_its_limit_and_says_so(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch, "QUEUE_LIMIT", 1)
    first = seen(conn, 1, ago(hours=2))
    seen(conn, 2, ago(hours=1))
    caught = work(client, keys)["catch_up"]
    assert [row["id"] for row in caught["new"]] == [first] and caught["truncated"] is True


def test_catch_up_reopened_lists_closed_items_open_at_the_source(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    again = seen(conn, 1, ago(days=5))
    quiet = seen(conn, 2, ago(days=5))
    shut = seen(conn, 3, ago(days=5))
    with store.transaction(conn):
        for item_id in (again, quiet):
            store.update_item(conn, item_id, {"state": "done"}, JOSUI)
        conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (ago(hours=1), again))
        conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (ago(days=3), quiet))
        store.update_item(conn, shut, {"state": "done"}, JOSUI)
        found = store.get_item(conn, shut) or {}
        store.update_item(conn, shut, {"extra": {**found["extra"], "source_state": "closed"}}, "intake")
        conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (ago(hours=1), shut))
    reopened = work(client, keys)["catch_up"]["reopened"]
    assert [(row["id"], row["state"]) for row in reopened] == [(again, "done")]


def test_the_default_since_is_the_sweep_mark_else_a_day_back(client: TestClient, keys: Keys) -> None:
    caught = work(client, keys)["catch_up"]
    until = datetime.strptime(caught["until"], "%Y-%m-%dT%H:%M:%SZ")
    assert caught["since"] == dispatch.stamp(until - timedelta(days=1))
    client.put("/v1/marks/sweep", headers=keys[JOSUI], json={"value": "2026-11-18T14:00Z"})
    assert work(client, keys)["catch_up"]["since"] == "2026-11-18T14:00:00Z"
    client.put("/v1/marks/sweep", headers=keys[JOSUI], json={"value": "2026-11-18T09:00:00-05:00"})
    assert work(client, keys)["catch_up"]["since"] == "2026-11-18T14:00:00Z"


def test_a_mark_in_short_form_still_compares_right(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    seen(conn, 1, "2020-06-01T13:59:59Z")
    on_the_mark = seen(conn, 2, "2020-06-01T14:00:00Z")
    client.put("/v1/marks/sweep", headers=keys[JOSUI], json={"value": "2020-06-01T14:00Z"})
    caught = work(client, keys)["catch_up"]
    assert caught["since"] == "2020-06-01T14:00:00Z"
    assert [row["id"] for row in caught["new"]] == [on_the_mark]


def test_a_since_that_is_not_a_time_is_422(client: TestClient, keys: Keys) -> None:
    bad = client.get("/v1/work?since=yesterday", headers=keys[SEAT])
    assert bad.status_code == UNPROCESSABLE_ENTITY and "since" in bad.json()["detail"]
    client.put("/v1/marks/sweep", headers=keys[JOSUI], json={"value": "last week"})
    assert client.get("/v1/work", headers=keys[SEAT]).status_code == UNPROCESSABLE_ENTITY


def test_advancing_is_admin_only_and_moves_the_mark(client: TestClient, keys: Keys) -> None:
    assert client.post("/v1/work", headers=keys[SEAT]).status_code == FORBIDDEN
    assert client.get("/v1/marks/sweep", headers=keys[JOSUI]).status_code == NOT_FOUND
    moved = client.post("/v1/work", headers=keys[JOSUI])
    assert moved.status_code == OK
    caught = moved.json()["catch_up"]
    assert caught["advanced"] is True
    assert client.get("/v1/marks/sweep", headers=keys[JOSUI]).json()["value"] == caught["until"]
    assert work(client, keys)["catch_up"]["advanced"] is False
