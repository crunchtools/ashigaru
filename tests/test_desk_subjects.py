"""Subjects: the forge's state of each issue and PR, from the webhook, and the items it settles."""

import dataclasses
import sqlite3
from http.client import CREATED, FORBIDDEN, NOT_FOUND, OK, UNPROCESSABLE_ENTITY
from typing import Any
from urllib.parse import quote

import pytest
from conftest import Keys
from fastapi.testclient import TestClient
from test_desk_webhooks import deliver, hooked, payload

from ashigaru.desk import store, subjects
from ashigaru.desk.forges import IntakeItem
from ashigaru.desk.model import Kind

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
    merge(hooked, title="at merge")
    pull(hooked, "edited", "d-old", at="2026-11-19T09:00:00Z", state="open", title="older")
    subject = hooked.get(f"/v1/subjects/{quote(PR, safe='')}", headers=keys[SEAT]).json()
    assert (subject["state"], subject["title"]) == ("merged", "at merge")
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


def events(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM events").fetchone()[0])


def test_a_story_whose_issue_is_open_and_whose_pr_merged_stays_done(
    hooked: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    issue = payload("issues", state="open", updated_at="2026-11-19T10:00:00Z")
    deliver(hooked, "issues", {**issue, "action": "opened"}, "d-issue")
    story = str((store.item_by_subject(conn, f"crunchtools/ashigaru#{issue['issue']['number']}") or {})["id"])
    hooked.patch(f"/v1/items/{story}", headers=keys[JOSUI], json={"pr": PR, "mode": "auto"})
    merge(hooked)
    assert get(hooked, keys, story)["state"] == "done"
    settled = events(conn)
    for _ in range(3):
        hooked.get("/v1/status", headers=keys[SEAT])
    assert get(hooked, keys, story)["state"] == "done" and events(conn) == settled


def test_a_read_with_nothing_due_writes_nothing(
    hooked: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    create(hooked, keys, subject_key="crunchtools/ashigaru#192", mode="manual")
    create(hooked, keys, pr=PR)
    pull(hooked, "closed", "d-192", number=192, state="closed")
    pull(hooked, "closed", "d-191", state="closed")
    quiet = events(conn)
    hooked.get("/v1/work", headers=keys[SEAT])
    hooked.get("/v1/status", headers=keys[SEAT])
    assert events(conn) == quiet
    due = {"key": None, "actor": subjects.ACTOR, "source": subjects.SOURCE}
    assert conn.execute(subjects.DUE, due).fetchone() is None, "hygiene would take the write lock"


def test_only_settling_reopens_what_settling_closed(hooked: TestClient, keys: Keys) -> None:
    closed = create(hooked, keys, subject_key=PR)
    hooked.patch(f"/v1/items/{closed}", headers=keys[JOSUI], json={"state": "done", "verified_by": "desk"})
    pull(hooked, "reopened", "d-open", state="open")
    hooked.get("/v1/status", headers=keys[SEAT])
    assert get(hooked, keys, closed)["state"] == "done"


def test_an_issue_reopened_after_settling_closed_it_is_queued_again(hooked: TestClient, keys: Keys) -> None:
    body = payload("issues", state="closed", updated_at="2026-11-19T10:00:00Z")
    item = create(hooked, keys, subject_key=f"crunchtools/ashigaru#{body['issue']['number']}")
    deliver(hooked, "issues", {**body, "action": "closed"}, "d-close")
    assert get(hooked, keys, item)["state"] == "done"
    reopened = payload("issues", state="open", updated_at="2026-11-19T11:00:00Z")
    deliver(hooked, "issues", {**reopened, "action": "reopened"}, "d-reopen")
    assert get(hooked, keys, item)["state"] == "queued"


def test_a_review_delivery_is_recorded_on_the_subject(hooked: TestClient, keys: Keys) -> None:
    body = payload("pull_request", head={"sha": HEAD}, updated_at="2026-11-19T10:00:00Z")
    review = {"action": "submitted", "review": {"state": "APPROVED"}, "pull_request": body["pull_request"]}
    deliver(hooked, "pull_request_review", {**body, **review}, "d-review")
    subject = hooked.get(f"/v1/subjects/{quote(PR, safe='')}", headers=keys[SEAT]).json()
    assert (subject["review"], subject["source"], subject["state"]) == ("approved", "webhook", "open")


def test_a_reopened_issue_does_not_requeue_a_story_whose_pr_merged(hooked: TestClient, keys: Keys) -> None:
    closed = payload("issues", state="closed", updated_at="2026-11-19T10:00:00Z")
    story = create(hooked, keys, subject_key=f"crunchtools/ashigaru#{closed['issue']['number']}", pr=PR)
    deliver(hooked, "issues", {**closed, "action": "closed"}, "d-close")
    merge(hooked)
    reopened = payload("issues", state="open", updated_at="2026-11-19T11:00:00Z")
    deliver(hooked, "issues", {**reopened, "action": "reopened"}, "d-reopen")
    assert get(hooked, keys, story)["state"] == "done"


def test_a_delivery_in_the_same_second_lands(hooked: TestClient, keys: Keys) -> None:
    pull(hooked, "opened", "d-1", state="open", title="first")
    pull(hooked, "edited", "d-2", state="open", title="same second")
    subject = hooked.get(f"/v1/subjects/{quote(PR, safe='')}", headers=keys[SEAT]).json()
    assert subject["title"] == "same second"


def test_a_work_read_settles_too(hooked: TestClient, keys: Keys) -> None:
    merge(hooked)
    story = create(hooked, keys)
    hooked.patch(f"/v1/items/{story}", headers=keys[JOSUI], json={"pr": PR})
    assert hooked.get("/v1/work", headers=keys[SEAT]).status_code == OK
    assert get(hooked, keys, story)["state"] == "done"


@pytest.mark.parametrize("pr", ["", "  ", None])
def test_a_blank_pr_is_none(hooked: TestClient, keys: Keys, pr: str | None) -> None:
    assert get(hooked, keys, create(hooked, keys, pr=pr))["pr"] is None


def test_a_pr_in_a_repo_named_issues_is_a_pr(hooked: TestClient, keys: Keys) -> None:
    story = create(hooked, keys, pr="https://github.com/crunchtools/issues/pull/191")
    assert get(hooked, keys, story)["pr"] == "crunchtools/issues#191"


@pytest.mark.parametrize(
    "pr",
    [
        "RT#1500",
        "https://rt.crunchtools.com/Ticket/Display.html?id=7",
        "not a pr",
        "https://github.com/crunchtools/ashigaru/issues/191",
    ],
)
def test_a_pr_must_name_a_pull_request(hooked: TestClient, keys: Keys, pr: str) -> None:
    made = hooked.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "pr": pr})
    assert made.status_code == UNPROCESSABLE_ENTITY


def test_a_seat_sets_the_pr_of_its_own_item_only(
    hooked: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    other = "Exedore 2 - Max"
    with store.transaction(conn):
        for name in (SEAT, other):
            store.write_seat(conn, name, {"status": "free", "lane": "exedore-max"})
    mine, theirs = create(hooked, keys), create(hooked, keys)
    hooked.post(f"/v1/items/{mine}/assign", headers=keys[JOSUI], json={"seat": SEAT})
    hooked.post(f"/v1/items/{theirs}/assign", headers=keys[JOSUI], json={"seat": other})
    assert hooked.patch(f"/v1/items/{mine}", headers=keys[SEAT], json={"pr": PR}).json()["pr"] == PR
    assert hooked.patch(f"/v1/items/{theirs}", headers=keys[SEAT], json={"pr": PR}).status_code == FORBIDDEN


def test_an_item_with_two_closed_subjects_settles_by_the_merged_one(
    hooked: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    story = create(hooked, keys, subject_key="crunchtools/ashigaru#193", pr="crunchtools/ashigaru#194")
    closed = IntakeItem(
        "crunchtools/ashigaru#193", "t", "u", "github", "crunchtools/ashigaru", Kind.STORY, closed=True
    )
    merged = dataclasses.replace(closed, subject_key="crunchtools/ashigaru#194", merged=True, head=HEAD)
    with store.transaction(conn):
        subjects.record(conn, closed, "webhook")
        subjects.record(conn, merged, "webhook")
    assert subjects.hygiene(conn) == [story]
    found = get(hooked, keys, story)
    assert (found["state"], found["result"]) == ("done", f"merged at {HEAD[:12]}")
