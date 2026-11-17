"""The hosted MCP server: the shared command set as tools, served inside the Desk at /mcp/<code>.

FastMCP over Streamable HTTP, as Acquacotta serves its hosted MCP, but inside the
Desk's own app rather than beside it. The code in the path is checked before MCP
sees the request, and names the caller; nothing in a tool's arguments does.

Every tool calls the REST route's own function with that caller, so authority is
the API's, line for line, and the MCP is never a side door: a 403 there is a 403
here. Stateless, so a revoked code fails on its very next request.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from pydantic import Field, ValidationError
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Lifespan, Receive, Scope, Send

from .. import api, auth, store
from ..model import (
    Assign,
    CheckRow,
    FileChild,
    Item,
    ItemFilter,
    ItemPatch,
    Lane,
    Mode,
    OutcomeRow,
    Report,
    Seat,
    State,
    Usage,
    Verb,
    Wrap,
)
from . import codes

CALLER = "desk_caller"
DB = "desk_db"
ItemId = Annotated[str, Field(description="An item id, e.g. a204.")]

mcp = FastMCP(
    "ashigaru-desk",
    instructions=(
        "The Ashigaru Desk: one queue of work items and the seats that do them. The tools mirror the "
        "`ashigaru` CLI. This URL is bound to one identity; a seat writes only its own open items."
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
def usage() -> Usage:
    """`ashigaru usage`: per seat context, tokens, turn time and model; spend by billing source."""
    with desk() as (conn, who):
        return api.usage(conn, who)


@mcp.tool
def agents() -> list[Seat]:
    """`ashigaru agents`: every seat, free or busy, the item it holds, and since when."""
    with desk() as (conn, who):
        return api.list_seats(conn, who)


@mcp.tool
def work(
    show_all: Annotated[bool, Field(description="Include cleared, wontdo and abandoned.")] = False,
) -> list[CheckRow]:
    """`ashigaru work`: the /agents check table, items blocked first, then newest."""
    with desk() as (conn, who):
        return api.check(conn, who, show_all)


@mcp.tool
def queue() -> list[Item]:
    """`ashigaru queue`: queued items with what each waits on (`after`), then proposed children
    with their expiry."""
    with desk() as (conn, who):
        return [
            *api.list_items(conn, who, ItemFilter.model_validate({"state": State.QUEUED})),
            *api.list_items(conn, who, ItemFilter.model_validate({"state": State.PROPOSED})),
        ]


@mcp.tool
def outcomes() -> list[OutcomeRow]:
    """`ashigaru outcomes`: each outcome with its stories' total, done, blocked and open counts."""
    with desk() as (conn, who):
        return api.outcomes(conn, who)


@mcp.tool
def desk_list(filters: ItemFilter | None = None) -> list[Item]:
    """Items, newest first; each filter given must match. Pages of `limit` from `offset`."""
    with desk() as (conn, who):
        return api.list_items(conn, who, filters or ItemFilter.model_validate({}))


@mcp.tool
def desk_get(item_id: ItemId) -> Item:
    """One item, every field."""
    with desk() as (conn, who):
        return api.get_item(conn, who, item_id)


# Actions


@mcp.tool
def assign(item_id: ItemId, seat: str | None = None, lane: Lane | None = None) -> Item:
    """`ashigaru assign`, admin only: give the item to `seat`, or to the seat in `lane` (default: the
    item's) that has been free longest and is not held by Scott."""
    with desk() as (conn, who):
        return api.assign(conn, api.admin(who), item_id, Assign(seat=seat, lane=lane))


@mcp.tool
def auto(item_id: ItemId) -> Item:
    """`ashigaru auto`: hand the item to Josui."""
    with desk() as (conn, who):
        return api.patch_item(conn, who, item_id, ItemPatch(mode=Mode.AUTO))


@mcp.tool
def manual(item_id: ItemId) -> Item:
    """`ashigaru manual`: hand the item to Scott."""
    with desk() as (conn, who):
        return api.patch_item(conn, who, item_id, ItemPatch(mode=Mode.MANUAL))


@mcp.tool
def wrap(
    item_id: ItemId,
    stage: Annotated[Wrap, Field(description="wrapping once /cc seat is sent, cleared once the seat is.")],
) -> Item:
    """`ashigaru wrap`: record the item's wrap-up stage."""
    with desk() as (conn, who):
        return api.patch_item(conn, who, item_id, ItemPatch(wrap=stage))


@mcp.tool
def desk_claim(item_id: ItemId) -> Item:
    """A seat takes a queued, unowned, auto item in its lane whose `after` items are done."""
    with desk() as (conn, who):
        return api.claim(conn, who, item_id)


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
) -> Item:
    """Change an item's fields, as PATCH /v1/items/{id}."""
    with desk() as (conn, who):
        return api.patch_item(conn, who, item_id, ItemPatch.model_validate(fields))


@mcp.tool
def desk_report(
    item_id: ItemId,
    verb: Verb,
    text: Annotated[str | None, Field(description="The result, the blocker, or the progress note.")] = None,
    next: str | None = None,
    learned: Annotated[str | None, Field(description="The Learned: line; 'none' when nothing was.")] = None,
) -> Item:
    """The report contract: DONE moves to review, BLOCKED to blocked, WORKING to working; CC-DONE marks
    the learnings memorized."""
    with desk() as (conn, who):
        return api.report(conn, who, item_id, Report(verb=verb, text=text, next=next, learned=learned))


@mcp.tool
def desk_file(item_id: ItemId, task: str, done_when: str | None = None, note: str | None = None) -> Item:
    """File one child of this story as proposed, under its outcome; one per assignment."""
    with desk() as (conn, who):
        return api.file(conn, who, item_id, FileChild(task=task, done_when=done_when, note=note))


class Endpoint:
    """/mcp/{code}: the code names the caller, then the request goes to MCP as if at its root.
    An unknown or revoked code is a 404 that says no more than an unknown path would."""

    def __init__(self, db_path: str | Path, app: ASGIApp) -> None:
        self.db_path = str(db_path)
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        conn = store.connect(self.db_path)
        try:
            who = codes.authenticate(conn, scope["path_params"]["code"])
        finally:
            conn.close()
        if who is None:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        state = {**scope.get("state", {}), CALLER: who, DB: self.db_path}
        await self.app(
            {**scope, "path": "/", "raw_path": b"/", "root_path": "", "state": state}, receive, send
        )


def install(db_path: str | Path) -> tuple[Lifespan[Any], Route]:
    """A fresh MCP app for one Desk app: the lifespan the Desk must run (it starts the session
    manager, which runs once per app) and the /mcp/{code} route in front of it."""
    hosted = mcp.http_app(path="/", stateless_http=True, json_response=True)
    return hosted.lifespan, Route("/mcp/{code}", Endpoint(db_path, hosted), include_in_schema=False)
