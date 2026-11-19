"""The hosted MCP: codes as credentials, and the same authority as the REST API, identity by identity."""

import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import CONFLICT, CREATED, FORBIDDEN, NOT_FOUND, OK, UNPROCESSABLE_ENTITY
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from conftest import Keys, RequiredChecks
from fastapi.testclient import TestClient

from ashigaru.desk import mcp as mcp_package
from ashigaru.desk import store
from ashigaru.desk.api import create_app
from ashigaru.desk.mcp import codes
from ashigaru.desk.model import Verdict

JOSUI = "Josui Prime"
SEAT = "Exedore 1 - Max"
OTHER = "Exedore 2 - Max"
IDENTITIES = (JOSUI, SEAT, OTHER, "ci")
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def mint_url(client: TestClient, keys: Keys, name: str) -> str:
    response = client.post(f"/v1/identities/{name}/mcp", headers=keys[JOSUI], json={"label": "test"})
    assert response.status_code == CREATED
    url: str = response.json()["url"]
    return url.removeprefix("http://testserver")


def call(client: TestClient, path: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """One tools/call, as an MCP client sends it; returns the JSON-RPC result."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    response = client.post(path, headers=MCP_HEADERS, json=body)
    assert response.status_code == OK, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


def mcp_status(result: dict[str, Any]) -> int:
    """The REST status a tool result stands for: the API's code from a refusal, else 200."""
    if not result["isError"]:
        return OK
    return int(result["content"][0]["text"].split(":", 1)[0])


@pytest.fixture
def urls(client: TestClient, keys: Keys) -> dict[str, str]:
    return {name: mint_url(client, keys, name) for name in IDENTITIES}


@pytest.fixture
def access(keys: Keys, urls: dict[str, str]) -> tuple[Keys, dict[str, str]]:
    return keys, urls


def test_initialize_and_list_tools(client: TestClient, urls: dict[str, str]) -> None:
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}
        },
    }  # fmt: skip
    assert client.post(urls[SEAT], headers=MCP_HEADERS, json=init).status_code == OK
    listed = client.post(
        urls[SEAT], headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "desk_usage", "desk_agents", "desk_status", "desk_work", "desk_queue", "desk_outcomes", "desk_list",
        "desk_get", "desk_create", "desk_update", "desk_mark", "desk_report", "desk_assign", "desk_auto",
        "desk_manual", "desk_wait", "desk_wrap", "desk_claim", "desk_file",
    }  # fmt: skip
    for tool in listed.json()["result"]["tools"]:
        assert not {"who", "identity", "caller"} & set(tool["inputSchema"].get("properties", {}))


def test_codes_are_stored_hashed(client: TestClient, keys: Keys, conn: sqlite3.Connection) -> None:
    url = mint_url(client, keys, SEAT)
    code_id, _, secret = url.removeprefix("/mcp/").partition("_")
    row = conn.execute("SELECT * FROM mcp_codes WHERE code_id = ?", (code_id,)).fetchone()
    assert secret not in "".join(str(value) for value in row)
    assert codes.authenticate(conn, f"{code_id}_{secret}x") is None
    who = codes.authenticate(conn, f"{code_id}_{secret}")
    assert who is not None and who.name == SEAT


@pytest.mark.parametrize("path", ["/mcp/nope", "/mcp/abc_def", "/mcp/_", "/mcp/", "/mcp"])
def test_an_unknown_code_is_not_found(client: TestClient, urls: dict[str, str], path: str) -> None:
    assert client.post(path, headers=MCP_HEADERS, json={}).status_code == NOT_FOUND


def test_only_an_admin_mints(client: TestClient, keys: Keys) -> None:
    for name in (SEAT, "ci"):
        assert client.post(f"/v1/identities/{name}/mcp", headers=keys[name], json={}).status_code == FORBIDDEN
    assert client.post("/v1/identities/nobody/mcp", headers=keys[JOSUI], json={}).status_code == NOT_FOUND


