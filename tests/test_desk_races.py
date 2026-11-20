"""The races of 4.x, each reproduced as it happened, and the 5.0.0 rules that end them: one open item
per seat with the seat row derived from it, seat sessions fenced by an epoch and kept up by a lease,
claims acknowledged by the seat's next turn, and writes that can name the version they read."""

import sqlite3
from datetime import UTC, datetime, timedelta
from http.client import CONFLICT, NO_CONTENT, OK, PRECONDITION_FAILED
from pathlib import Path
from typing import Any

import pytest
from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import reconcile, store, subjects

JOSUI, MINE, OTHER, CI = "Josui Prime", "Exedore 1 - Max", "Exedore 2 - Max", "ci"


def ago(**span: float) -> str:
    return (datetime.now(UTC) - timedelta(**span)).strftime("%Y-%m-%dT%H:%M:%SZ")


def item(client: TestClient, keys: Keys, **fields: object) -> str:
    return str(client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", **fields}).json()["id"])


def get(client: TestClient, keys: Keys, item_id: str) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"/v1/items/{item_id}", headers=keys[CI]).json()
    return found


def seat_row(client: TestClient, keys: Keys, name: str) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"/v1/seats/{name}", headers=keys[JOSUI]).json()
    return found


def up(client: TestClient, keys: Keys, name: str, lane: str = "exedore-max") -> None:
    """A seat in the lane, reporting for itself so its lease is live."""
    assert client.patch(f"/v1/seats/{name}", headers=keys[JOSUI], json={"lane": lane}).status_code == OK
    assert client.patch(f"/v1/seats/{name}", headers=keys[name], json={"turn": "idle"}).status_code == OK


def assign(client: TestClient, keys: Keys, item_id: str, **body: object) -> Any:
    return client.post(f"/v1/items/{item_id}/assign", headers=keys[JOSUI], json=body)


def switch_on(client: TestClient, keys: Keys) -> None:
    client.put("/v1/marks/autodispatch", headers=keys[JOSUI], json={"value": "on"})


def session(client: TestClient, keys: Keys, name: str, session_id: str, source: str) -> Any:
    body = {"session_id": session_id, "source": source}
    return client.post(f"/v1/seats/{name}/session", headers=keys[name], json=body)


def epoch(keys: Keys, name: str, value: int) -> dict[str, str]:
    return {**keys[name], "X-Ashigaru-Epoch": str(value)}


# R3: a /clear on a seat holding Prime's item left the seat busy with nothing in its context.


def test_r3_a_clear_before_the_wrap_blocks_the_auto_item_and_frees_the_seat(
    client: TestClient, keys: Keys
) -> None:
    up(client, keys, MINE)
    work = item(client, keys, lane="exedore-max")
    assert assign(client, keys, work, seat=MINE).status_code == OK
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "idle", "task": None})
    found = get(client, keys, work)
    assert (found["state"], found["seat"]) == ("blocked", None)
    assert "cleared" in found["blocked_reason"]
    assert seat_row(client, keys, MINE)["status"] == "free"
    needs = client.get("/v1/status", headers=keys[JOSUI]).json()["needs_you"]
    assert [need["work"] for need in needs if need["why"] == "blocked"] == [work]


def test_r3_a_clear_on_a_wrapping_item_waits_for_the_wrap(client: TestClient, keys: Keys) -> None:
    up(client, keys, MINE)
    work = item(client, keys, lane="exedore-max")
    assign(client, keys, work, seat=MINE)
    client.post(f"/v1/items/{work}/wrap", headers=keys[JOSUI], json={"stage": "wrapping"})
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "idle", "task": None})
    assert (get(client, keys, work)["state"], get(client, keys, work)["seat"]) == ("assigned", MINE)


# R4a: a seat on a dead host still looked free, and PICK_SEAT chose the one free longest.


