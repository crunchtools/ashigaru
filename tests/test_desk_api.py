"""The API's own rules, and the committed contract staying equal to the code."""

import json
import sqlite3
import stat
from http.client import CONFLICT, CREATED, FORBIDDEN, NOT_FOUND, OK, UNPROCESSABLE_ENTITY
from pathlib import Path

import pytest
from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import __main__ as desk_main
from ashigaru.desk import store
from ashigaru.desk.api import create_app

CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "desk-api.json"
JOSUI = "Josui Prime"


def test_committed_contract_matches_the_code() -> None:
    generated = json.loads(json.dumps(create_app(":memory:").openapi(), sort_keys=True))
    assert json.loads(CONTRACT.read_text()) == generated, "run: ashigaru-desk openapi > docs/desk-api.json"


def test_contract_documents_error_responses() -> None:
    patch = create_app(":memory:").openapi()["paths"]["/v1/items/{item_id}"]["patch"]["responses"]
    assert {"200", "401", "403", "404", "409", "422"} <= set(patch)


def test_health_needs_no_key(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True, "schema": len(store.MIGRATIONS)}


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    assert store.migrate(conn) == len(store.MIGRATIONS)
    assert store.schema_version(conn) == len(store.MIGRATIONS)


def test_a_version_one_database_upgrades_in_place(tmp_path: Path) -> None:
    old = store.connect(tmp_path / "v1.db")
    with store.transaction(old):
        for statement in filter(str.strip, store.MIGRATIONS[0].split(";")):
            old.execute(statement)
        old.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, 'then')")
    old.execute("INSERT INTO seats (name, updated_at) VALUES ('Exedore 1 - Max', 'then')")
    assert store.migrate(old) == len(store.MIGRATIONS)
    upgraded = store.get_seat(old, "Exedore 1 - Max") or {}
    assert upgraded["session_cost_usd"] == 0 and upgraded["model"] is None


def test_subject_key_and_id_are_unique(client: TestClient, keys: Keys) -> None:
    body = {"task": "t", "subject_key": "crunchtools/ashigaru#1"}
    first = client.post("/v1/items", headers=keys[JOSUI], json=body)
    assert first.status_code == CREATED and first.json()["id"] == "a1" and first.json()["state"] == "queued"
    assert client.post("/v1/items", headers=keys[JOSUI], json=body).status_code == CONFLICT
    assert (
        client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "id": "a1"}).status_code == CONFLICT
    )
    odd = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "id": "a99x"})
    assert odd.status_code == CREATED and odd.json()["id"] == "a99x"
    assert client.post("/v1/items", headers=keys[JOSUI], json={"task": "t"}).json()["id"] == "a2"
    assert (
        client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "bogus": 1}).status_code
        == UNPROCESSABLE_ENTITY
    )


def test_filters_and_state_timestamps(client: TestClient, keys: Keys) -> None:
    client.post("/v1/items", headers=keys[JOSUI], json={"task": "q", "lane": "vertex"})
    assigned = client.post("/v1/items", headers=keys[JOSUI], json={"task": "a", "state": "assigned"}).json()
    assert assigned["assigned_at"] and not assigned["queued_at"]
    assert [i["task"] for i in client.get("/v1/items?lane=vertex", headers=keys["ci"]).json()] == ["q"]
    paged = client.get("/v1/items?limit=1&offset=1", headers=keys["ci"]).json()
    assert [i["task"] for i in paged] == ["q"]
    assert client.get("/v1/items?limit=0", headers=keys["ci"]).status_code == UNPROCESSABLE_ENTITY
    assert client.get("/v1/items?state=nonsense", headers=keys["ci"]).status_code == UNPROCESSABLE_ENTITY
    assert client.get("/v1/items/zzz", headers=keys["ci"]).status_code == NOT_FOUND


SEAT = "Exedore 1 - Max"


def test_a_taken_subject_key_names_its_item(client: TestClient, keys: Keys) -> None:
    body = {"task": "t", "subject_key": "RT#9"}
    first = client.post("/v1/items", headers=keys[JOSUI], json=body).json()
    again = client.post("/v1/items", headers=keys[JOSUI], json=body)
    assert (
        again.status_code == CONFLICT
        and again.json()["detail"] == f"subject_key RT#9 is taken by {first['id']}"
    )
    taken = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "id": first["id"]})
    assert taken.json()["detail"] == f"id {first['id']} is taken"