def test_list_and_revoke_are_admin_or_owner(client: TestClient, keys: Keys) -> None:
    url = mint_url(client, keys, SEAT)
    code_id = url.removeprefix("/mcp/").partition("_")[0]
    assert client.get(f"/v1/identities/{SEAT}/mcp", headers=keys[OTHER]).status_code == FORBIDDEN
    listed = client.get(f"/v1/identities/{SEAT}/mcp", headers=keys[SEAT]).json()
    assert [row["code_id"] for row in listed] == [code_id] and "url" not in listed[0]
    assert client.delete(f"/v1/identities/{SEAT}/mcp/{code_id}", headers=keys[OTHER]).status_code == FORBIDDEN
    assert (
        client.delete(f"/v1/identities/{OTHER}/mcp/{code_id}", headers=keys[OTHER]).status_code == NOT_FOUND
    )
    assert call(client, url, "desk_agents")["isError"] is False
    revoked = client.delete(f"/v1/identities/{SEAT}/mcp/{code_id}", headers=keys[SEAT])
    assert revoked.status_code == OK and revoked.json()["revoked_at"]
    assert client.post(url, headers=MCP_HEADERS, json={}).status_code == NOT_FOUND


def test_a_disabled_identity_loses_its_url(
    client: TestClient, urls: dict[str, str], conn: sqlite3.Connection
) -> None:
    conn.execute("UPDATE identities SET disabled_at = 'now' WHERE name = ?", (SEAT,))
    assert client.post(urls[SEAT], headers=MCP_HEADERS, json={}).status_code == NOT_FOUND


def test_the_code_names_the_actor(client: TestClient, urls: dict[str, str], conn: sqlite3.Connection) -> None:
    store.insert_item(conn, {"id": "a1", "task": "t", "seat": SEAT, "state": "working"}, JOSUI)
    assert (
        call(client, urls[SEAT], "desk_update", {"item_id": "a1", "fields": {"result": "r"}})["isError"]
        is False
    )
    assert store.events(conn, "a1")[-1]["actor"] == SEAT


# Parity: each action, by each identity, gets the same answer through MCP as through REST.

ADMIN, OWNER, OWNER_OR_ADMIN, SEATS, ANYONE = (
    {JOSUI}, {SEAT}, {JOSUI, SEAT}, {SEAT, OTHER}, set(IDENTITIES)
)  # fmt: skip
CASES: list[tuple[str, dict[str, Any], str, str, dict[str, Any] | None, set[str]]] = [
    # tool, arguments, REST method, REST path ({id} is the item), REST body, who succeeds
    ("desk_get", {}, "GET", "/v1/items/{id}", None, ANYONE),
    ("desk_update", {"fields": {"result": "r"}}, "PATCH", "/v1/items/{id}", {"result": "r"}, OWNER_OR_ADMIN),
    ("desk_update", {"fields": {"state": "done"}}, "PATCH", "/v1/items/{id}", {"state": "done"}, ADMIN),
    ("desk_update", {"fields": {"seat": OTHER}}, "PATCH", "/v1/items/{id}", {"seat": OTHER}, ADMIN),
    ("desk_report", {"verb": "DONE", "text": "ok"}, "POST", "/v1/items/{id}/report",
     {"verb": "DONE", "text": "ok"}, OWNER_OR_ADMIN),
    ("desk_file", {"task": "child"}, "POST", "/v1/items/{id}/file", {"task": "child"}, OWNER_OR_ADMIN),
    ("desk_auto", {}, "PATCH", "/v1/items/{id}", {"mode": "auto"}, ADMIN),
    ("desk_manual", {}, "PATCH", "/v1/items/{id}", {"mode": "manual"}, ADMIN),
    ("desk_wrap", {"stage": "wrapping"}, "POST", "/v1/items/{id}/wrap", {"stage": "wrapping"}, ADMIN),
    ("desk_wrap", {"force": True}, "POST", "/v1/items/{id}/wrap", {"force": True}, ADMIN),
    ("desk_assign", {"seat": OTHER}, "POST", "/v1/items/{id}/assign", {"seat": OTHER}, ADMIN),
    ("desk_claim", {}, "POST", "/v1/items/{id}/claim", None, SEATS),
]  # fmt: skip
Case = tuple[str, dict[str, Any], str, str, dict[str, Any] | None, set[str]]