def test_r4_a_seat_whose_lease_lapsed_is_never_picked_or_assigned(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    up(client, keys, MINE)
    up(client, keys, OTHER)
    conn.execute(
        "UPDATE seats SET since = ?, updated_at = ?, lease_until = ?, turn = 'active', silent_since = ?"
        " WHERE name = ?",
        (ago(hours=3), ago(hours=1), ago(minutes=50), ago(minutes=40), OTHER),
    )
    picked = assign(client, keys, item(client, keys, lane="exedore-max"))
    assert picked.status_code == OK and picked.json()["seat"] == MINE
    refused = assign(client, keys, item(client, keys), seat=OTHER)
    assert refused.status_code == CONFLICT and "missing" in refused.json()["detail"]
    assert seat_row(client, keys, OTHER)["missing"] is True


# R4b/c, R10: a session the Desk has ended (its lease lapsed, or a newer one started) is fenced out.


def test_r4_each_new_session_gets_a_new_epoch_and_the_old_one_is_refused(
    client: TestClient, keys: Keys
) -> None:
    up(client, keys, MINE)
    first = session(client, keys, MINE, "s1", "startup").json()["epoch"]
    work = item(client, keys, lane="exedore-max")
    assign(client, keys, work, seat=MINE)
    assert get(client, keys, work)["epoch"] == first
    client.post(f"/v1/items/{work}/wrap", headers=keys[JOSUI], json={"stage": "wrapping"})
    second = session(client, keys, MINE, "s2", "clear").json()["epoch"]
    assert second > first
    stale = client.post(
        f"/v1/items/{work}/report", headers=epoch(keys, MINE, first), json={"verb": "WORKING", "text": "x"}
    )
    assert stale.status_code == CONFLICT and stale.json()["code"] == "stale-session"
    resumed = session(client, keys, MINE, "s1", "resume")
    assert resumed.status_code == CONFLICT and resumed.json()["code"] == "stale-session"
    again = session(client, keys, MINE, "s2", "resume")
    assert again.status_code == OK and again.json()["epoch"] == second


def test_r4_a_stale_session_cannot_claim_report_or_keep_the_seat_up(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    up(client, keys, MINE)
    old = session(client, keys, MINE, "s1", "startup").json()["epoch"]
    session(client, keys, MINE, "s2", "startup")
    switch_on(client, keys)
    work = item(client, keys, lane="exedore-max")
    claimed = client.post(f"/v1/seats/{MINE}/next?wait=0", headers=epoch(keys, MINE, old))
    assert claimed.status_code == CONFLICT and claimed.json()["code"] == "stale-session"
    assert client.post(f"/v1/items/{work}/claim", headers=epoch(keys, MINE, old)).status_code == CONFLICT
    conn.execute("UPDATE seats SET lease_until = ? WHERE name = ?", (ago(minutes=1), MINE))
    client.patch(f"/v1/seats/{MINE}", headers=epoch(keys, MINE, old), json={"turn": "idle"})
    assert seat_row(client, keys, MINE)["lease_until"] < ago()


def test_r4_a_lapsed_lease_ends_the_session_and_requeues_its_auto_work(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    up(client, keys, MINE)
    up(client, keys, OTHER)
    first = session(client, keys, MINE, "s1", "startup").json()["epoch"]
    auto = item(client, keys, lane="exedore-max")
    assign(client, keys, auto, seat=MINE)
    manual = item(client, keys, lane="exedore-max", mode="manual")
    assign(client, keys, manual, seat=OTHER)
    conn.execute("UPDATE seats SET lease_until = ?, updated_at = ?", (ago(minutes=5), ago(minutes=70)))
    reconcile.run(str(db), reconcile.Settings())
    requeued = get(client, keys, auto)
    assert (requeued["state"], requeued["seat"]) == ("queued", None) and "lease" in requeued["note"]
    assert (get(client, keys, manual)["seat"], seat_row(client, keys, OTHER)["status"]) == (OTHER, "busy")
    assert seat_row(client, keys, MINE)["epoch"] > first
    resumed = session(client, keys, MINE, "s1", "resume")
    assert resumed.status_code == CONFLICT and resumed.json()["code"] == "stale-session"


# R2 residue: the next hook stopped for good on a 409 that only meant "busy until the wrap".


def test_r2_next_says_why_it_refuses_so_a_hook_can_wait_out_busy(client: TestClient, keys: Keys) -> None:
    up(client, keys, MINE)
    switch_on(client, keys)
    assign(client, keys, item(client, keys, lane="exedore-max"), seat=MINE)
    busy = client.post(f"/v1/seats/{MINE}/next?wait=0", headers=keys[MINE])
    assert busy.status_code == CONFLICT and busy.json()["code"] == "busy"
    client.patch(f"/v1/seats/{OTHER}", headers=keys[OTHER], json={"turn": "idle"})
    laneless = client.post(f"/v1/seats/{OTHER}/next?wait=0", headers=keys[OTHER])
    assert laneless.status_code == CONFLICT and laneless.json()["code"] == "no-lane"


# R6: Scott typing on a seat autodispatch had just claimed: his prompt and the brief shared a context.


def test_r6_a_typed_prompt_before_the_claim_is_started_hands_the_claim_back(
    client: TestClient, keys: Keys
) -> None:
    up(client, keys, MINE)
    switch_on(client, keys)
    work = item(client, keys, lane="exedore-max")
    assert client.post(f"/v1/seats/{MINE}/next?wait=0", headers=keys[MINE]).status_code == OK
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "active", "task": "Scott's own"})
    back = get(client, keys, work)
    assert (back["state"], back["seat"]) == ("queued", None)
    row = seat_row(client, keys, MINE)
    assert (row["owner"], get(client, keys, row["item"])["task"]) == ("Scott", "Scott's own")


# R7: a seat change, a requeue or a reopen left item.seat and seats.item saying different things.


def test_r7_moving_an_item_takes_the_new_seat_and_frees_the_old(client: TestClient, keys: Keys) -> None:
    up(client, keys, MINE)
    up(client, keys, OTHER)
    work = item(client, keys, lane="exedore-max")
    assign(client, keys, work, seat=MINE)
    assert client.patch(f"/v1/items/{work}", headers=keys[JOSUI], json={"seat": OTHER}).status_code == OK
    assert (seat_row(client, keys, OTHER)["item"], seat_row(client, keys, MINE)["status"]) == (work, "free")
    second = item(client, keys, lane="exedore-max")
    moved = client.patch(
        f"/v1/items/{second}", headers=keys[JOSUI], json={"seat": OTHER, "state": "assigned"}
    )
    assert moved.status_code == CONFLICT and moved.json()["code"] == "busy"


def test_r7_a_requeue_clears_the_seat(client: TestClient, keys: Keys) -> None:
    up(client, keys, MINE)
    work = item(client, keys, lane="exedore-max")
    assign(client, keys, work, seat=MINE)
    client.patch(f"/v1/items/{work}", headers=keys[JOSUI], json={"state": "queued"})
    assert (get(client, keys, work)["seat"], seat_row(client, keys, MINE)["status"]) == (None, "free")
    assert assign(client, keys, work).json()["seat"] == MINE


def test_r7_a_reopened_subject_requeues_its_item_without_a_seat(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    up(client, keys, MINE)
    work = item(client, keys, lane="exedore-max", subject_key="crunchtools/x#1")
    assign(client, keys, work, seat=MINE)
    with store.transaction(conn):
        store.update_item(conn, work, {"state": "done"}, subjects.ACTOR, subjects.SOURCE)
    assign(client, keys, item(client, keys, lane="exedore-max"), seat=MINE)
    conn.execute(
        "INSERT INTO subjects (subject_key, repo, kind, state, source, seen_at)"
        " VALUES ('crunchtools/x#1', 'crunchtools/x', 'story', 'open', 'webhook', ?)",
        (ago(),),
    )
    assert subjects.hygiene(conn) == [work]
    assert (get(client, keys, work)["state"], get(client, keys, work)["seat"]) == ("queued", None)


def test_r7_occupancy_is_the_items_and_no_one_writes_it_directly(client: TestClient, keys: Keys) -> None:
    up(client, keys, MINE)
    refused = client.patch(f"/v1/seats/{MINE}", headers=keys[JOSUI], json={"status": "busy", "item": "a9"})
    assert refused.status_code == CONFLICT
    assert seat_row(client, keys, MINE)["status"] == "free"


# R11: a claim nobody read sat assigned until a person looked.


def test_r11_the_seats_next_turn_acknowledges_a_claim(client: TestClient, keys: Keys) -> None:
    up(client, keys, MINE)
    switch_on(client, keys)
    work = item(client, keys, lane="exedore-max")
    client.post(f"/v1/seats/{MINE}/next?wait=0", headers=keys[MINE])
    client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "active"})
    assert get(client, keys, work)["state"] == "working"


