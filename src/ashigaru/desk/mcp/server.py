"""The hosted MCP server: the shared command set as tools, served inside the Desk at /mcp/<code>.

FastMCP over Streamable HTTP, as Acquacotta serves its hosted MCP, but inside the
Desk's own app rather than beside it. The code in the path is checked before MCP
sees the request, and names the caller; nothing in a tool's arguments does.

Every tool calls the REST route's own function with that caller, so authority is
the API's, line for line, and the MCP is never a side door: a 403 there is a 403
here. Stateless, so a revoked code fails on its very next request.
"""

import asyncio
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Lifespan, Receive, Scope, Send

from .. import api, auth, store
from ..model import (
    MARK_NAME,
    MAX_WAIT,
    Assign,
    Board,
    Changes,
    CIResult,
    CIWait,
    FileChild,
    Item,
    ItemCreate,
    ItemFilter,
    ItemPatch,
    Lane,
    Mark,
    MarkSet,
    Mode,
    NewItem,
    OutcomeRow,
    Queue,
    Report,
    Round,
    Seat,
    Sweep,
    Usage,
    WrapUp,
)
from . import codes

CALLER = "desk_caller"
SECRET_PATH = re.compile(r"/mcp/[A-Za-z0-9_-]+")
REDACTED = "/mcp/<code>"
SETTLED = {"review", "done", "blocked", "wontdo", "abandoned"}
POLL = 2.0
DB = "desk_db"
WAITERS = "desk_waiters"
SWEEPER = "desk_sweeper"
ItemId = Annotated[str, Field(description="An item id, e.g. a204.")]
IfVersion = Annotated[
    int | None, Field(description="The item's version the write was decided on; refused (412) once it moved.")
]

mcp = FastMCP(
    "ashigaru-desk",
    instructions=(
        "The Ashigaru Desk: one queue of work items and the seats that do them. The tools are "
        "`desk_<command>` for each `ashigaru` command (mcp and hook are CLI-only). This URL is bound to "
        "one identity; a seat writes only its own open items."
    ),
)


@contextmanager
def desk() -> Iterator[tuple[sqlite3.Connection, auth.Caller]]:
    """The caller the code named and a connection, with the API's refusals as tool errors."""
    state = get_http_request().scope["state"]
    conn = store.connect(state[DB])
    try:
        yield conn, state[CALLER]
    except HTTPException as error:
        raise ToolError(f"{error.status_code}: {error.detail}") from error
    except ValidationError as error:
        raise ToolError(f"422: {error}") from error
    finally:
        conn.close()


# Views


@mcp.tool
def desk_usage() -> Usage:
    """`ashigaru usage`: per seat context, tokens, turn time and model; spend by billing source."""
    with desk() as (conn, who):
        return api.usage(conn, who)


@mcp.tool
def desk_agents() -> list[Seat]:
    """`ashigaru agents`: every seat, free or busy, the item it holds, and since when."""
    with desk() as (conn, who):
        return api.list_seats(conn, who)


@mcp.tool
def desk_status() -> Board:
    """`ashigaru status`: the board a person reads: busy seats with their work in full, free and
    missing seats, the tally, and NEEDS YOU (manual seats idle on a question, Scott's blocked work,
    subjects held for approval)."""
    with desk() as (conn, who):
        return api.board(conn, who)


@mcp.tool
def desk_work(
    all: Annotated[bool, Field(description="Include closed and cleared items.")] = False,
    since: Annotated[
        str | None, Field(description="Catch up from this time instead of the sweep mark.")
    ] = None,
    advance: Annotated[bool, Field(description="Admin: move the sweep mark to this round's end.")] = False,
) -> Round:
    """`ashigaru work`: the whole check round: the check table (open items unless `all`), the seats,
    the queue, and the forge catch-up since the sweep mark. With `advance`, admin only, the sweep
    mark then moves forward to the catch-up's `until`; `since` cannot go with it."""
    with desk() as (conn, who):
        if advance:
            return api.advance(conn, api.admin(who), all, since)
        return api.work(conn, who, all, since)


@mcp.tool
def desk_changes(
    since: Annotated[str | None, Field(description="Look from this time instead of your seen mark.")] = None,
    ack: Annotated[bool, Field(description="Then move your seen mark to this look's end.")] = False,
) -> Changes:
    """`ashigaru changes`: each item someone else changed since you last acked (else the last day),
    with the fields that moved, who moved them and its NEXT now. With `ack`, your seen mark then
    moves to the look's `until`; `since` cannot go with it."""
    with desk() as (conn, who):
        if ack:
            if since is not None:
                raise ToolError("422: since cannot go with ack")
            return api.ack(conn, who)
        return api.changed(conn, who, since)


