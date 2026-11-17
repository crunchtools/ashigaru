"""The API's own rules, and the committed contract staying equal to the code."""

import json
import sqlite3
import stat
from http.client import CONFLICT, CREATED, NOT_FOUND, OK, UNPROCESSABLE_ENTITY
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


def test_subject_key_and_id_are_unique(client: TestClient, keys: Keys) -> None:
    body = {"task": "t", "subject_key": "crunchtools/ashigaru#1"}
    first = client.post("/v1/items", headers=keys[JOSUI], json=body)
    assert first.status_code == CREATED and first.json()["id"] == "a1" and first.json()["state"] == "queued"
    assert client.post("/v1/items", headers=keys[JOSUI], json=body).status_code == CONFLICT
    assert (
        client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "id": "a1"}).status_code == CONFLICT
    )
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