def test_r11_an_unacknowledged_claim_goes_back_to_the_queue(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    up(client, keys, MINE)
    switch_on(client, keys)
    claimed = item(client, keys, lane="exedore-max")
    client.post(f"/v1/seats/{MINE}/next?wait=0", headers=keys[MINE])
    conn.execute("UPDATE items SET assigned_at = ? WHERE id = ?", (ago(minutes=11), claimed))
    conn.execute("UPDATE events SET at = ? WHERE item_id = ?", (ago(minutes=11), claimed))
    reconcile.run(str(db), reconcile.Settings())
    assert (get(client, keys, claimed)["state"], get(client, keys, claimed)["seat"]) == ("queued", None)
    assert client.post(f"/v1/seats/{MINE}/next?wait=0", headers=keys[MINE]).status_code in (OK, NO_CONTENT)


def test_r11_an_assign_by_josui_is_never_handed_back(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    up(client, keys, MINE)
    work = item(client, keys, lane="exedore-max")
    assign(client, keys, work, seat=MINE)
    conn.execute("UPDATE items SET assigned_at = ? WHERE id = ?", (ago(minutes=30), work))
    conn.execute("UPDATE events SET at = ? WHERE item_id = ?", (ago(minutes=30), work))
    reconcile.run(str(db), reconcile.Settings())
    assert get(client, keys, work)["seat"] == MINE


# R12: Prime's read, decide, write lost a seat's report that landed in between.


def test_r12_a_write_naming_an_old_version_is_refused(client: TestClient, keys: Keys) -> None:
    up(client, keys, MINE)
    work = item(client, keys, lane="exedore-max")
    assign(client, keys, work, seat=MINE)
    read = get(client, keys, work)["version"]
    client.post(f"/v1/items/{work}/report", headers=keys[MINE], json={"verb": "BLOCKED", "text": "no token"})
    match = {**keys[JOSUI], "If-Match": str(read)}
    late = client.post(f"/v1/items/{work}/wrap", headers=match, json={"force": True})
    assert late.status_code == PRECONDITION_FAILED and late.json()["code"] == "version"
    assert get(client, keys, work)["state"] == "blocked"
    patched = client.patch(f"/v1/items/{work}", headers=match, json={"state": "done"})
    assert patched.status_code == PRECONDITION_FAILED
    now = {**keys[JOSUI], "If-Match": f'"{get(client, keys, work)["version"]}"'}
    assert client.patch(f"/v1/items/{work}", headers=now, json={"note": "seen"}).status_code == OK


def test_the_migration_leaves_one_open_item_per_seat_and_rows_that_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.x data as the races left it: a requeued item still naming its seat, a seat named by two open
    items (the row's own item kept, the other let go and requeued), and rows saying busy or free
    against their items."""
    old = store.connect(tmp_path / "old.db")
    monkeypatch.setattr(store, "MIGRATIONS", store.MIGRATIONS[:-1])
    store.migrate(old)
    rows = [
        ("a1", MINE, "queued", "auto"),
        ("a2", MINE, "working", "auto"),
        ("a3", MINE, "assigned", "auto"),
        ("a4", OTHER, "blocked", "manual"),
        ("a5", OTHER, "done", "auto"),
    ]
    for item_id, seat, state, mode in rows:
        old.execute(
            "INSERT INTO items (id, seat, state, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, seat, state, mode, ago(hours=2), ago(hours=2)),
        )
    for name, status, held in (
        (MINE, "busy", "a2"),
        (OTHER, "free", None),
        ("Exedore 3 - Max", "busy", "a5"),
    ):
        old.execute(
            "INSERT INTO seats (name, status, item, since, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, status, held, ago(hours=1), ago(minutes=5)),
        )
    monkeypatch.undo()
    store.migrate(old)
    found = {row["id"]: (row["seat"], row["state"]) for row in old.execute("SELECT * FROM items")}
    assert found == {
        "a1": (None, "queued"),
        "a2": (MINE, "working"),
        "a3": (None, "queued"),
        "a4": (OTHER, "blocked"),
        "a5": (OTHER, "done"),
    }
    seats = {
        row["name"]: (row["status"], row["item"], row["owner"]) for row in old.execute("SELECT * FROM seats")
    }
    assert seats == {
        MINE: ("busy", "a2", None),
        OTHER: ("busy", "a4", "Scott"),
        "Exedore 3 - Max": ("free", None, None),
    }
    moved = old.execute("SELECT item_id FROM events WHERE source = 'migration' ORDER BY item_id").fetchall()
    assert [row[0] for row in moved] == ["a1", "a3"]
    assert old.execute("SELECT lease_until > updated_at FROM seats WHERE name = ?", (MINE,)).fetchone()[0]
    old.close()
