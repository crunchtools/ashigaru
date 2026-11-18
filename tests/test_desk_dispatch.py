"""Assign with seat-pick, desk_claim, usage and spend, and the outcomes rollup."""

import sqlite3
from http.client import CONFLICT, FORBIDDEN, NOT_FOUND, OK, UNPROCESSABLE_ENTITY

import pytest
from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import dispatch, store

MONTHLY_LIMIT = 500
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