@mcp.tool
def desk_queue() -> Queue:
    """`ashigaru queue`: queued items with the state of each item they wait on (`waits_on`), then
    proposed children with their expiry."""
    with desk() as (conn, who):
        return api.queue(conn, who)


@mcp.tool
async def desk_sweep(
    run: Annotated[bool, Field(description="Admin: start a run now; it returns at once.")] = False,
) -> Sweep:
    """`ashigaru sweep`: whether the timer that reads GitHub back runs, how often, whether a run is
    under way, and the last run's counts and error; with `run` (admin), start one now."""
    sweeper = get_http_request().scope["state"][SWEEPER]
    with desk() as (conn, who):
        if run:
            api.admin(who)
            return api.sweep_start(sweeper, conn)
        return api.sweep_view(sweeper, conn)


@mcp.tool
def desk_outcomes() -> list[OutcomeRow]:
    """`ashigaru outcomes`: each outcome with its stories' total, done, blocked and open counts."""
    with desk() as (conn, who):
        return api.outcomes(conn, who)


@mcp.tool
def desk_list(filters: ItemFilter | None = None) -> list[Item]:
    """`ashigaru list`: items, newest first; each filter given must match, `state` any of a list.
    Find an item by `subject_key` or `ref` here. Pages of `limit` from `offset`."""
    with desk() as (conn, who):
        return api.list_items(conn, who, filters or ItemFilter.model_validate({}))


@mcp.tool
def desk_get(
    item_id: Annotated[str | None, Field(description="An item id, e.g. a204.")] = None,
    subject_key: Annotated[str | None, Field(description="The item's unique key, e.g. RT#7.")] = None,
    ref: Annotated[str | None, Field(description="The most recently updated item with this ref.")] = None,
) -> Item:
    """`ashigaru get`: one item, every field, by exactly one of `item_id`, `subject_key` or `ref`."""
    if sum(x is not None for x in (item_id, subject_key, ref)) != 1:
        raise ToolError("422: name one of: item_id, subject_key, ref")
    with desk() as (conn, who):
        if item_id is not None:
            return api.get_item(conn, who, item_id)
        found = api.list_items(
            conn, who, ItemFilter.model_validate({"subject_key": subject_key, "ref": ref, "limit": 1})
        )
        if not found:
            raise ToolError(
                f"404: no item with {'subject_key' if subject_key else 'ref'} {subject_key or ref}"
            )
        return found[0]


# Actions


@mcp.tool
def desk_assign(
    item_id: Annotated[
        str | None, Field(description="An item id; with `record`, the id to record it under.")
    ] = None,
    seat: str | None = None,
    lane: Lane | None = None,
    record: Annotated[
        NewItem | None, Field(description="Record this new item first, then assign it.")
    ] = None,
    if_version: IfVersion = None,
) -> Item:
    """`ashigaru assign`, admin only: give the item to `seat`, or to the seat in `lane` (default: the
    item's) that has been free longest and is not held by Scott. With `record` (`assign --task`) the
    item is recorded and assigned in one transaction, in `lane`: a refused assign records nothing."""
    if seat and lane:
        raise ToolError("422: name a seat or a lane, not both")
    with desk() as (conn, who):
        admin = api.admin(who)
        target = Assign(seat=seat, lane=lane)
        if record is not None:
            if if_version is not None:
                raise ToolError("422: if_version is for an existing item, not a new one")
            fields = record.model_dump(exclude_none=True) | {"id": item_id, "lane": lane}
            new = {key: value for key, value in fields.items() if value is not None}
            return api.create_item(conn, admin, ItemCreate.model_validate(new | {"assign": target}))
        if item_id is None:
            raise ToolError("422: name an item_id, or give a record to create one")
        return api.assign(conn, admin, item_id, target, api.Guard(version=if_version))


@mcp.tool
def desk_auto(item_id: ItemId) -> Item:
    """`ashigaru auto`: hand the item to Josui."""
    with desk() as (conn, who):
        return api.patch_item(conn, who, item_id, ItemPatch(mode=Mode.AUTO))