def arrange(conn: sqlite3.Connection, tool: str, item_id: str) -> None:
    """An item the action can succeed on for someone: SEAT's working story, or a claimable one."""
    if tool in ("desk_claim", "desk_assign"):
        store.insert_item(conn, {"id": item_id, "task": "t", "lane": "exedore-max", "state": "queued"}, JOSUI)
    else:
        store.insert_item(conn, {"id": item_id, "task": "t", "seat": SEAT, "state": "working"}, JOSUI)
    for seat in (SEAT, OTHER):
        with store.transaction(conn):
            store.write_seat(conn, seat, {"lane": "exedore-max", "status": "free", "item": None})


@pytest.mark.parametrize(
    ("name", "case"),
    [(name, case) for case in CASES for name in IDENTITIES],
    ids=[f"{case[0]}{index}-{name}" for index, case in enumerate(CASES) for name in IDENTITIES],
)
def test_mcp_authority_matches_rest(
    client: TestClient, conn: sqlite3.Connection, access: tuple[Keys, dict[str, str]], name: str, case: Case
) -> None:
    """Twin items, one acted on through REST and one through MCP, by the same identity."""
    keys, urls = access
    tool, arguments, method, path, body, allowed = case
    arrange(conn, tool, "rest")
    rest = client.request(method, path.format(id="rest"), headers=keys[name], json=body)
    arrange(conn, tool, "mcp")
    result = call(client, urls[name], tool, {"item_id": "mcp", **arguments})
    rest_status = OK if rest.status_code == CREATED else rest.status_code
    assert mcp_status(result) == rest_status, (rest.json(), result)
    assert (rest_status == OK) == (name in allowed), rest.json()
    if not result["isError"]:
        for field in ("state", "seat", "mode", "wrap", "result", "task"):
            assert result["structuredContent"][field] == rest.json()[field]


def test_desk_create_is_admin_only_and_names_a_taken_subject_key(
    client: TestClient, urls: dict[str, str]
) -> None:
    fields = {"task": "t", "subject_key": "RT#7", "filed_by": SEAT}
    created = call(client, urls[JOSUI], "desk_create", {"fields": fields})["structuredContent"]
    assert (created["state"], created["seat"], created["filed_by"]) == ("queued", None, SEAT)
    again = call(client, urls[JOSUI], "desk_create", {"fields": fields})
    assert mcp_status(again) == CONFLICT and created["id"] in again["content"][0]["text"]
    assert mcp_status(call(client, urls[SEAT], "desk_create", {"fields": {"task": "t"}})) == FORBIDDEN


def test_desk_mark_reads_for_all_and_sets_for_an_admin(
    client: TestClient, keys: Keys, urls: dict[str, str]
) -> None:
    assert mcp_status(call(client, urls[SEAT], "desk_mark", {"name": "sweep"})) == NOT_FOUND
    assert mcp_status(call(client, urls[SEAT], "desk_mark", {"name": "sweep", "value": "t1"})) == FORBIDDEN
    set_ = call(client, urls[JOSUI], "desk_mark", {"name": "sweep", "value": "t1"})["structuredContent"]
    read = call(client, urls["ci"], "desk_mark", {"name": "sweep"})["structuredContent"]
    assert set_ == read == client.get("/v1/marks/sweep", headers=keys["ci"]).json()
    assert call(client, urls[JOSUI], "desk_mark", {"name": "Bad_Name", "value": "t1"})["isError"] is True


def test_views_answer_every_identity(client: TestClient, keys: Keys, urls: dict[str, str]) -> None:
    for name in IDENTITIES:
        for tool, path in (
            ("desk_usage", "/v1/usage"),
            ("desk_agents", "/v1/seats"),
            ("desk_outcomes", "/v1/outcomes"),
        ):
            result = call(client, urls[name], tool)
            rest = client.get(path, headers=keys[name]).json()
            content = result["structuredContent"]
            assert content.get("result", content) == rest


