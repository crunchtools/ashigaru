"""`GET /v1/changes` and `POST /v1/changes`: what others changed since each reader last looked."""

import sqlite3
from http.client import CREATED, OK, UNPROCESSABLE_ENTITY
from typing import Any

import pytest
from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import changes, dispatch, store

JOSUI, SEAT, OTHER = "Josui Prime", "Exedore 1 - Max", "Exedore 2 - Max"
WRITES = 2
THREE = 3


def create(client: TestClient, keys: Keys, **body: Any) -> str:
    made = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t"} | body)
    assert made.status_code == CREATED, made.text
    return str(made.json()["id"])


def look(client: TestClient, keys: Keys, who: str, ack: bool = False, **params: str) -> dict[str, Any]:
    found = (
        client.post("/v1/changes", headers=keys[who])
        if ack
        else client.get("/v1/changes", headers=keys[who], params=params)
    )
    assert found.status_code == OK, found.text
    result: dict[str, Any] = found.json()
    return result


def test_a_reader_sees_others_writes_once_each_and_not_their_own(client: TestClient, keys: Keys) -> None:
    item = create(client, keys, task="Write the docs")
    patched = client.patch(f"/v1/items/{item}", headers=keys[JOSUI], json={"next": "review it"})
    assert patched.status_code == OK, patched.text
    (row,) = look(client, keys, SEAT)["items"]
    assert row["id"] == item and row["new"] and row["by"] == [JOSUI] and row["writes"] == WRITES
    assert "next" in row["fields"] and row["task"] == "Write the docs" and row["step"] == "review it"
    assert look(client, keys, JOSUI)["items"] == []


def test_an_ack_moves_only_the_readers_mark(client: TestClient, keys: Keys) -> None:
    create(client, keys)
    acked = look(client, keys, SEAT, ack=True)
    assert acked["acked"] and len(acked["items"]) == 1
    assert look(client, keys, SEAT)["items"] == []
    assert look(client, keys, SEAT)["seen_at"]
    assert len(look(client, keys, OTHER)["items"]) == 1, "each identity keeps its own mark"
    assert not look(client, keys, SEAT, ack=True)["acked"], "already there"
    item = create(client, keys)
    client.patch(f"/v1/items/{item}", headers=keys[JOSUI], json={"note": "n"})
    (row,) = look(client, keys, SEAT)["items"]
    assert row["id"] == item


def test_since_reads_from_a_time_and_a_bad_one_is_422(client: TestClient, keys: Keys) -> None:
    create(client, keys)
    assert look(client, keys, SEAT, since="2999-01-01T00:00:00Z")["items"] == []
    assert len(look(client, keys, SEAT, since="2020-01-01T00:00:00Z")["items"]) == 1
    bad = client.get("/v1/changes", headers=keys[SEAT], params={"since": "soon"})
    assert bad.status_code == UNPROCESSABLE_ENTITY


def test_the_board_shows_changes_without_moving_the_mark(client: TestClient, keys: Keys) -> None:
    item = create(client, keys)
    board = client.get("/v1/status", headers=keys[SEAT]).json()
    assert [row["id"] for row in board["changes"]["items"]] == [item]
    assert [row["id"] for row in look(client, keys, SEAT)["items"]] == [item]


def test_a_look_folds_each_items_writers_and_drops_quiet_ones(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    item, quiet = create(client, keys), create(client, keys)
    look(client, keys, SEAT, ack=True)
    with store.transaction(conn):
        store.update_item(conn, item, {"note": "one"}, "desk", "sweep")
        store.update_item(conn, item, {"next": "two"}, JOSUI)
        store.update_item(conn, item, {"note": "three"}, "desk", "sweep")
        store.update_item(conn, quiet, {"extra": {"source_state": "open"}}, "desk", "webhook")
    (row,) = look(client, keys, SEAT)["items"]
    assert row["id"] == item and not row["new"] and row["by"] == ["desk", JOSUI]
    assert row["writes"] == THREE and row["fields"] == ["next", "note"]


def test_a_look_cut_at_the_limit_acks_only_what_it_read(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, third = create(client, keys), create(client, keys), create(client, keys)
    monkeypatch.setattr(dispatch, "QUEUE_LIMIT", 2)
    cut = look(client, keys, SEAT, ack=True)
    assert cut["truncated"] and {row["id"] for row in cut["items"]} == {first, second}
    rest = look(client, keys, SEAT, ack=True)
    assert not rest["truncated"] and [row["id"] for row in rest["items"]] == [third]


def test_an_ack_never_moves_the_mark_back(client: TestClient, keys: Keys, conn: sqlite3.Connection) -> None:
    create(client, keys)
    ahead = dispatch.QUEUE_LIMIT * 10
    with store.transaction(conn):
        store.set_mark(conn, changes.mark(SEAT), str(ahead), SEAT)
    assert not look(client, keys, SEAT, ack=True)["acked"]
    assert (store.get_mark(conn, changes.mark(SEAT)) or {})["value"] == str(ahead)


def test_a_first_look_says_it_covers_the_last_day(client: TestClient, keys: Keys) -> None:
    found = look(client, keys, SEAT)
    assert found["since"] is None and found["seen_at"] is None