def test_an_admin_sets_filed_by_and_a_seat_does_not(client: TestClient, keys: Keys) -> None:
    item = client.post(
        "/v1/items",
        headers=keys[JOSUI],
        json={"task": "t", "seat": SEAT, "state": "working", "filed_by": SEAT},
    ).json()
    assert item["filed_by"] == SEAT
    path = f"/v1/items/{item['id']}"
    assert client.patch(path, headers=keys[JOSUI], json={"filed_by": "Scott"}).json()["filed_by"] == "Scott"
    assert client.patch(path, headers=keys[SEAT], json={"filed_by": SEAT}).status_code == FORBIDDEN


def test_list_by_several_states_learned_memorized_subject_key_and_ref(client: TestClient, keys: Keys) -> None:
    def make(**fields: object) -> str:
        return str(client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", **fields}).json()["id"])

    done = make(state="done", learned="poll less", ref="https://rt.example/1", subject_key="RT#1")
    blocked = make(state="blocked", learned="ask first", memorized=True)
    make(state="wontdo")
    make(state="working", learned="x")

    def ids(query: str) -> list[str]:
        return [row["id"] for row in client.get(f"/v1/items?{query}", headers=keys["ci"]).json()]

    assert ids("state=done&state=blocked") == [blocked, done]
    assert ids("state=done&state=blocked&learned=true&memorized=false") == [done]
    assert ids("subject_key=RT%231") == ids("ref=https://rt.example/1") == [done]
    assert len(ids("learned=false")) == 1


def test_queue_names_the_state_of_what_each_item_waits_on(client: TestClient, keys: Keys) -> None:
    client.post("/v1/items", headers=keys[JOSUI], json={"id": "a1", "task": "t", "state": "working"})
    client.post("/v1/items", headers=keys[JOSUI], json={"id": "a2", "task": "t", "after": ["a1", "a9"]})
    client.post("/v1/items", headers=keys[JOSUI], json={"id": "a3", "task": "t", "state": "proposed"})
    view = client.get("/v1/queue", headers=keys["ci"]).json()
    assert [(row["id"], row["waits_on"]) for row in view["queued"]] == [
        ("a2", [{"id": "a1", "state": "working"}, {"id": "a9", "state": "unknown"}])
    ]
    assert [row["id"] for row in view["proposed"]] == ["a3"]


def test_wrap_marks_wrapping_then_clears_once_memorized_and_idle(client: TestClient, keys: Keys) -> None:
    client.patch(f"/v1/seats/{SEAT}", headers=keys[JOSUI], json={"lane": "exedore-max"})
    item = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "lane": "exedore-max"}).json()
    path = f"/v1/items/{item['id']}"
    client.post(f"{path}/assign", headers=keys[JOSUI], json={"seat": SEAT})
    client.post(f"{path}/report", headers=keys[SEAT], json={"verb": "DONE", "text": "PR #1"})
    assert client.post(f"{path}/wrap", headers=keys[SEAT], json={}).status_code == FORBIDDEN
    wrapping = client.post(f"{path}/wrap", headers=keys[JOSUI], json={"stage": "wrapping"}).json()
    assert (wrapping["wrap"], wrapping["state"]) == ("wrapping", "review")

    early = client.post(f"{path}/wrap", headers=keys[JOSUI], json={})
    assert early.status_code == CONFLICT and "/cc seat" in early.json()["detail"]
    client.post(f"{path}/report", headers=keys[SEAT], json={"verb": "CC-DONE"})
    client.patch(f"/v1/seats/{SEAT}", headers=keys[SEAT], json={"turn": "active"})
    busy = client.post(f"{path}/wrap", headers=keys[JOSUI], json={})
    assert busy.status_code == CONFLICT and "mid-turn" in busy.json()["detail"]

    client.patch(f"/v1/seats/{SEAT}", headers=keys[SEAT], json={"turn": "idle"})
    cleared = client.post(f"{path}/wrap", headers=keys[JOSUI], json={"learned": "small PRs"}).json()
    assert (cleared["wrap"], cleared["state"], cleared["learned"]) == ("cleared", "done", "small PRs")
    assert client.get(f"/v1/seats/{SEAT}", headers=keys["ci"]).json()["status"] == "free"


