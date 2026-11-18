"""Who may do what: keys, and the scoping of items and seats to their owner."""

import sqlite3
from http.client import CONFLICT, CREATED, FORBIDDEN, NOT_FOUND, OK, UNAUTHORIZED

from conftest import Keys
from fastapi.testclient import TestClient

JOSUI, MINE, OTHER, CI = "Josui Prime", "Exedore 1 - Max", "Exedore 2 - Max", "ci"


def make(client: TestClient, keys: Keys, seat: str | None = MINE, **fields: object) -> str:
    response = client.post(
        "/v1/items", headers=keys[JOSUI], json={"task": "t", "seat": seat, "state": "assigned", **fields}
    )
    assert response.status_code == CREATED, response.text
    return str(response.json()["id"])


def test_no_key_bad_key_and_malformed_key_are_401(client: TestClient, keys: Keys) -> None:
    good = keys[MINE]["Authorization"]
    for headers in ({}, {"Authorization": good[:-2] + "xx"}, {"Authorization": "Bearer nope"}):
        assert client.get("/v1/items", headers=headers).status_code == UNAUTHORIZED
    assert client.get("/v1/me", headers={"Authorization": good}).json()["name"] == MINE


def test_key_is_header_only_and_stored_as_a_digest(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    token = keys[MINE]["Authorization"].removeprefix("Bearer ")
    assert client.get(f"/v1/me?api_key={token}").status_code == UNAUTHORIZED
    rows = conn.execute("SELECT * FROM api_keys").fetchall()
    secret = token.split("_", 2)[2]
    assert all(secret not in str(tuple(row)) for row in rows)


def test_key_use_is_recorded_at_most_once_per_interval(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    client.get("/v1/me", headers=keys[MINE])
    conn.execute("UPDATE api_keys SET last_used_at = '2999-01-01T00:00:00Z' WHERE identity = ?", (MINE,))
    client.get("/v1/me", headers=keys[MINE])
    used = conn.execute("SELECT last_used_at FROM api_keys WHERE identity = ?", (MINE,)).fetchone()[0]
    assert used == "2999-01-01T00:00:00Z"
    conn.execute("UPDATE api_keys SET last_used_at = '2000-01-01T00:00:00Z' WHERE identity = ?", (MINE,))
    client.get("/v1/me", headers=keys[MINE])
    used = conn.execute("SELECT last_used_at FROM api_keys WHERE identity = ?", (MINE,)).fetchone()[0]
    assert used > "2000-01-01T00:00:00Z"


def test_seat_writes_its_own_item_only(client: TestClient, keys: Keys) -> None:
    mine, theirs = make(client, keys), make(client, keys, seat=OTHER)
    ok = client.patch(f"/v1/items/{mine}", headers=keys[MINE], json={"state": "working", "result": "r"})
    assert ok.status_code == OK and ok.json()["state"] == "working"
    denied = client.patch(f"/v1/items/{theirs}", headers=keys[MINE], json={"state": "working"})
    assert denied.status_code == FORBIDDEN
    unowned = make(client, keys, seat=None)
    assert (
        client.patch(f"/v1/items/{unowned}", headers=keys[MINE], json={"note": "x"}).status_code == FORBIDDEN
    )


def test_seat_cannot_reassign_close_or_verify(client: TestClient, keys: Keys) -> None:
    mine = make(client, keys)
    for body in (
        {"seat": MINE},
        {"verified_by": MINE},
        {"mode": "manual"},
        {"state": "done"},
        {"state": "queued"},
    ):
        assert client.patch(f"/v1/items/{mine}", headers=keys[MINE], json=body).status_code == FORBIDDEN, body
    assert client.get(f"/v1/items/{mine}", headers=keys[MINE]).json()["state"] == "assigned"


def test_josui_writes_any_item(client: TestClient, keys: Keys) -> None:
    theirs = make(client, keys, seat=OTHER)
    body = {"state": "done", "verified_by": JOSUI, "seat": MINE}
    response = client.patch(f"/v1/items/{theirs}", headers=keys[JOSUI], json=body)
    assert response.status_code == OK
    assert response.json()["closed_at"] and response.json()["verified_by"] == JOSUI


def test_only_admins_create_items_and_ci_only_reads(client: TestClient, keys: Keys) -> None:
    for who in (MINE, CI):
        assert client.post("/v1/items", headers=keys[who], json={"task": "t"}).status_code == FORBIDDEN
    item = make(client, keys, seat=CI)
    assert client.patch(f"/v1/items/{item}", headers=keys[CI], json={"note": "x"}).status_code == FORBIDDEN
    assert client.get("/v1/check", headers=keys[CI]).status_code == OK


def test_report_contract_maps_onto_the_item(client: TestClient, keys: Keys) -> None:
    mine = make(client, keys)
    done = client.post(
        f"/v1/items/{mine}/report",
        headers=keys[MINE],
        json={"verb": "DONE", "text": "PR #1", "learned": "none"},
    ).json()
    assert (done["state"], done["result"], done["learned"]) == ("review", "PR #1", None)
    blocked = client.post(
        f"/v1/items/{mine}/report", headers=keys[MINE], json={"verb": "BLOCKED", "text": "Scott: token"}
    ).json()
    assert (blocked["state"], blocked["blocked_reason"]) == ("blocked", "Scott: token")
    cc = client.post(f"/v1/items/{mine}/report", headers=keys[MINE], json={"verb": "CC-DONE"}).json()
    assert cc["memorized"] is True
    theirs = make(client, keys, seat=OTHER)
    assert (
        client.post(f"/v1/items/{theirs}/report", headers=keys[MINE], json={"verb": "DONE"}).status_code
        == FORBIDDEN
    )


def test_seat_files_one_child_per_assignment(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    mine = make(client, keys, parent="a900")
    child = client.post(f"/v1/items/{mine}/file", headers=keys[MINE], json={"task": "follow-up"})
    assert child.status_code == CREATED
    body = child.json()
    assert (body["state"], body["parent"], body["filed_by"], body["filed_under"]) == (
        "proposed", "a900", MINE, mine,
    )  # fmt: skip
    assert body["expires_at"] > body["created_at"]
    again = client.post(f"/v1/items/{mine}/file", headers=keys[MINE], json={"task": "another"})
    assert again.status_code == CONFLICT
    theirs = make(client, keys, seat=OTHER)
    assert (
        client.post(f"/v1/items/{theirs}/file", headers=keys[MINE], json={"task": "x"}).status_code
        == FORBIDDEN
    )
    # A new assignment allows one more.
    conn.execute("UPDATE items SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?", (body["id"],))
    client.patch(f"/v1/items/{mine}", headers=keys[JOSUI], json={"state": "assigned"})
    assert (
        client.post(f"/v1/items/{mine}/file", headers=keys[MINE], json={"task": "next"}).status_code
        == CREATED
    )


def test_seat_writes_only_turn_task_and_since_on_its_own_row(client: TestClient, keys: Keys) -> None:
    ok = client.patch(
        f"/v1/seats/{MINE}", headers=keys[MINE], json={"turn": "active", "task": "first prompt"}
    )
    assert ok.status_code == OK and ok.json()["turn_started_at"]
    for body in ({"owner": None}, {"lane": "vertex"}, {"item": "a1"}, {"status": "free"}):
        assert client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json=body).status_code == FORBIDDEN, body
    assert (
        client.patch(f"/v1/seats/{OTHER}", headers=keys[MINE], json={"turn": "idle"}).status_code == FORBIDDEN
    )
    assert client.patch(f"/v1/seats/{CI}", headers=keys[CI], json={"turn": "idle"}).status_code == FORBIDDEN
    rows = client.get("/v1/seats", headers=keys[CI]).json()
    assert [(seat["name"], seat["missing"]) for seat in rows] == [(MINE, False), (OTHER, True)]


def test_a_seat_cannot_undo_scotts_manual_hold(client: TestClient, keys: Keys) -> None:
    held = client.patch(f"/v1/seats/{MINE}", headers=keys[JOSUI], json={"owner": "Scott", "status": "busy"})
    assert held.status_code == OK
    assert (
        client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json={"owner": None}).status_code == FORBIDDEN
    )
    seat = client.get(f"/v1/seats/{MINE}", headers=keys[MINE]).json()
    assert (seat["owner"], seat["status"]) == ("Scott", "busy")


def test_a_seat_cannot_reopen_or_file_under_a_closed_item(client: TestClient, keys: Keys) -> None:
    done = make(client, keys, state="done")
    cleared = make(client, keys, state="review", wrap="cleared")
    wontdo = make(client, keys, state="wontdo")
    for item in (done, cleared, wontdo):
        attempts = (
            client.patch(f"/v1/items/{item}", headers=keys[MINE], json={"state": "working"}),
            client.patch(f"/v1/items/{item}", headers=keys[MINE], json={"result": "rewritten"}),
            client.post(f"/v1/items/{item}/report", headers=keys[MINE], json={"verb": "DONE", "text": "x"}),
            client.post(f"/v1/items/{item}/file", headers=keys[MINE], json={"task": "child"}),
        )
        assert [response.status_code for response in attempts] == [FORBIDDEN] * len(attempts), item
    after = client.get(f"/v1/items/{done}", headers=keys[MINE]).json()
    assert after["state"] == "done" and after["result"] is None
    reopened = client.patch(f"/v1/items/{done}", headers=keys[JOSUI], json={"state": "working"})
    assert reopened.status_code == OK


def test_keys_minted_by_admin_and_revoked_by_admin_or_self(client: TestClient, keys: Keys) -> None:
    assert client.post(f"/v1/identities/{MINE}/keys", headers=keys[MINE], json={}).status_code == FORBIDDEN
    minted = client.post(f"/v1/identities/{MINE}/keys", headers=keys[JOSUI], json={"label": "hooks"})
    assert minted.status_code == CREATED
    second = {"Authorization": f"Bearer {minted.json()['token']}"}
    assert client.get("/v1/me", headers=second).json()["name"] == MINE
    listed = client.get(f"/v1/identities/{MINE}/keys", headers=keys[MINE]).json()
    assert len(listed) == len([MINE, "hooks"]) and all("token" not in key for key in listed)
    assert client.get(f"/v1/identities/{MINE}/keys", headers=keys[OTHER]).status_code == FORBIDDEN
    key_id = minted.json()["key_id"]
    assert client.delete(f"/v1/identities/{MINE}/keys/{key_id}", headers=keys[OTHER]).status_code == FORBIDDEN
    assert (
        client.delete(f"/v1/identities/{OTHER}/keys/{key_id}", headers=keys[OTHER]).status_code == NOT_FOUND
    )
    revoked = client.delete(f"/v1/identities/{MINE}/keys/{key_id}", headers=keys[MINE])
    assert revoked.status_code == OK and revoked.json()["revoked_at"]
    assert client.get("/v1/me", headers=second).status_code == UNAUTHORIZED
    assert client.get("/v1/me", headers=keys[MINE]).status_code == OK


def test_identities_are_admin_only(client: TestClient, keys: Keys) -> None:
    assert client.get("/v1/identities", headers=keys[MINE]).status_code == FORBIDDEN
    body = {"name": "Dolza 1 - Vertex", "role": "seat"}
    assert client.post("/v1/identities", headers=keys[MINE], json=body).status_code == FORBIDDEN
    assert client.post("/v1/identities", headers=keys[JOSUI], json=body).status_code == CREATED
    assert client.post("/v1/identities", headers=keys[JOSUI], json=body).status_code == CONFLICT