def test_queue_lists_queued_then_proposed(
    client: TestClient, keys: Keys, urls: dict[str, str], conn: sqlite3.Connection
) -> None:
    store.insert_item(conn, {"id": "a1", "task": "q", "state": "queued", "after": ["a2"]}, JOSUI)
    store.insert_item(conn, {"id": "a2", "task": "p", "state": "proposed"}, JOSUI)
    store.insert_item(conn, {"id": "a3", "task": "w", "state": "working"}, JOSUI)
    content = call(client, urls[SEAT], "desk_queue")["structuredContent"]
    assert [(row["id"], row["waits_on"]) for row in content["queued"]] == [
        ("a1", [{"id": "a2", "state": "proposed"}])
    ]
    assert [row["id"] for row in content["proposed"]] == ["a2"]
    assert content == client.get("/v1/queue", headers=keys[SEAT]).json()


CLIENT_LOGS = ("httpx", "httpcore")
"""The test's own client, which logs the URL it was asked to fetch."""


def serve(db: Path) -> Iterator[str]:
    """The Desk under a real uvicorn at TRACE, access log on, on a free port; yields its base URL."""
    config = uvicorn.Config(create_app(db), port=0, log_level="trace", access_log=True, log_config=None)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_no_log_and_no_header_carries_a_code(
    db: Path, client: TestClient, urls: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Every request shape under /mcp/<code>, through uvicorn's own logging at its most verbose."""
    secret = urls[SEAT].removeprefix("/mcp/")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "desk_agents", "arguments": {}},
    }
    caplog.set_level(1)
    with contextmanager(serve)(db) as base, httpx.Client(base_url=base) as http:
        responses = [
            http.post(f"/mcp/{secret}", headers=MCP_HEADERS, json=init),
            http.post(f"/mcp/{secret}/", headers=MCP_HEADERS, json=init),
            http.post(f"/mcp/{secret}?x=1", headers=MCP_HEADERS, json=init),
            http.get(f"/mcp/{secret}/deeper/"),
            http.get(f"/mcp/{secret}x/"),
        ]
    assert [response.status_code for response in responses[:3]] == [OK, OK, OK]
    assert [response.status_code for response in responses[3:]] == [NOT_FOUND, NOT_FOUND]
    server_log = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith(CLIENT_LOGS)
    )
    assert "/mcp/<code>" in server_log and "ASGI [" in server_log, "the probe logged nothing"
    assert secret not in server_log
    for response in responses:
        assert secret not in str(response.headers)


