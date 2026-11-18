"""Scott's hold, inferred by the Desk from a seat's own reports: no seat can claim one or drop one."""

from http.client import OK
from typing import Any

from conftest import Keys
from fastapi.testclient import TestClient

JOSUI, MINE = "Josui Prime", "Exedore 1 - Max"


def report(client: TestClient, keys: Keys, **fields: object) -> dict[str, Any]:
    response = client.patch(f"/v1/seats/{MINE}", headers=keys[MINE], json=fields)
    assert response.status_code == OK, response.text
    seat: dict[str, Any] = response.json()
    return seat


def items(client: TestClient, keys: Keys) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = client.get("/v1/items", headers=keys[JOSUI]).json()
    return found


def free_seat(client: TestClient, keys: Keys) -> None:
    body = {"lane": "exedore-max", "status": "free"}
    assert client.patch(f"/v1/seats/{MINE}", headers=keys[JOSUI], json=body).status_code == OK


def test_a_typed_first_prompt_on_a_free_seat_is_scotts_manual_work(client: TestClient, keys: Keys) -> None:
    free_seat(client, keys)
    seat = report(client, keys, turn="active", task="look at the build")
    (work,) = items(client, keys)
    assert (seat["status"], seat["owner"], seat["hold"], seat["item"]) == (
        "busy",
        "Scott",
        "busy-manual",
        work["id"],
    )
    assert (work["mode"], work["state"], work["seat"], work["task"]) == (
        "manual",
        "working",
        MINE,
        "look at the build",
    )
    assert work["lane"] == "exedore-max"
    report(client, keys, turn="idle")
    again = report(client, keys, turn="active", task="a later session's first prompt")
    assert again["item"] == work["id"] and len(items(client, keys)) == 1


def test_claude_codes_own_turns_never_start_a_hold(client: TestClient, keys: Keys) -> None:
    free_seat(client, keys)
    seat = report(client, keys, turn="active")
    assert seat["hold"] == "free" and items(client, keys) == []


def test_a_seat_on_an_assigned_item_stays_on_it(client: TestClient, keys: Keys) -> None:
    free_seat(client, keys)
    item = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t"}).json()["id"]
    assert client.post(f"/v1/items/{item}/assign", headers=keys[JOSUI], json={"seat": MINE}).status_code == OK
    seat = report(client, keys, turn="active", task="a typed prompt")
    assert (seat["item"], seat["owner"], seat["hold"]) == (item, None, "busy-auto")
    assert len(items(client, keys)) == 1


def test_a_clear_releases_the_hold_and_nothing_else_does(client: TestClient, keys: Keys) -> None:
    free_seat(client, keys)
    work = report(client, keys, turn="active", task="t")["item"]
    assert report(client, keys, turn="idle")["hold"] == "busy-manual"
    assert report(client, keys, turn="idle", task="something else")["hold"] == "busy-manual"
    seat = report(client, keys, turn="idle", task=None)
    assert (seat["hold"], seat["owner"], seat["item"]) == ("free", None, None)
    assert client.get(f"/v1/items/{work}", headers=keys[JOSUI]).json()["wrap"] == "cleared"


def test_a_wrap_releases_the_hold(client: TestClient, keys: Keys) -> None:
    free_seat(client, keys)
    work = report(client, keys, turn="active", task="t")["item"]
    wrapped = client.patch(
        f"/v1/items/{work}", headers=keys[JOSUI], json={"wrap": "cleared", "state": "done"}
    )
    assert wrapped.status_code == OK
    seat = client.get(f"/v1/seats/{MINE}", headers=keys[JOSUI]).json()
    assert (seat["hold"], seat["owner"]) == ("free", None)


def test_the_hold_follows_the_items_mode(client: TestClient, keys: Keys) -> None:
    free_seat(client, keys)
    work = report(client, keys, turn="active", task="t")["item"]
    client.patch(f"/v1/items/{work}", headers=keys[JOSUI], json={"mode": "auto"})
    assert client.get(f"/v1/seats/{MINE}", headers=keys[JOSUI]).json()["hold"] == "busy-auto"
    client.patch(f"/v1/items/{work}", headers=keys[JOSUI], json={"mode": "manual"})
    assert client.get(f"/v1/seats/{MINE}", headers=keys[JOSUI]).json()["hold"] == "busy-manual"


def test_josuis_own_seat_writes_infer_nothing(client: TestClient, keys: Keys) -> None:
    body = {"lane": "exedore-max", "status": "free", "turn": "active", "task": "set by hand"}
    assert client.patch(f"/v1/seats/{MINE}", headers=keys[JOSUI], json=body).json()["hold"] == "free"
    assert items(client, keys) == []