@mcp.tool
def desk_manual(item_id: ItemId) -> Item:
    """`ashigaru manual`: hand the item to Scott."""
    with desk() as (conn, who):
        return api.patch_item(conn, who, item_id, ItemPatch(mode=Mode.MANUAL))


@mcp.tool
def desk_wrap(
    item_id: ItemId,
    fields: Annotated[
        WrapUp | None,
        Field(
            description="stage (wrapping once /cc seat is sent; cleared, the default, closes), learned, "
            "state, force: as `ashigaru wrap`."
        ),
    ] = None,
    if_version: IfVersion = None,
) -> Item:
    """`ashigaru wrap`, admin only. `wrapping` records that /cc seat was sent. `cleared` refuses until
    CC-DONE (or a `learned`) and an idle turn, unless `force`; then clears the wrap and closes the
    item in `state`, which frees the seat."""
    with desk() as (conn, who):
        return api.wrap(
            conn, api.admin(who), item_id, fields or WrapUp.model_validate({}), api.Guard(version=if_version)
        )


@mcp.tool
def desk_create(
    fields: Annotated[
        dict[str, Any],
        Field(
            description="The item: `task` and any of id, kind, parent, subject_key, done_when, ref, repo, "
            "lane, mode, state (default queued), after, priority, note, filed_by. A `seat` given here is "
            "recorded without assign's checks; use assign to give an item to a seat."
        ),
    ],
) -> Item:
    """`ashigaru create`, admin only: record an item without assigning it. A taken id or subject_key is
    a 409 that names the item holding it."""
    with desk() as (conn, who):
        return api.create_item(conn, api.admin(who), ItemCreate.model_validate(fields))


@mcp.tool
def desk_mark(
    name: Annotated[str, Field(pattern=MARK_NAME, description="The watermark, e.g. sweep.")],
    value: Annotated[str | None, Field(description="Set it to this (admin); omit to read it.")] = None,
) -> Mark:
    """`ashigaru mark`: read a named watermark, such as how far the last sweep reached, or set it."""
    with desk() as (conn, who):
        if value is None:
            return api.get_mark(conn, who, name)
        return api.set_mark(conn, api.admin(who), name, MarkSet(value=value))


@mcp.tool
def desk_claim(item_id: ItemId, if_version: IfVersion = None) -> Item:
    """`ashigaru claim`: a seat takes a queued, unowned, auto item in its lane whose `after` are done."""
    with desk() as (conn, who):
        return api.claim(conn, who, item_id, api.Guard(version=if_version))


@mcp.tool
def desk_update(
    item_id: ItemId,
    fields: Annotated[
        dict[str, Any],
        Field(
            description="Item fields to set; null clears. A seat may set only state (working, review, "
            "blocked), result, next, learned, note, blocked_reason, memorized and check_after."
        ),
    ],
    if_version: IfVersion = None,
) -> Item:
    """`ashigaru update`: change an item's fields, as PATCH /v1/items/{id}."""
    with desk() as (conn, who):
        return api.patch_item(
            conn, who, item_id, ItemPatch.model_validate(fields), api.Guard(version=if_version)
        )


@mcp.tool
def desk_report(
    item_id: ItemId,
    fields: Annotated[
        Report,
        Field(
            description="verb (DONE, BLOCKED, WORKING, CC-DONE), text (the result, the blocker or the "
            "progress note), next, learned ('none' when nothing was): as `ashigaru report`."
        ),
    ],
    if_version: IfVersion = None,
) -> Item:
    """`ashigaru report`, the report contract: DONE moves to review, BLOCKED to blocked, WORKING to
    working; CC-DONE marks the learnings memorized."""
    with desk() as (conn, who):
        return api.report(conn, who, item_id, fields, api.Guard(version=if_version))


@mcp.tool
def desk_file(
    item_id: ItemId,
    task: str,
    done_when: str | None = None,
    note: str | None = None,
    if_version: IfVersion = None,
) -> Item:
    """`ashigaru file`: file one child of this story as proposed, under its outcome; one per assignment."""
    with desk() as (conn, who):
        body = FileChild(task=task, done_when=done_when, note=note)
        return api.file(conn, who, item_id, body, api.Guard(version=if_version))