def test_without_fastmcp_there_is_no_mcp(
    db: Path, keys: Keys, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RPM Desk: no `mcp` extra, so no /mcp route, and minting a code says why instead of
    handing out a dead URL. Listing and revoking still work for codes minted elsewhere."""
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    monkeypatch.delitem(sys.modules, "ashigaru.desk.mcp.server", raising=False)
    monkeypatch.delattr(mcp_package, "server", raising=False)
    code_id, code = codes.mint(conn, SEAT)
    with TestClient(create_app(db)) as bare:
        minted = bare.post(f"/v1/identities/{SEAT}/mcp", headers=keys[JOSUI], json={})
        assert minted.status_code == CONFLICT and "mcp" in minted.json()["detail"]
        assert bare.post(f"/mcp/{code}", headers=MCP_HEADERS, json={}).status_code == NOT_FOUND
        assert [
            row["code_id"] for row in bare.get(f"/v1/identities/{SEAT}/mcp", headers=keys[SEAT]).json()
        ] == [code_id]


def test_desk_wait_reads_what_rest_reads(
    client: TestClient, keys: Keys, urls: dict[str, str], conn: sqlite3.Connection
) -> None:
    sha = "a" * 40
    check = {"repo": "crunchtools/ashigaru", "sha": sha, "key": "workflow:1:push", "name": "CI (push)"}
    with store.transaction(conn):
        store.write_check(
            conn,
            {**check, "verdict": "failure", "run": "1", "url": None, "source": "github", "updated_at": "t"},
        )
    query: dict[str, str | int] = {"repo": "crunchtools/ashigaru", "sha": sha, "timeout": 0}
    rest = client.get("/v1/ci/wait", headers=keys[SEAT], params=query).json()
    result = call(client, urls[SEAT], "desk_wait", query)
    assert not result["isError"] and result["structuredContent"]["result"] == rest
    assert rest["status"] == "failure" and rest["failing"] == ["CI (push)"]
    refused = call(client, urls[SEAT], "desk_wait", {**query, "sha": sha[:12]})
    assert mcp_status(refused) == UNPROCESSABLE_ENTITY


def test_desk_work_is_the_round(
    client: TestClient, keys: Keys, urls: dict[str, str], conn: sqlite3.Connection
) -> None:
    store.insert_item(conn, {"id": "a1", "task": "w", "seat": SEAT, "state": "working"}, JOSUI)
    store.insert_item(conn, {"id": "a2", "task": "q", "state": "queued"}, JOSUI)
    for name in IDENTITIES:
        content = call(client, urls[name], "desk_work")["structuredContent"]
        assert {"items", "seats", "summary", "queue", "catch_up"} <= set(content)
        assert content == client.get("/v1/work", headers=keys[name]).json()
    assert [row["id"] for row in content["queue"]["queued"]] == ["a2"]
    shown = call(client, urls[SEAT], "desk_work", {"all": True})["structuredContent"]
    assert shown == client.get("/v1/work", headers=keys[SEAT], params={"all": True}).json()


def test_desk_work_advance_is_admin_only_and_moves_the_sweep_mark(
    client: TestClient, keys: Keys, urls: dict[str, str]
) -> None:
    refused = call(client, urls[SEAT], "desk_work", {"advance": True})
    assert mcp_status(refused) == FORBIDDEN
    assert mcp_status(call(client, urls[SEAT], "desk_mark", {"name": "sweep"})) == NOT_FOUND
    advanced = call(client, urls[JOSUI], "desk_work", {"advance": True})["structuredContent"]
    assert advanced["catch_up"]["advanced"] is True
    mark = client.get("/v1/marks/sweep", headers=keys[SEAT]).json()
    assert mark["value"] == str(advanced["catch_up"]["until"])


def test_desk_get_by_subject_key_and_ref(
    client: TestClient, urls: dict[str, str], conn: sqlite3.Connection
) -> None:
    store.insert_item(conn, {"id": "a1", "task": "t", "subject_key": "RT#7", "ref": "r/x#1"}, JOSUI)
    store.insert_item(conn, {"id": "a2", "task": "t", "ref": "r/x#1"}, JOSUI)
    by_key = call(client, urls[SEAT], "desk_get", {"subject_key": "RT#7"})
    assert by_key["structuredContent"]["id"] == "a1"
    by_ref = call(client, urls[SEAT], "desk_get", {"ref": "r/x#1"})
    assert by_ref["structuredContent"]["id"] in ("a1", "a2")
    assert call(client, urls[SEAT], "desk_get", {"item_id": "a2"})["structuredContent"]["id"] == "a2"
    assert mcp_status(call(client, urls[SEAT], "desk_get", {"subject_key": "RT#8"})) == NOT_FOUND
    assert mcp_status(call(client, urls[SEAT], "desk_get", {"ref": "nope"})) == NOT_FOUND
    assert mcp_status(call(client, urls[SEAT], "desk_get")) == UNPROCESSABLE_ENTITY
    both = {"item_id": "a1", "ref": "r/x#1"}
    assert mcp_status(call(client, urls[SEAT], "desk_get", both)) == UNPROCESSABLE_ENTITY


def test_desk_assign_with_a_task_creates_then_assigns(
    client: TestClient, urls: dict[str, str], conn: sqlite3.Connection
) -> None:
    with store.transaction(conn):
        store.write_seat(conn, SEAT, {"lane": "exedore-max", "status": "free", "item": None})
    arguments = {"record": {"task": "do it", "done_when": "merged", "subject_key": "RT#9"}, "seat": SEAT}
    assert mcp_status(call(client, urls[SEAT], "desk_assign", arguments)) == FORBIDDEN
    item = call(client, urls[JOSUI], "desk_assign", arguments)["structuredContent"]
    assert (item["task"], item["done_when"], item["subject_key"]) == ("do it", "merged", "RT#9")
    assert item["seat"] == SEAT and store.get_item(conn, item["id"]) is not None
    assert mcp_status(call(client, urls[JOSUI], "desk_assign", {"seat": SEAT})) == UNPROCESSABLE_ENTITY


def test_desk_wait_holds_a_commit_to_the_branch_it_names(
    client: TestClient,
    keys: Keys,
    urls: dict[str, str],
    conn: sqlite3.Connection,
    required_checks: RequiredChecks,
) -> None:
    sha = "b" * 40
    check = {"repo": "crunchtools/ashigaru", "sha": sha, "key": "workflow:1:push", "name": "CI (push)"}
    with store.transaction(conn):
        store.write_check(
            conn,
            {**check, "verdict": "success", "run": "1", "url": None, "source": "github", "updated_at": "t"},
        )
    required_checks.rules["release"] = ("sdlc ci",)
    query: dict[str, str | int] = {"repo": "crunchtools/ashigaru", "sha": sha, "timeout": 0}
    assert call(client, urls[SEAT], "desk_wait", query)["structuredContent"]["result"]["status"] == "success"
    held = call(client, urls[SEAT], "desk_wait", {**query, "branch": "release"})["structuredContent"][
        "result"
    ]
    assert held["status"] == "pending" and held["pending"] == ["sdlc ci"] and held["branch"] == "release"
    required_checks.reported["sdlc ci"] = Verdict.FAILURE
    failed = call(client, urls[SEAT], "desk_wait", {**query, "branch": "release"})["structuredContent"][
        "result"
    ]
    assert failed["status"] == "failure" and failed["failing"] == ["sdlc ci"]
    assert (
        mcp_status(call(client, urls[SEAT], "desk_wait", {**query, "branch": "a b"})) == UNPROCESSABLE_ENTITY
    )


def test_desk_wait_on_an_item_reads_it(
    client: TestClient, urls: dict[str, str], conn: sqlite3.Connection
) -> None:
    store.insert_item(conn, {"id": "a1", "task": "t", "seat": SEAT, "state": "working"}, JOSUI)
    working = call(client, urls[SEAT], "desk_wait", {"item_id": "a1", "timeout": 0})
    assert not working["isError"] and working["structuredContent"]["result"]["state"] == "working"
    store.insert_item(conn, {"id": "a2", "task": "t", "seat": SEAT, "state": "review"}, JOSUI)
    settled = call(client, urls[SEAT], "desk_wait", {"item_id": "a2"})
    assert settled["structuredContent"]["result"]["state"] == "review"
    assert mcp_status(call(client, urls[SEAT], "desk_wait", {"item_id": "zz", "timeout": 0})) == NOT_FOUND
    assert mcp_status(call(client, urls[SEAT], "desk_wait", {"timeout": 0})) == UNPROCESSABLE_ENTITY
    mixed = {"item_id": "a1", "repo": "o/r", "sha": "a" * 40}
    assert mcp_status(call(client, urls[SEAT], "desk_wait", mixed)) == UNPROCESSABLE_ENTITY


@pytest.mark.parametrize("half", [{"repo": "o/r"}, {"sha": "a" * 40}, {"item_id": "a1", "repo": "o/r"}])
def test_desk_wait_refuses_half_a_commit(
    client: TestClient, urls: dict[str, str], half: dict[str, str]
) -> None:
    assert mcp_status(call(client, urls[SEAT], "desk_wait", {**half, "timeout": 0})) == UNPROCESSABLE_ENTITY
