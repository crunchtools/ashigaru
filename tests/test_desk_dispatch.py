"""Assign with seat-pick, desk_claim, usage and spend, and the outcomes rollup."""

import asyncio
import sqlite3
import time
from http.client import CONFLICT, FORBIDDEN, NO_CONTENT, NOT_FOUND, OK, UNPROCESSABLE_ENTITY
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import Keys
from fastapi import Request
from fastapi.testclient import TestClient

from ashigaru.desk import api, auth, dispatch, store
from ashigaru.desk.model import NextWait, Role

MONTHLY_LIMIT = 500
WAIT = 1
JOSUI, MINE, OTHER, CI = "Josui Prime", "Exedore 1 - Max", "Exedore 2 - Max", "ci"


def item(client: TestClient, keys: Keys, **fields: object) -> str:
    return str(client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", **fields}).json()["id"])


def seat(client: TestClient, keys: Keys, name: str, **fields: object) -> None:
    body = {"lane": "exedore-max", "status": "free", **fields}
    assert client.patch(f"/v1/seats/{name}", headers=keys[JOSUI], json=body).status_code == OK


def backdate(conn: sqlite3.Connection, name: str, since: str) -> None:
    conn.execute("UPDATE seats SET since = ? WHERE name = ?", (since, name))


def test_assign_picks_the_longest_free_unheld_seat_in_the_lane(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    seat(client, keys, MINE)
    seat(client, keys, OTHER)
    seat(client, keys, "Dolza 1 - Max", lane="dolza-max")
    seat(client, keys, "Exedore 3 - Max", owner="Scott")
    backdate(conn, OTHER, "2000-01-01T00:00:00Z")
    backdate(conn, "Exedore 3 - Max", "1999-01-01T00:00:00Z")
    first = item(client, keys, lane="exedore-max")
    assigned = client.post(f"/v1/items/{first}/assign", headers=keys[JOSUI], json={}).json()
    assert (assigned["seat"], assigned["state"]) == (OTHER, "assigned") and assigned["assigned_at"]
    held = client.get(f"/v1/seats/{OTHER}", headers=keys[CI]).json()
    assert (held["status"], held["item"]) == ("busy", first)
    second = item(client, keys)
    response = client.post(f"/v1/items/{second}/assign", headers=keys[JOSUI], json={"lane": "exedore-max"})
    assert response.json()["seat"] == MINE and response.json()["lane"] == "exedore-max"
    third = item(client, keys, lane="exedore-max")
    assert client.post(f"/v1/items/{third}/assign", headers=keys[JOSUI], json={}).status_code == CONFLICT
    assert client.post(
        f"/v1/items/{item(client, keys)}/assign", headers=keys[JOSUI], json={}
    ).status_code == (CONFLICT)


def test_assign_refuses_busy_held_unknown_seats_waiting_and_closed_items(
    client: TestClient, keys: Keys
) -> None:
    seat(client, keys, MINE, status="busy")
    seat(client, keys, OTHER, owner="Scott")
    for name in (MINE, OTHER, "nobody"):
        response = client.post(
            f"/v1/items/{item(client, keys)}/assign", headers=keys[JOSUI], json={"seat": name}
        )
        assert response.status_code == CONFLICT, name
    seat(client, keys, "Exedore 3 - Max")
    blocker = item(client, keys)
    waiting = item(client, keys, after=[blocker])
    named = {"seat": "Exedore 3 - Max"}
    assert client.post(f"/v1/items/{waiting}/assign", headers=keys[JOSUI], json=named).status_code == CONFLICT
    done = item(client, keys, state="done")
    assert client.post(f"/v1/items/{done}/assign", headers=keys[JOSUI], json=named).status_code == CONFLICT
    client.patch(f"/v1/items/{blocker}", headers=keys[JOSUI], json={"state": "done"})
    assert client.post(f"/v1/items/{waiting}/assign", headers=keys[JOSUI], json=named).status_code == OK
    assert client.post("/v1/items/zzz/assign", headers=keys[JOSUI], json=named).status_code == NOT_FOUND
    assert client.post(f"/v1/items/{blocker}/assign", headers=keys[MINE], json=named).status_code == FORBIDDEN


def test_closing_an_item_frees_its_seat(client: TestClient, keys: Keys) -> None:
    seat(client, keys, MINE)
    work = item(client, keys)
    client.post(f"/v1/items/{work}/assign", headers=keys[JOSUI], json={"seat": MINE})
    client.patch(f"/v1/items/{work}", headers=keys[JOSUI], json={"state": "done", "wrap": "cleared"})
    freed = client.get(f"/v1/seats/{MINE}", headers=keys[CI]).json()
    assert (freed["status"], freed["item"]) == ("free", None)


def test_claim_is_atomic_and_scoped_to_queued_auto_items_in_the_seats_lane(
    client: TestClient, keys: Keys, db: object
) -> None:
    seat(client, keys, MINE)
    seat(client, keys, OTHER)
    work = item(client, keys, lane="exedore-max")
    claimed = client.post(f"/v1/items/{work}/claim", headers=keys[MINE])
    assert claimed.status_code == OK and (claimed.json()["seat"], claimed.json()["state"]) == (
        MINE,
        "assigned",
    )
    assert client.post(f"/v1/items/{work}/claim", headers=keys[OTHER]).status_code == CONFLICT
    assert client.post(f"/v1/items/{item(client, keys)}/claim", headers=keys[MINE]).status_code == CONFLICT
    for refused in (
        item(client, keys, mode="manual"),
        item(client, keys, lane="dolza-max"),
        item(client, keys, state="proposed"),
        item(client, keys, after=[work]),
    ):
        assert client.post(f"/v1/items/{refused}/claim", headers=keys[OTHER]).status_code == CONFLICT, refused
    assert client.post(f"/v1/items/{item(client, keys)}/claim", headers=keys[JOSUI]).status_code == FORBIDDEN
    assert client.post(f"/v1/items/{item(client, keys)}/claim", headers=keys[CI]).status_code == FORBIDDEN


def test_a_second_claim_from_another_connection_loses(
    client: TestClient, keys: Keys, db: object, conn: sqlite3.Connection
) -> None:
    seat(client, keys, MINE)
    seat(client, keys, OTHER)
    work = item(client, keys, lane="exedore-max")
    other = store.connect(str(db))
    dispatch.claim(conn, work, MINE)
    with pytest.raises(store.Conflict):
        dispatch.claim(other, work, OTHER)
    other.close()


def next_item(client: TestClient, keys: Keys, name: str, who: str | None = None, wait: int = 0) -> Any:
    return client.post(f"/v1/seats/{name}/next?wait={wait}", headers=keys[who or name])


def switch(client: TestClient, keys: Keys, value: str) -> None:
    assert client.put("/v1/marks/autodispatch", headers=keys[JOSUI], json={"value": value}).status_code == OK


def test_next_hands_out_nothing_until_the_switch_is_on(client: TestClient, keys: Keys) -> None:
    seat(client, keys, MINE)
    work = item(client, keys, lane="exedore-max", task="the brief's task")
    assert next_item(client, keys, MINE).status_code == NO_CONTENT
    switch(client, keys, "off")
    assert next_item(client, keys, MINE).status_code == NO_CONTENT
    switch(client, keys, "on")
    handed = next_item(client, keys, MINE)
    assert handed.status_code == OK, handed.text
    body = handed.json()
    assert (body["item"]["id"], body["item"]["seat"], body["item"]["state"]) == (work, MINE, "assigned")
    assert "the brief's task" in body["brief"] and f"ashigaru report {work}" in body["brief"]
    taken = client.get(f"/v1/seats/{MINE}", headers=keys[MINE]).json()
    assert (taken["status"], taken["item"]) == ("busy", work)


def test_next_takes_the_oldest_ready_auto_item_in_the_seats_lane(client: TestClient, keys: Keys) -> None:
    seat(client, keys, MINE)
    switch(client, keys, "on")
    blocker = item(client, keys, lane="dolza-max")
    item(client, keys, lane="exedore-max", after=[blocker])
    item(client, keys, lane="exedore-max", mode="manual")
    item(client, keys, lane="dolza-max")
    item(client, keys, lane="exedore-max", state="proposed")
    ready = item(client, keys, lane="exedore-max")
    item(client, keys, lane="exedore-max")
    assert next_item(client, keys, MINE).json()["item"]["id"] == ready


def test_next_refuses_a_seat_that_cannot_take_work(client: TestClient, keys: Keys) -> None:
    seat(client, keys, MINE)
    seat(client, keys, OTHER, lane=None)
    switch(client, keys, "on")
    item(client, keys, lane="exedore-max")
    item(client, keys, lane="exedore-max")
    assert next_item(client, keys, MINE).status_code == OK
    assert next_item(client, keys, MINE).status_code == CONFLICT
    assert next_item(client, keys, OTHER).status_code == CONFLICT
    assert next_item(client, keys, MINE, who=OTHER).status_code == FORBIDDEN
    assert next_item(client, keys, OTHER, who=CI).status_code == FORBIDDEN


def test_next_never_hands_a_mid_turn_seat_a_second_brief(client: TestClient, keys: Keys) -> None:
    seat(client, keys, MINE)
    switch(client, keys, "on")
    work = item(client, keys, lane="exedore-max")
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "active"})
    assert next_item(client, keys, MINE).status_code == NO_CONTENT
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "idle"})
    assert next_item(client, keys, MINE).json()["item"]["id"] == work


