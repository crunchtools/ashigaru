"""Outcomes: what each piece of work serves, named on create, inherited by a story's review and
fix-ups, and grouped by in `GET /v1/work`."""

import sqlite3
from http.client import CREATED, OK, UNPROCESSABLE_ENTITY
from typing import Any

from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import outcomes, store, subjects
from ashigaru.desk.forges import IntakeItem
from ashigaru.desk.model import Kind

JOSUI = "Josui Prime"
INSTRUCTION = "Make the status board say what each seat's work is for, so Scott sees it at a glance"


def create(client: TestClient, keys: Keys, **body: Any) -> dict[str, Any]:
    made = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t"} | body)
    assert made.status_code == CREATED, made.text
    result: dict[str, Any] = made.json()
    return result


def rows(client: TestClient, keys: Keys) -> list[dict[str, Any]]:
    got = client.get("/v1/work", headers=keys[JOSUI])
    assert got.status_code == OK, got.text
    found: list[dict[str, Any]] = got.json()["items"]
    return found


def test_a_ref_names_one_outcome_recorded_once(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    first = create(client, keys, outcome="RT#1652")
    second = create(client, keys, outcome="rt #1652")
    outcome = store.get_item(conn, first["parent"]) or {}
    assert first["parent"] == second["parent"]
    assert (outcome["kind"], outcome["subject_key"], outcome["mode"]) == ("outcome", "RT#1652", "manual")
    assert create(client, keys, outcome=outcome["id"])["parent"] == outcome["id"], "an id names it too"


def test_an_instruction_with_no_ticket_is_a_one_off_outcome(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    named = (
        store.get_item(conn, create(client, keys, outcome="board", outcome_text=INSTRUCTION)["parent"]) or {}
    )
    bare = store.get_item(conn, create(client, keys, outcome_text=INSTRUCTION)["parent"]) or {}
    assert (named["task"], named["note"], named["subject_key"]) == ("board", INSTRUCTION, None)
    assert bare["task"] == INSTRUCTION[: outcomes.LABEL] and bare["note"] == INSTRUCTION
    assert named["id"] != bare["id"]


def test_an_unknown_outcome_without_its_text_is_422(client: TestClient, keys: Keys) -> None:
    made = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t", "outcome": "the board"})
    assert made.status_code == UNPROCESSABLE_ENTITY
    assert "give its instruction text" in made.text


def test_an_update_sets_the_outcome(client: TestClient, keys: Keys) -> None:
    story = create(client, keys)
    patched = client.patch(f"/v1/items/{story['id']}", headers=keys[JOSUI], json={"outcome": "RT#9"})
    assert patched.status_code == OK, patched.text
    assert patched.json()["parent"]


def test_a_review_and_a_filed_child_inherit_the_storys_outcome(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    story = create(client, keys, outcome="RT#1652", pr="crunchtools/ashigaru#221")
    review = create(client, keys, subject_key="crunchtools/ashigaru#221", task="review #221")
    with store.transaction(conn):
        child = store.insert_item(
            conn, {"task": "fix-up", "filed_under": story["id"], "state": "queued"}, JOSUI
        )
    labels = {row["id"]: row["outcome_label"] for row in rows(client, keys)}
    assert labels[story["id"]] == labels[review["id"]] == labels[child] == "RT#1652"


def test_rows_group_by_outcome_needs_you_first_and_none_last(client: TestClient, keys: Keys) -> None:
    loose = create(client, keys, task="no outcome")
    quiet = create(client, keys, outcome="RT#1")
    stuck = create(client, keys, outcome="RT#2", mode="manual", state="blocked", blocked_reason="pick one")
    sibling = create(client, keys, outcome="RT#2")
    order = [row["id"] for row in rows(client, keys)]
    assert order.index(stuck["id"]) < order.index(sibling["id"]) < order.index(quiet["id"])
    assert order[-1] == loose["id"]
    found = {row["id"]: row for row in rows(client, keys)}
    assert found[stuck["id"]]["needs_you"] and found[stuck["id"]]["step"] == "blocked: pick one"
    assert found[loose["id"]]["outcome_label"] is None


def test_next_says_overdue_and_landed_before_the_items_own_next() -> None:
    now = "2026-11-19T16:00:00Z"
    row = {"state": "working", "check_after": "2026-11-19T15:00:00Z", "next": "keep going", "mode": "auto"}
    assert outcomes.step(row, now) == "overdue since 15:00"
    assert outcomes.step(row | {"landed_at": now}, now) == "landed: wrap it"
    assert outcomes.step(row | {"check_after": None}, now) == "keep going"


def test_next_says_a_prs_ci_is_red_from_one_query(
    client: TestClient, keys: Keys, conn: sqlite3.Connection
) -> None:
    red = create(client, keys, pr="crunchtools/x#5", next="keep going")
    green = create(client, keys, pr="crunchtools/x#6", next="keep going")
    with store.transaction(conn):
        for number, verdict in ((5, "failure"), (6, "success")):
            sha = f"{number}" * 40
            subjects.record(
                conn,
                IntakeItem(
                    subject_key=f"crunchtools/x#{number}",
                    title="t",
                    ref=f"https://github.com/crunchtools/x/pull/{number}",
                    source="github",
                    repo="crunchtools/x",
                    kind=Kind.STORY,
                    head=sha,
                ),
                "webhook",
            )
            stamp = store.now()
            store.write_check(
                conn,
                {
                    "repo": "crunchtools/x",
                    "sha": sha,
                    "key": "ci",
                    "name": "ci",
                    "verdict": verdict,
                    "run": "1",
                    "url": None,
                    "source": "github",
                    "updated_at": stamp,
                    "received_at": stamp,
                },
            )
    queries: list[str] = []
    conn.set_trace_callback(queries.append)
    try:
        found = {row["id"]: row for row in outcomes.arrange(conn, store.check(conn))}
    finally:
        conn.set_trace_callback(None)
    assert found[red["id"]]["step"] == "crunchtools/x#5 CI red"
    assert found[green["id"]]["step"] == "keep going"
    assert len([query for query in queries if "ci_checks" in query]) == 1
