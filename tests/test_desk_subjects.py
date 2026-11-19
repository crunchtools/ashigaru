"""Subjects: the forge's state of each issue and PR, from the webhook, and the items it settles."""

import sqlite3
from http.client import CREATED, NOT_FOUND, OK
from typing import Any
from urllib.parse import quote

from conftest import Keys
from fastapi.testclient import TestClient
from test_desk_webhooks import deliver, hooked, payload

from ashigaru.desk import store

__all__ = ["hooked"]

JOSUI, SEAT = "Josui Prime", "Exedore 1 - Max"
PR = "crunchtools/ashigaru#191"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def create(client: TestClient, keys: Keys, **body: Any) -> str:
    made = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t"} | body)
    assert made.status_code == CREATED, made.text
    return str(made.json()["id"])


def get(client: TestClient, keys: Keys, item_id: str) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"/v1/items/{item_id}", headers=keys[JOSUI]).json()
    return found


def pull(client: TestClient, action: str, delivery: str, at: str = "2026-11-19T10:00:00Z", **pr: Any) -> None:
    body = payload("pull_request", head={"sha": HEAD}, updated_at=at, **pr)
    assert deliver(client, "pull_request", {**body, "action": action}, delivery).status_code == OK


def merge(client: TestClient, delivery: str = "d-merge", **pr: Any) -> None:
    pull(client, "closed", delivery, state="closed", merged=True, merged_at="2026-11-19T10:00:00Z", **pr)


def test_a_merged_subject_closes_its_seatless_auto_item_and_is_kept(hooked: TestClient, keys: Keys) -> None:
    item = create(hooked, keys, subject_key=PR)
    merge(hooked)
    found = get(hooked, keys, item)
    assert (found["state"], found["result"], found["verified_by"]) == (
        "done",
        f"merged at {HEAD[:12]}",
        "desk",
    )
    subject = hooked.get(f"/v1/subjects/{quote(PR, safe='')}", headers=keys[SEAT]).json()
    assert (subject["state"], subject["head_sha"], subject["source"]) == ("merged", HEAD, "webhook")
    assert hooked.get("/v1/subjects/crunchtools%2Fx%239", headers=keys[SEAT]).status_code == NOT_FOUND


def test_a_pr_closed_unmerged_is_wontdo_and_reopening_queues_it_again(hooked: TestClient, keys: Keys) -> None:
    item = create(hooked, keys, subject_key=PR)
    pull(hooked, "closed", "d-close", state="closed")
    assert (get(hooked, keys, item)["state"], get(hooked, keys, item)["result"]) == (
        "wontdo",
        "closed unmerged at the source",
    )
    pull(hooked, "reopened", "d-reopen", at="2026-11-19T11:00:00Z", state="open")
    found = get(hooked, keys, item)
    assert (found["state"], found["verified_by"], found["closed_at"]) == ("queued", None, None)


def test_a_closed_issue_is_done(hooked: TestClient, keys: Keys) -> None:
    body = payload("issues", state="closed", updated_at="2026-11-19T10:00:00Z")
    item = create(hooked, keys, subject_key=f"crunchtools/ashigaru#{body['issue']['number']}")
    deliver(hooked, "issues", {**body, "action": "closed"})
    assert get(hooked, keys, item)["result"] == "closed at the source"


def test_a_seat_or_scott_keeps_the_item_and_only_hears_it_landed(
    hooked: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    with store.transaction(conn):
        store.write_seat(conn, SEAT, {"status": "free", "lane": "exedore-max"})
    held = create(hooked, keys, subject_key=PR)
    assert hooked.post(f"/v1/items/{held}/assign", headers=keys[JOSUI], json={"seat": SEAT}).status_code == OK
    manual = create(hooked, keys, subject_key="crunchtools/ashigaru#192", mode="manual")
    pull(hooked, "closed", "d-close", number=192, state="closed")
    assert get(hooked, keys, manual)["state"] == "queued" and get(hooked, keys, manual)["landed_at"] is None
    merge(hooked)
    merge(hooked, "d-merge-192", number=192)
    for item, state in ((held, "assigned"), (manual, "queued")):
        found = get(hooked, keys, item)
        assert (found["state"], found["verified_by"]) == (state, None) and found["landed_at"]


def test_an_older_delivery_never_moves_a_subject_back_and_merged_stays_merged(
    hooked: TestClient, keys: Keys
) -> None:
    merge(hooked)
    pull(hooked, "edited", "d-old", at="2026-11-19T09:00:00Z", state="open", title="older")
    pull(hooked, "edited", "d-new", at="2026-11-19T12:00:00Z", state="closed", title="newer")
    subject = hooked.get(f"/v1/subjects/{quote(PR, safe='')}", headers=keys[SEAT]).json()
    assert (subject["state"], subject["title"]) == ("merged", "newer")


def test_a_linked_pr_closes_its_story_only_on_merge(hooked: TestClient, keys: Keys) -> None:
    story = create(hooked, keys, pr=f"https://github.com/{PR.replace('#', '/pull/')}")
    assert get(hooked, keys, story)["pr"] == PR
    pull(hooked, "closed", "d-close", state="closed")
    assert get(hooked, keys, story)["state"] == "queued"
    merge(hooked)
    assert get(hooked, keys, story)["state"] == "done"


def test_a_read_settles_an_item_linked_after_the_merge(hooked: TestClient, keys: Keys) -> None:
    merge(hooked)
    story = create(hooked, keys)
    hooked.patch(f"/v1/items/{story}", headers=keys[JOSUI], json={"pr": PR})
    assert get(hooked, keys, story)["state"] == "queued"
    assert hooked.get("/v1/status", headers=keys[SEAT]).status_code == OK
    assert get(hooked, keys, story)["state"] == "done"