def test_next_takes_no_write_lock_to_find_nothing(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    seat(client, keys, MINE)
    item(client, keys, lane="exedore-max")
    writer = store.connect(str(db))
    writer.execute("PRAGMA busy_timeout = 0")
    reader = store.connect(str(db))
    reader.execute("PRAGMA busy_timeout = 0")
    writer.execute("BEGIN IMMEDIATE")
    try:
        assert dispatch.next_item(reader, MINE) is None
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        reader.close()


def test_next_claims_nothing_for_a_client_that_has_gone(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    class Gone:
        async def is_disconnected(self) -> bool:
            return True

    seat(client, keys, MINE)
    switch(client, keys, "on")
    work = item(client, keys, lane="exedore-max")
    caller = auth.Caller(name=MINE, role=Role.SEAT, key_id="k")
    answer = asyncio.run(api.next_item(cast(Request, Gone()), conn, caller, MINE, NextWait(wait=0)))
    assert getattr(answer, "status_code", None) == NO_CONTENT
    left = store.get_item(conn, work)
    assert left is not None and left["seat"] is None


def test_next_waits_out_its_window_when_nothing_comes(
    client: TestClient, keys: Keys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "NEXT_TICK", 0.05)
    seat(client, keys, MINE)
    switch(client, keys, "on")
    started = time.monotonic()
    assert next_item(client, keys, MINE, wait=WAIT).status_code == NO_CONTENT
    assert WAIT - api.NEXT_TICK < time.monotonic() - started < WAIT + 1


def test_usage_books_session_cost_deltas_against_spend_and_limits(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    def report(**body: object) -> None:
        response = client.post(
            f"/v1/seats/{MINE}/usage", headers=keys[MINE], json={"session_id": "s1", **body}
        )
        assert response.status_code == OK, response.text

    report(model="claude-opus-5-5", billing="max", context_used=50_000, context_window=200_000, cost_usd=1.5)
    report(session_tokens=90_000, cost_usd=2.0)
    report(session_id="s2", cost_usd=0.25)
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "active"})
    limits = client.put(
        "/v1/billing/max", headers=keys[JOSUI], json={"daily_usd": 50, "monthly_usd": MONTHLY_LIMIT}
    )
    assert limits.status_code == OK
    assert client.put("/v1/billing/max", headers=keys[MINE], json={"daily_usd": 1}).status_code == FORBIDDEN
    assert client.post(
        f"/v1/seats/{OTHER}/usage", headers=keys[MINE], json={"session_id": "x"}
    ).status_code == (FORBIDDEN)
    view = client.get("/v1/usage", headers=keys[CI]).json()
    [row] = view["seats"]
    assert (row["model"], row["billing"], row["context_used"], row["session_tokens"]) == (
        "claude-opus-5-5", "max", 50_000, 90_000,
    )  # fmt: skip
    assert row["session_cost_usd"] == pytest.approx(0.25) and row["turn_seconds"] is not None
    [spend] = view["spend"]
    assert spend["billing"] == "max" and spend["today_usd"] == pytest.approx(2.25)
    assert spend["month_usd"] == pytest.approx(2.25) and spend["monthly_limit_usd"] == MONTHLY_LIMIT


def spent(conn: sqlite3.Connection) -> dict[str, float]:
    return {row["billing"]: row["cost_usd"] for row in conn.execute("SELECT * FROM spend")}


def test_usage_books_the_backlog_once_billing_is_known(conn: sqlite3.Connection) -> None:
    dispatch.report_usage(conn, MINE, {"session_id": "s1", "cost_usd": 1.0})
    dispatch.report_usage(conn, MINE, {"session_id": "s1", "cost_usd": 1.5})
    assert spent(conn) == {}
    dispatch.report_usage(conn, MINE, {"session_id": "s1", "billing": "max", "cost_usd": 1.75})
    dispatch.report_usage(conn, MINE, {"session_id": "s1", "cost_usd": 2.0})
    assert spent(conn) == {"max": pytest.approx(2.0)}


def test_usage_bills_by_the_seats_lane(client: TestClient, keys: Keys, conn: sqlite3.Connection) -> None:
    seat(client, keys, MINE, lane="vertex")
    row = dispatch.report_usage(conn, MINE, {"session_id": "s1", "cost_usd": 0.5})
    assert row["billing"] == "vertex" and spent(conn) == {"vertex": pytest.approx(0.5)}


def test_a_late_lower_report_does_not_rewind_what_is_booked(conn: sqlite3.Connection) -> None:
    for cost in (2.0, 1.5, 2.1):
        dispatch.report_usage(conn, MINE, {"session_id": "s1", "billing": "max", "cost_usd": cost})
    assert spent(conn) == {"max": pytest.approx(2.1)}


def test_a_seats_lane_outranks_the_billing_its_own_key_reports(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    seat(client, keys, MINE)
    seat(client, keys, OTHER)
    for name, key in ((MINE, MINE), (OTHER, JOSUI)):
        response = client.post(
            f"/v1/seats/{name}/usage",
            headers=keys[key],
            json={"session_id": "s1", "billing": "openrouter", "cost_usd": 1.0},
        )
        assert response.status_code == OK, response.text
    assert spent(conn) == {"max": pytest.approx(1.0), "openrouter": pytest.approx(1.0)}


def test_usage_on_a_lane_with_no_billing_waits_for_one(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    seat(client, keys, MINE, lane="prime")
    dispatch.report_usage(conn, MINE, {"session_id": "s1", "cost_usd": 3.0})
    assert spent(conn) == {}
    dispatch.report_usage(conn, MINE, {"session_id": "s2", "cost_usd": 0.25})
    dispatch.report_usage(conn, MINE, {"session_id": "s2", "billing": "openrouter", "cost_usd": 0.5})
    assert spent(conn) == {"openrouter": pytest.approx(0.5)}


def test_the_migration_counts_billed_sessions_as_booked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = store.connect(tmp_path / "old.db")
    monkeypatch.setattr(store, "MIGRATIONS", store.MIGRATIONS[:5])
    store.migrate(old)
    for name, billing in ((MINE, "max"), (OTHER, None)):
        old.execute(
            "INSERT INTO seats (name, updated_at, billing, session_id, session_cost_usd)"
            " VALUES (?, 'then', ?, 's1', 2)",
            (name, billing),
        )
    monkeypatch.undo()
    store.migrate(old)
    assert [(store.get_seat(old, name) or {})["session_booked_usd"] for name in (MINE, OTHER)] == [2, 0]
    dispatch.report_usage(old, OTHER, {"session_id": "s1", "billing": "vertex", "cost_usd": 2.5})
    dispatch.report_usage(old, MINE, {"session_id": "s1", "cost_usd": 2.5})
    assert spent(old) == {"vertex": pytest.approx(2.5), "max": pytest.approx(0.5)}


def test_outcomes_roll_up_their_stories(client: TestClient, keys: Keys) -> None:
    outcome = item(client, keys, kind="outcome", ref="https://github.com/crunchtools/ashigaru/issues/178")
    for state in ("done", "done", "blocked", "working", "queued", "wontdo"):
        item(client, keys, parent=outcome, state=state)
    empty = item(client, keys, kind="outcome")
    rows = {row["id"]: row for row in client.get("/v1/outcomes", headers=keys[CI]).json()}
    counts = rows[outcome]
    assert (counts["total"], counts["done"], counts["blocked"], counts["open"]) == (6, 2, 1, 2)
    assert (rows[empty]["total"], rows[empty]["open"]) == (0, 0)


def test_a_seat_with_no_lane_claims_nothing(client: TestClient, keys: Keys) -> None:
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "idle"})
    assert client.get(f"/v1/seats/{MINE}", headers=keys[CI]).json()["lane"] is None
    for lane in ("prime", "exedore-max", None):
        target = item(client, keys, lane=lane)
        assert client.post(f"/v1/items/{target}/claim", headers=keys[MINE]).status_code == CONFLICT, lane
        assert client.get(f"/v1/items/{target}", headers=keys[CI]).json()["seat"] is None


def test_reassigning_frees_the_previous_holder(client: TestClient, keys: Keys) -> None:
    seat(client, keys, MINE)
    seat(client, keys, OTHER)
    seat(client, keys, "Exedore 3 - Max")
    work = item(client, keys)
    client.post(f"/v1/items/{work}/assign", headers=keys[JOSUI], json={"seat": MINE})
    moved = client.post(f"/v1/items/{work}/assign", headers=keys[JOSUI], json={"seat": OTHER})
    assert moved.status_code == OK and moved.json()["seat"] == OTHER
    seats = {row["name"]: row for row in client.get("/v1/seats", headers=keys[CI]).json()}
    assert (seats[MINE]["status"], seats[MINE]["item"]) == ("free", None)
    assert (seats[OTHER]["status"], seats[OTHER]["item"]) == ("busy", work)
    client.patch(f"/v1/items/{work}", headers=keys[JOSUI], json={"seat": "Exedore 3 - Max"})
    freed = client.get(f"/v1/seats/{OTHER}", headers=keys[CI]).json()
    assert (freed["status"], freed["item"]) == ("free", None)


def test_since_is_server_set_so_no_seat_jumps_the_pick_queue(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    seat(client, keys, MINE)
    seat(client, keys, OTHER)
    backdate(conn, OTHER, "2000-01-01T00:00:00Z")
    for who in (MINE, JOSUI):
        jump = client.patch(f"/v1/seats/{MINE}", headers=keys[who], json={"since": "1970-01-01T00:00:00Z"})
        assert jump.status_code == UNPROCESSABLE_ENTITY, who
    picked = client.post(
        f"/v1/items/{item(client, keys, lane='exedore-max')}/assign", headers=keys[JOSUI], json={}
    )
    assert picked.json()["seat"] == OTHER
    before = client.get(f"/v1/seats/{MINE}", headers=keys[CI]).json()["since"]
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "active"})
    assert client.get(f"/v1/seats/{MINE}", headers=keys[CI]).json()["since"] == before
    # A hold and its release are status changes: the seat goes to the back of the queue, never ahead.
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "active", "task": "x"})
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "idle", "task": None})
    assert client.get(f"/v1/seats/{MINE}", headers=keys[CI]).json()["since"] >= before


def test_an_item_with_no_lane_is_assigned_never_claimed(client: TestClient, keys: Keys) -> None:
    seat(client, keys, MINE)
    target = item(client, keys)
    assert client.get(f"/v1/items/{target}", headers=keys[CI]).json()["lane"] is None
    assert client.post(f"/v1/items/{target}/claim", headers=keys[MINE]).status_code == CONFLICT
    assert client.get(f"/v1/items/{target}", headers=keys[CI]).json()["seat"] is None
    given = client.post(f"/v1/items/{target}/assign", headers=keys[JOSUI], json={"seat": MINE})
    assert given.status_code == OK and given.json()["seat"] == MINE