def test_wrap_force_clears_a_seat_mid_turn(client: TestClient, keys: Keys) -> None:
    client.patch(f"/v1/seats/{SEAT}", headers=keys[JOSUI], json={"lane": "exedore-max"})
    item = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "lane": "exedore-max"}).json()
    path = f"/v1/items/{item['id']}"
    client.post(f"{path}/assign", headers=keys[JOSUI], json={"seat": SEAT})
    client.patch(f"/v1/seats/{SEAT}", headers=keys[SEAT], json={"turn": "active"})
    assert client.post(f"{path}/wrap", headers=keys[JOSUI], json={}).status_code == CONFLICT
    forced = client.post(
        f"{path}/wrap", headers=keys[JOSUI], json={"state": "abandoned", "force": True}
    ).json()
    assert (forced["wrap"], forced["state"]) == ("cleared", "abandoned")
    assert client.get(f"/v1/seats/{SEAT}", headers=keys["ci"]).json()["status"] == "free"


def test_wrap_force_keeps_a_closed_state(client: TestClient, keys: Keys) -> None:
    item = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "state": "wontdo"}).json()
    path = f"/v1/items/{item['id']}/wrap"
    assert client.post(path, headers=keys[JOSUI], json={"state": "done"}).status_code == CONFLICT
    forced = client.post(path, headers=keys[JOSUI], json={"state": "done", "force": True}).json()
    assert (forced["wrap"], forced["state"]) == ("cleared", "wontdo")
    assert client.post("/v1/items/nope/wrap", headers=keys[JOSUI], json={}).status_code == NOT_FOUND


def test_seats_show_the_missing(client: TestClient, keys: Keys, conn: sqlite3.Connection) -> None:
    client.patch(f"/v1/seats/{SEAT}", headers=keys[SEAT], json={"turn": "idle"})
    rows = {row["name"]: row for row in client.get("/v1/seats", headers=keys["ci"]).json()}
    assert set(rows) == {SEAT, "Exedore 2 - Max"}
    assert rows[SEAT]["missing"] is False
    assert rows["Exedore 2 - Max"]["missing"] is True and rows["Exedore 2 - Max"]["updated_at"] is None
    conn.execute("UPDATE seats SET updated_at = '2000-01-01T00:00:00Z' WHERE name = ?", (SEAT,))
    assert client.get(f"/v1/seats/{SEAT}", headers=keys["ci"]).json()["missing"] is True


def test_check_hides_cleared_and_dead_items_and_puts_blocked_first(client: TestClient, keys: Keys) -> None:
    def make(**fields: object) -> str:
        return str(client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", **fields}).json()["id"])

    blocked = make(state="blocked", blocked_reason="Scott: decide")
    make(state="done", wrap="cleared")
    make(state="wontdo")
    working = make(state="working", result="halfway")
    rows = client.get("/v1/check", headers=keys["ci"]).json()
    assert [row["id"] for row in rows] == [blocked, working]
    assert rows[0]["result_or_blocker"] == "Scott: decide" and rows[1]["result_or_blocker"] == "halfway"


def test_unpromoted_proposals_expire(client: TestClient, keys: Keys, conn: sqlite3.Connection) -> None:
    item = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "state": "proposed"}).json()
    conn.execute("UPDATE items SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (item["id"],))
    assert client.get("/v1/items?state=proposed", headers=keys["ci"]).json() == []
    assert client.get(f"/v1/items/{item['id']}", headers=keys["ci"]).json()["state"] == "abandoned"


def test_bootstrap_mints_a_working_admin_key(
    db: Path, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    out = db.parent / "key"
    argv = ["mint-key", "Scott", "--role", "admin", "--db", str(db), "--out", str(out)]
    assert desk_main.main(argv) == 0
    token = out.read_text().strip()
    assert stat.S_IMODE(out.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    assert token not in capsys.readouterr().out
    assert desk_main.main(argv) == 1
    assert client.get("/v1/identities", headers={"Authorization": f"Bearer {token}"}).status_code == OK
    other = db.parent / "key2"
    assert desk_main.main(["mint-key", "Scott", "--role", "seat", "--db", str(db), "--out", str(other)]) == 1
    assert not other.exists()
    changed = ["mint-key", "Scott", "--role", "seat", "--change-role", "--db", str(db), "--out", str(other)]
    assert desk_main.main(changed) == 0
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["role"] == "seat"