@mcp.tool
async def desk_wait(
    item_id: Annotated[str | None, Field(description="Wait on this item to settle.")] = None,
    repo: Annotated[str | None, Field(description="owner/name, e.g. crunchtools/ashigaru.")] = None,
    sha: Annotated[str | None, Field(description="The full commit id.")] = None,
    timeout: Annotated[int, Field(description="Seconds to wait, at most 90; 0 reads now.")] = MAX_WAIT,
    branch: Annotated[
        str | None, Field(description="The branch the commit targets; its required checks must pass.")
    ] = None,
) -> Item | CIResult:
    """`ashigaru wait`: with `item_id`, the item as soon as it is review, done, blocked, wontdo or
    abandoned; with `repo` and `sha`, the commit's CI result as soon as a webhook delivery settles it
    and every check `branch` (default the repo's) requires has a conclusion. Either way, as it stands
    after `timeout` (still working or `pending`: call again)."""
    commit = repo is not None and sha is not None
    if (repo is None) != (sha is None) or (item_id is not None) == commit:
        raise ToolError("422: name an item_id, or a repo and a sha")
    with desk() as (conn, who):
        if item_id is not None:
            return await wait_item(conn, item_id, min(max(timeout, 0), MAX_WAIT))
        query = CIWait(repo=repo or "", sha=sha or "", timeout=timeout, branch=branch)
        waiters = get_http_request().scope["state"][WAITERS]
        return await waiters.wait(conn, query.repo, query.sha, query.timeout, query.branch)


async def wait_item(conn: sqlite3.Connection, item_id: str, timeout: int) -> Item:
    """The item once it has settled, or as it stands after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        row = await run_in_threadpool(store.get_item, conn, item_id)
        if row is None:
            raise ToolError("404: no such item")
        item = Item.model_validate(row)
        left = deadline - time.monotonic()
        if item.state in SETTLED or left <= 0:
            return item
        await asyncio.sleep(min(left, POLL))


class Endpoint:
    """/mcp/{code}, with or without anything after it: the code names the caller, then the request
    goes to MCP as if at its root, with the code gone from the scope. An unknown or revoked code,
    or a path under it, is a 404 that says no more than an unknown path would. The route takes
    every path under the code so the router never answers one with a redirect that echoes it."""

    def __init__(self, db_path: str | Path, app: ASGIApp) -> None:
        self.db_path = str(db_path)
        self.app = app

    def authenticate(self, code: str) -> auth.Caller | None:
        conn = store.connect(self.db_path)
        try:
            return codes.authenticate(conn, code)
        finally:
            conn.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        params = scope["path_params"]
        who = None
        if params["rest"] in ("", "/"):
            who = await run_in_threadpool(self.authenticate, params["code"])
        if who is None:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        # The MCP app becomes scope["app"] below, so the Desk's waiters and sweeper travel in the state.
        desk_state = scope["app"].state
        state = {
            **scope.get("state", {}), CALLER: who, DB: self.db_path,
            WAITERS: desk_state.waiters, SWEEPER: desk_state.sweeper,
        }  # fmt: skip
        inner = {**scope, "path": "/", "raw_path": b"/", "root_path": "", "path_params": {}, "state": state}
        await self.app(inner, receive, send)


class RedactCodes(logging.Filter):
    """The code is the credential: any log argument that would print /mcp/<secret> prints
    /mcp/<code> instead, whether it is the access log's path or the ASGI scope at TRACE."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(arg) for arg in record.args)
        if isinstance(record.msg, str):
            record.msg = SECRET_PATH.sub(REDACTED, record.msg)
        return True


def redact(arg: object) -> object:
    """The argument, or its text with codes replaced if it would print one; numbers stay numbers."""
    text = arg if isinstance(arg, str) else str(arg)
    return SECRET_PATH.sub(REDACTED, text) if SECRET_PATH.search(text) else arg


SERVER_LOGS = tuple(logging.getLogger(name) for name in ("uvicorn.access", "uvicorn.asgi", "uvicorn.error"))
"""Every logger uvicorn writes a request's path or scope to."""


def install(db_path: str | Path) -> tuple[Lifespan[Any], Route]:
    """A fresh MCP app for one Desk app: the lifespan the Desk must run, which starts the session
    manager (once per app) and redacts codes from uvicorn's logs, and the /mcp/{code} route."""
    hosted = mcp.http_app(path="/", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        for log in SERVER_LOGS:  # here, after uvicorn has configured its loggers
            if not any(isinstance(existing, RedactCodes) for existing in log.filters):
                log.addFilter(RedactCodes())
        async with hosted.lifespan(app):
            yield

    route = Route("/mcp/{code}{rest:path}", Endpoint(db_path, hosted), include_in_schema=False)
    return lifespan, route
