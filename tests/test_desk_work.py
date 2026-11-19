"""`GET /v1/work` and `POST /v1/work` against the real Desk: the check round and the forge catch-up."""

import dataclasses
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
        conn.execute("UPDATE events SET at = ? WHERE item_id = ?", (at, item_id))
    return item_id


def advance(client: TestClient, keys: Keys) -> dict[str, Any]:
    moved = client.post("/v1/work", headers=keys[JOSUI])
    assert moved.status_code == OK, moved.text
    caught: dict[str, Any] = moved.json()["catch_up"]
    return caught


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
    seen(conn, 3, ago(days=3), author_writes=True)
    trusted = seen(conn, 1, ago(hours=2), author_writes=True, lane_hint="vertex")
    held = seen(conn, 2, ago(hours=1))
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


def test_an_advance_after_a_truncated_round_loses_and_repeats_nothing(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    made = [seen(conn, number, ago(hours=1)) for number in range(1, dispatch.QUEUE_LIMIT + 2)]
    first = advance(client, keys)
    assert first["truncated"] is True and len(first["new"]) == dispatch.QUEUE_LIMIT
    assert client.get("/v1/marks/sweep", headers=keys[JOSUI]).json()["value"] == str(first["until"])
    second = advance(client, keys)
    assert second["truncated"] is False and second["after"] == first["until"]
    assert [row["id"] for row in first["new"] + second["new"]] == made
    assert advance(client, keys)["new"] == []


def test_a_delivery_after_the_round_is_the_next_rounds(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    before = seen(conn, 1, ago(minutes=1))
    assert [row["id"] for row in advance(client, keys)["new"]] == [before]
    later = seen(conn, 2, ago(minutes=2))  # stamped earlier than `before`, committed after the round
    assert [row["id"] for row in advance(client, keys)["new"]] == [later]


def test_catch_up_reopened_lists_closed_items_reopened_at_the_source(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    again, quiet, shut = (seen(conn, number, ago(days=5)) for number in (1, 2, 3))
    issue = IntakeItem(
        subject_key="crunchtools/x#1", title="title 1", ref="https://github.com/crunchtools/x/issues/1",
        source="webhook", repo="crunchtools/x", kind=Kind.STORY, author="fatherlinux",
    )  # fmt: skip
    with store.transaction(conn):
        for item_id in (again, quiet, shut):
            store.update_item(conn, item_id, {"state": "done"}, JOSUI)
        intake.upsert(conn, dataclasses.replace(issue, closed=True))
    client.put("/v1/marks/sweep", headers=keys[JOSUI], json={"value": ago(hours=1)})
    with store.transaction(conn):
        intake.upsert(conn, issue)
        intake.upsert(conn, dataclasses.replace(issue, subject_key="crunchtools/x#3", closed=True))
    caught = work(client, keys)["catch_up"]
    assert caught["new"] == [] and [(row["id"], row["state"]) for row in caught["reopened"]] == [
        (again, "done")
    ]


def test_a_later_delivery_on_an_item_closed_here_and_open_there_is_not_reopened(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    """Reopened is the event that flipped the source state, not the item's state on every write."""
    done = seen(conn, 1, ago(days=5))
    issue = IntakeItem(
        subject_key="crunchtools/x#1", title="title 1", ref="https://github.com/crunchtools/x/issues/1",
        source="webhook", repo="crunchtools/x", kind=Kind.STORY, author="fatherlinux",
    )  # fmt: skip
    with store.transaction(conn):
        store.update_item(conn, done, {"state": "done"}, JOSUI)
        intake.upsert(conn, dataclasses.replace(issue, closed=True))
        intake.upsert(conn, issue)
    assert [row["id"] for row in advance(client, keys)["reopened"]] == [done]
    with store.transaction(conn):
        intake.upsert(conn, dataclasses.replace(issue, title="title 1, edited"))
        intake.upsert(conn, dataclasses.replace(issue, title="title 1, edited", labels=("bug",)))
    assert advance(client, keys)["reopened"] == []


def test_without_a_mark_the_catch_up_reaches_a_day_back(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    seen(conn, 1, ago(days=2))
    recent = seen(conn, 2, ago(hours=1))
    caught = work(client, keys)["catch_up"]
    assert [row["id"] for row in caught["new"]] == [recent] and caught["since"]


def test_a_mark_set_to_a_time_is_read_in_any_form(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    seen(conn, 1, "2020-06-01T13:59:59Z")
    on_the_mark = seen(conn, 2, "2020-06-01T14:00:00Z")
    for value in ("2020-06-01T14:00Z", "2020-06-01T09:00:00-05:00"):
        client.put("/v1/marks/sweep", headers=keys[JOSUI], json={"value": value})
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
    caught = advance(client, keys)
    assert caught["advanced"] is True
    assert client.get("/v1/marks/sweep", headers=keys[JOSUI]).json()["value"] == str(caught["until"])
    assert work(client, keys)["catch_up"]["advanced"] is False
    assert work(client, keys)["catch_up"]["since"] is None


def test_advance_refuses_since(client: TestClient, keys: Keys) -> None:
    refused = client.post(f"/v1/work?since={ago(days=1)}", headers=keys[JOSUI])
    assert refused.status_code == UNPROCESSABLE_ENTITY and "since" in refused.json()["detail"]
    assert client.get("/v1/marks/sweep", headers=keys[JOSUI]).status_code == NOT_FOUND


def test_a_stale_advance_never_moves_the_mark_back(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    """Two rounds racing: the one that read less commits its mark last and must not win."""
    seen(conn, 1, ago(hours=1))
    stale = dispatch.catch_up(conn)
    seen(conn, 2, ago(minutes=1))
    fresh = advance(client, keys)
    assert fresh["until"] > stale["until"]
    assert store.advance_mark(conn, dispatch.SWEEP, stale["until"], JOSUI) is False
    assert client.get("/v1/marks/sweep", headers=keys[JOSUI]).json()["value"] == str(fresh["until"])
    assert store.advance_mark(conn, dispatch.SWEEP, fresh["until"], JOSUI) is False
    assert advance(client, keys)["advanced"] is False
    client.put("/v1/marks/sweep", headers=keys[JOSUI], json={"value": ago(days=2)})
    assert store.advance_mark(conn, dispatch.SWEEP, stale["until"], JOSUI) is True


def test_an_advance_that_lost_the_race_says_so(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen(conn, 1, ago(hours=1))
    round_ = dispatch.work

    def raced(conn: sqlite3.Connection, show_all: bool = False, since: str | None = None) -> dict[str, Any]:
        found = round_(conn, show_all, since)
        store.set_mark(conn, dispatch.SWEEP, str(found["catch_up"]["until"] + 1), "another round")
        return found

    monkeypatch.setattr(dispatch, "work", raced)
    caught = advance(client, keys)
    assert caught["advanced"] is False
    assert client.get("/v1/marks/sweep", headers=keys[JOSUI]).json()["value"] == str(caught["until"] + 1)


def test_the_check_table_stops_at_its_limit_and_says_so(
    client: TestClient, keys: Keys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch, "QUEUE_LIMIT", 1)
    create(client, keys, task="one")
    create(client, keys, task="two")
    round_ = work(client, keys)
    assert len(round_["items"]) == 1 and round_["truncated"] is True and round_["summary"]["open"] == TWO


def board(client: TestClient, keys: Keys) -> dict[str, Any]:
    got = client.get("/v1/status", headers=keys[SEAT])
    assert got.status_code == OK, got.text
    result: dict[str, Any] = got.json()
    return result


def test_the_board_shows_busy_seats_in_full_and_free_ones_by_name(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    other = "Exedore 2 - Max"
    with store.transaction(conn):
        for name in (SEAT, other, "Dolza 1 - Max"):
            store.write_seat(conn, name, {"status": "free", "lane": "exedore-max"})
        conn.execute("UPDATE seats SET updated_at = ? WHERE name = 'Dolza 1 - Max'", (LONG_AGO,))
    task = "a task long enough that any table would have clipped it, " * 3
    work_id = create(client, keys, task=task)["id"]
    assigned = client.post(f"/v1/items/{work_id}/assign", headers=keys[JOSUI], json={"seat": other})
    assert assigned.status_code == OK, assigned.text
    found = board(client, keys)
    assert [row["seat"] for row in found["busy"]] == [other]
    row = found["busy"][0]
    assert (row["mode"], row["work"], row["state"], row["task"]) == ("auto", work_id, "assigned", task)
    assert row["since"] and row["active"] and row["missing"] is False
    assert (found["free"], found["missing"]) == ([SEAT], ["Dolza 1 - Max"])
    assert found["summary"] == {"up": TWO, "seats": 3, "open": 1}


def test_needs_you_lists_a_waiting_manual_seat_then_blocked_manual_work_then_held_subjects(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    with store.transaction(conn):
        store.write_seat(conn, SEAT, {"status": "free", "lane": "exedore-max"})
    held = seen(conn, 7, ago(minutes=5))
    seen(conn, 8, ago(minutes=5), author_writes=True, lane_hint="vertex")
    blocked = create(client, keys, mode="manual", state="blocked", blocked_reason="which deck?")["id"]
    create(client, keys, state="blocked", blocked_reason="Josui's, not Scott's")
    path = f"/v1/seats/{SEAT}"
    assert (
        client.patch(path, headers=keys[SEAT], json={"turn": "active", "task": "OLF deck"}).status_code == OK
    )
    assert client.patch(
        path, headers=keys[SEAT], json={"turn": "idle", "waiting_on": "Keep slide 4?"}
    ).json()["waiting_on"] == ("Keep slide 4?")
    needs = board(client, keys)["needs_you"]
    assert [(need["why"], need["work"]) for need in needs][1:] == [("blocked", blocked), ("held", held)]
    waiting = needs[0]
    assert (waiting["why"], waiting["seat"], waiting["mode"], waiting["text"]) == (
        "waiting",
        SEAT,
        "manual",
        "Keep slide 4?",
    )
    assert waiting["state"] == "working"
    assert "crunchtools/x#7 by fatherlinux" in needs[2]["text"]
    client.patch(path, headers=keys[SEAT], json={"turn": "active"})
    assert [need["why"] for need in board(client, keys)["needs_you"]] == ["blocked", "held"]


def test_a_clear_drops_what_the_seat_was_waiting_on(client: TestClient, keys: Keys) -> None:
    path = f"/v1/seats/{SEAT}"
    client.patch(path, headers=keys[SEAT], json={"turn": "idle", "waiting_on": "Ship it?"})
    assert (
        client.patch(path, headers=keys[SEAT], json={"turn": "idle", "task": None}).json()["waiting_on"]
        is None
    )


def test_only_an_admin_and_the_seat_itself_read_what_it_is_waiting_on(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    path = f"/v1/seats/{SEAT}"
    client.patch(path, headers=keys[SEAT], json={"turn": "active", "task": "OLF deck"})
    client.patch(path, headers=keys[SEAT], json={"turn": "idle", "waiting_on": "Keep slide 4?"})

    def read(who: str) -> list[Any]:
        listed = {row["name"]: row for row in client.get("/v1/seats", headers=keys[who]).json()}
        rounds = {row["name"]: row for row in client.get("/v1/work", headers=keys[who]).json()["seats"]}
        needs = client.get("/v1/status", headers=keys[who]).json()["needs_you"]
        return [
            listed[SEAT]["waiting_on"],
            client.get(path, headers=keys[who]).json()["waiting_on"],
            rounds[SEAT]["waiting_on"],
            [need["text"] for need in needs if need["why"] == "waiting"],
        ]

    shown = ["Keep slide 4?", "Keep slide 4?", "Keep slide 4?", ["Keep slide 4?"]]
    assert read(JOSUI) == shown and read(SEAT) == shown
    assert read("Exedore 2 - Max") == read("ci") == [None, None, None, []]
