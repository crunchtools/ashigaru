"""The Desk's REST API. Everything else (CLI, MCP, hooks, Josui) is a client of this.

Authority: an admin (Josui, Scott) writes anything. A seat reads everything and
writes only items whose `seat` is its own name, only SEAT_FIELDS, and only to
SEAT_STATES; Josui verifies and closes. A ci identity reads.
"""

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import auth, dispatch, store
from .model import (
    CLOSED,
    MARK_NAME,
    SEAT_FIELDS,
    SEAT_ROW_FIELDS,
    SEAT_STATES,
    ApiKey,
    Assign,
    Billing,
    BillingLimits,
    Board,
    CheckRow,
    Event,
    FileChild,
    Identity,
    IdentityCreate,
    Item,
    ItemCreate,
    ItemFilter,
    ItemPatch,
    KeyCreate,
    Mark,
    MarkSet,
    MintedKey,
    OutcomeRow,
    Problem,
    Queue,
    Report,
    Role,
    Round,
    Seat,
    SeatPatch,
    State,
    Usage,
    UsageReport,
    Verb,
    Wrap,
    WrapUp,
)

VERSION = "3.0.0-dev"
bearer = HTTPBearer(auto_error=False, description="An API key: `Authorization: Bearer ash_<id>_<secret>`.")


def connection(request: Request) -> Iterator[sqlite3.Connection]:
    conn = store.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(connection)]


def caller(
    conn: Conn, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> auth.Caller:
    found = auth.authenticate(conn, credentials.credentials) if credentials else None
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing, unknown or revoked API key")
    return found


Who = Annotated[auth.Caller, Depends(caller)]


def admin(who: Who) -> auth.Caller:
    if not who.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return who


Admin = Annotated[auth.Caller, Depends(admin)]


def _found(value: dict[str, Any] | None, what: str) -> dict[str, Any]:
    if value is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such {what}")
    return value


def _conflict(error: store.Conflict) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(error))


def writable_item(conn: sqlite3.Connection, item_id: str, who: auth.Caller) -> dict[str, Any]:
    """The item, if `who` may write it: admins any, a seat only its own and only while it is open.
    Once Josui closes an item (done, wontdo, abandoned) or clears it, a seat cannot reopen it."""
    item = _found(store.get_item(conn, item_id), "item")
    if who.admin:
        return item
    if who.role != Role.SEAT or item["seat"] != who.name:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{item_id} belongs to {item['seat'] or 'no seat'}")
    if item["state"] in CLOSED or item["wrap"] == Wrap.CLEARED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{item_id} is closed; only an admin may change it")
    return item


def seat_may(who: auth.Caller, fields: dict[str, Any]) -> None:
    if who.admin:
        return
    refused = sorted(set(fields) - SEAT_FIELDS)
    if refused:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"a seat may not set: {', '.join(refused)}")
    if "state" in fields and fields["state"] not in SEAT_STATES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"a seat may not move an item to {fields['state']}")


def report_fields(report: Report) -> dict[str, Any]:
    """Map a DONE / BLOCKED / WORKING / CC-DONE report onto item fields."""
    by_verb: dict[Verb, dict[str, Any]] = {
        Verb.DONE: {"state": State.REVIEW, "result": report.text},
        Verb.BLOCKED: {"state": State.BLOCKED, "blocked_reason": report.text},
        Verb.WORKING: {"state": State.WORKING, "note": report.text},
        Verb.CC_DONE: {"memorized": True},
    }
    fields = by_verb[report.verb]
    if report.next is not None:
        fields["next"] = report.next
    if report.learned and report.learned.strip().lower() != "none":
        fields["learned"] = report.learned
    return {key: value for key, value in fields.items() if value is not None}


def _patch_item(conn: sqlite3.Connection, item_id: str, fields: dict[str, Any], who: auth.Caller) -> Item:
    with store.transaction(conn):
        writable_item(conn, item_id, who)
        seat_may(who, fields)
        try:
            store.update_item(conn, item_id, fields, who.name)
        except store.Conflict as error:
            raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, item_id))


ERRORS = {
    401: "Missing, unknown or revoked API key.",
    403: "The caller's role, or its seat, may not do this.",
    404: "No such item, seat, identity or key.",
    409: (
        "A taken id or subject_key (the detail names the item holding it), an existing identity, a"
        " second child in one assignment, or an item or seat not in a state to assign, claim or wrap."
    ),
    422: "A query or body the Desk cannot read, such as a time that is not one.",
}


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """The error responses a route can return, for the published contract."""
    return {code: {"model": Problem, "description": ERRORS[code]} for code in codes}


router = APIRouter()


@router.get("/healthz", tags=["meta"])
def healthz(conn: Conn) -> dict[str, Any]:
    """Unauthenticated. 200 when the database answers."""
    return {"ok": True, "schema": store.schema_version(conn)}


@router.get("/v1/me", tags=["identities"], responses=errors(401))
def me(who: Who) -> dict[str, str]:
    return {"name": who.name, "role": who.role, "key_id": who.key_id}


@router.get("/v1/items", tags=["items"], responses=errors(401))
def list_items(conn: Conn, who: Who, filters: Annotated[ItemFilter, Query()]) -> list[Item]:
    store.expire_proposed(conn)
    return [Item.model_validate(row) for row in store.list_items(conn, **filters.model_dump())]


@router.post(
    "/v1/items", tags=["items"], status_code=status.HTTP_201_CREATED, responses=errors(401, 403, 409)
)
def create_item(conn: Conn, who: Admin, body: ItemCreate) -> Item:
    with store.transaction(conn):
        try:
            item_id = store.insert_item(conn, body.model_dump(exclude_unset=True), who.name)
        except store.Conflict as error:
            raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, item_id))


@router.get("/v1/items/{item_id}", tags=["items"], responses=errors(401, 404))
def get_item(conn: Conn, who: Who, item_id: str) -> Item:
    return Item.model_validate(_found(store.get_item(conn, item_id), "item"))


@router.patch("/v1/items/{item_id}", tags=["items"], responses=errors(401, 403, 404, 409))
def patch_item(conn: Conn, who: Who, item_id: str, body: ItemPatch) -> Item:
    return _patch_item(conn, item_id, body.model_dump(exclude_unset=True), who)


@router.post("/v1/items/{item_id}/report", tags=["items"], responses=errors(401, 403, 404, 409))
def report(conn: Conn, who: Who, item_id: str, body: Report) -> Item:
    """The report contract. DONE moves to review (Josui verifies), BLOCKED to blocked, WORKING to
    working; CC-DONE marks the learnings memorized. Learned: none is not recorded."""
    return _patch_item(conn, item_id, report_fields(body), who)


@router.post(
    "/v1/items/{item_id}/file",
    tags=["items"],
    status_code=status.HTTP_201_CREATED,
    responses=errors(401, 403, 404, 409),
)
def file(conn: Conn, who: Who, item_id: str, body: FileChild) -> Item:
    """desk_file: record a child of this story as proposed, under the story's outcome. One per
    assignment (409 on the second); abandoned after 14 days unless Josui promotes it."""
    with store.transaction(conn):
        story = writable_item(conn, item_id, who)
        try:
            child = store.file_child(conn, story, body.model_dump(exclude_unset=True), who.name)
        except store.Conflict as error:
            raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, child))


@router.get("/v1/items/{item_id}/events", tags=["items"], responses=errors(401, 404))
def item_events(
    conn: Conn,
    who: Who,
    item_id: str,
    after: Annotated[int, Query(ge=0, description="Return events with seq above this.")] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[Event]:
    """The item's history, oldest first, a page at a time: pass the last seq as `after` for the next."""
    _found(store.get_item(conn, item_id), "item")
    return [Event.model_validate(row) for row in store.events(conn, item_id, after, limit)]


@router.get("/v1/check", tags=["views"], responses=errors(401))
def check(conn: Conn, who: Who, show_all: Annotated[bool, Query(alias="all")] = False) -> list[CheckRow]:
    """The check table alone, as `items` in /v1/work: open items, blocked first, then newest. `all`
    includes closed and cleared items."""
    store.expire_proposed(conn)
    return [CheckRow.model_validate(row) for row in store.check(conn, show_all)]


Since = Annotated[str | None, Query(description="Start the catch-up at this time instead of the sweep mark.")]


def _round(conn: sqlite3.Connection, show_all: bool, since: str | None) -> Round:
    try:
        return Round.model_validate(dispatch.work(conn, show_all, since))
    except ValueError as error:
        what = "since" if since is not None else "the sweep mark"
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{what} is not a time: {error}") from error


@router.get("/v1/work", tags=["views"], responses=errors(401, 422))
def work(
    conn: Conn, who: Who, show_all: Annotated[bool, Query(alias="all")] = False, since: Since = None
) -> Round:
    """`ashigaru work`, the whole check round in one call: the check table (open items unless `all`),
    the seats with how many are up and how many items are open, the queue, and the forge catch-up
    from the org webhook's intake after the sweep mark. It leaves the mark alone; like every view of
    the queue, it abandons proposed children past their expiry."""
    return _round(conn, show_all, since)


@router.post("/v1/work", tags=["views"], responses=errors(401, 403, 422))
def advance(
    conn: Conn, who: Admin, show_all: Annotated[bool, Query(alias="all")] = False, since: Since = None
) -> Round:
    """`ashigaru work --advance`, admin: the same round, then the sweep mark moves to its catch-up's
    `until`, the last event it covered, so the next round starts after it and nothing is lost or
    repeated, even when this one was truncated. Only forward: when it is already there, or a concurrent
    round moved it further, it stays and `advanced` is false. `since` is 422: a round read from a time could
    move the mark past events it never read."""
    if since is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "since cannot be used with advance")
    found = _round(conn, show_all, since)
    moved = store.advance_mark(conn, dispatch.SWEEP, found.catch_up.until, who.name)
    return found.model_copy(update={"catch_up": found.catch_up.model_copy(update={"advanced": moved})})


@router.get("/v1/status", tags=["views"], responses=errors(401))
def board(conn: Conn, who: Who) -> Board:
    """`ashigaru status`, the board a person reads: each busy seat with its work, how long it has held
    it, its last check-in and its whole task; the free and missing seats by name; how many seats are
    up and items open; and NEEDS YOU: each manual seat idle on what it last said, Scott's blocked work,
    and subjects held for his approval."""
    return Board.model_validate(dispatch.status(conn))


@router.get("/v1/queue", tags=["views"], responses=errors(401))
def queue(conn: Conn, who: Who) -> Queue:
    """`ashigaru queue`: queued items with the state of each item they wait on, then proposed children
    with their expiry."""
    return Queue.model_validate(dispatch.queue(conn))


@router.get("/v1/seats", tags=["seats"], responses=errors(401))
def list_seats(conn: Conn, who: Who) -> list[Seat]:
    """Every seat, then each seat identity that never checked in. `missing` marks those, and any seat
    whose row has not changed in a day."""
    return [Seat.model_validate(row) for row in dispatch.seats(conn)]


@router.get("/v1/seats/{name}", tags=["seats"], responses=errors(401, 404))
def get_seat(conn: Conn, who: Who, name: str) -> Seat:
    return Seat.model_validate(_found(dispatch.seat(conn, name), "seat"))


@router.patch("/v1/seats/{name}", tags=["seats"], responses=errors(401, 403))
def patch_seat(conn: Conn, who: Who, name: str, body: SeatPatch) -> Seat:
    """Created on first report. A seat writes only turn and task on its own row; lane, status, owner
    (Scott's manual hold) and item are Josui's. `since` is the Desk's: it moves when status does.
    The Desk infers Scott's hold from the report: a first typed prompt (turn active with a task) on a
    seat holding no item opens a manual item and holds the seat; a /clear (task null) releases it."""
    own_seat(who, name)
    fields = body.model_dump(exclude_unset=True)
    refused = sorted(set(fields) - SEAT_ROW_FIELDS)
    if refused and not who.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"a seat may not set: {', '.join(refused)}")
    return Seat.model_validate(dispatch.report_seat(conn, name, fields, who.name))


@router.post("/v1/seats/{name}/usage", tags=["seats"], responses=errors(401, 403))
def report_usage(conn: Conn, who: Who, name: str, body: UsageReport) -> Seat:
    """From the seat's hooks and status line: model, billing source, context, session tokens, and the
    session's running cost, of which the increase since the last report is booked to today's spend.
    A seat's lane decides its billing over the report's, so a seat key cannot pick what it bills."""
    own_seat(who, name)
    lane_first = who.role == Role.SEAT
    return Seat.model_validate(dispatch.report_usage(conn, name, body.model_dump(), lane_first))


@router.get("/v1/usage", tags=["views"], responses=errors(401))
def usage(conn: Conn, who: Who) -> Usage:
    """`ashigaru usage`: per seat context, tokens, turn time and model; spend by billing source today
    and this month (UTC), against the limits Josui set."""
    return Usage.model_validate(dispatch.usage(conn))


@router.put("/v1/billing/{billing}", tags=["views"], responses=errors(401, 403))
def set_limits(conn: Conn, who: Admin, billing: Billing, body: BillingLimits) -> Usage:
    """Admin: the daily and monthly limits `usage` reports spend against. Null clears one."""
    dispatch.set_limits(conn, billing, body.model_dump())
    return Usage.model_validate(dispatch.usage(conn))


@router.get("/v1/outcomes", tags=["views"], responses=errors(401))
def outcomes(conn: Conn, who: Who) -> list[OutcomeRow]:
    """`ashigaru outcomes`: each outcome with its stories' total, done, blocked and open counts."""
    return [OutcomeRow.model_validate(row) for row in dispatch.outcomes(conn)]


@router.post("/v1/items/{item_id}/assign", tags=["items"], responses=errors(401, 403, 404, 409))
def assign(conn: Conn, who: Admin, item_id: str, body: Assign) -> Item:
    """Admin: give an open item whose `after` items are all done to a seat. With no seat named, picks
    the seat in the lane that has been free longest and is not held by Scott; 409 when none is."""
    try:
        dispatch.assign(conn, item_id, body.seat, body.lane, who.name)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item") from error
    except store.Conflict as error:
        raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, item_id))


@router.post("/v1/items/{item_id}/wrap", tags=["items"], responses=errors(401, 403, 404, 409))
def wrap(conn: Conn, who: Admin, item_id: str, body: WrapUp) -> Item:
    """Admin: `wrapping` records that /cc seat was sent. `cleared` (the default) refuses with 409
    until the item is memorized (CC-DONE) or `learned` is sent, and while its seat is mid-turn,
    unless `force`; then it clears the wrap and closes an open item in `state`, freeing the seat."""
    try:
        dispatch.wrap(conn, item_id, body.model_dump(), who.name)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item") from error
    except dispatch.Refused as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return Item.model_validate(store.get_item(conn, item_id))


@router.post("/v1/items/{item_id}/claim", tags=["items"], responses=errors(401, 403, 404, 409))
def claim(conn: Conn, who: Who, item_id: str) -> Item:
    """desk_claim: the calling seat takes a queued, unowned, auto item in its lane whose `after` items
    are done. Atomic: of two seats claiming one item, one gets 409. The seat must be free, unheld and
    in a lane; an item with no lane is never claimed, only assigned."""
    if who.role != Role.SEAT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only a seat claims; an admin assigns")
    try:
        dispatch.claim(conn, item_id, who.name)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item") from error
    except store.Conflict as error:
        raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, item_id))


MarkName = Annotated[str, PathParam(pattern=MARK_NAME, description="e.g. sweep")]


@router.get("/v1/marks/{name}", tags=["views"], responses=errors(401, 404))
def get_mark(conn: Conn, who: Who, name: MarkName) -> Mark:
    """A named watermark, such as `sweep`: how far the last sweep reached. 404 until first set."""
    return Mark.model_validate(_found(store.get_mark(conn, name), "mark"))


@router.put("/v1/marks/{name}", tags=["views"], responses=errors(401, 403))
def set_mark(conn: Conn, who: Admin, name: MarkName, body: MarkSet) -> Mark:
    """Admin: set a watermark, replacing its value; the Desk stamps when and by whom."""
    store.set_mark(conn, name, body.value, who.name)
    return Mark.model_validate(store.get_mark(conn, name))


@router.get("/v1/identities", tags=["identities"], responses=errors(401, 403))
def list_identities(conn: Conn, who: Admin) -> list[Identity]:
    return [Identity.model_validate(row) for row in store.list_identities(conn)]


@router.post(
    "/v1/identities",
    tags=["identities"],
    status_code=status.HTTP_201_CREATED,
    responses=errors(401, 403, 409),
)
def create_identity(conn: Conn, who: Admin, body: IdentityCreate) -> Identity:
    try:
        return Identity.model_validate(store.create_identity(conn, body.name, body.role))
    except store.Conflict as error:
        raise _conflict(error) from error


def own_seat(who: auth.Caller, name: str) -> None:
    if not (who.admin or (who.role == Role.SEAT and who.name == name)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{who.name} may not write seat {name}")


def self_or_admin(who: auth.Caller, name: str) -> None:
    if not (who.admin or who.name == name):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{who.name} may not manage keys of {name}")


@router.get("/v1/identities/{name}/keys", tags=["identities"], responses=errors(401, 403))
def list_keys(conn: Conn, who: Who, name: str) -> list[ApiKey]:
    """Key metadata only; secrets are never stored."""
    self_or_admin(who, name)
    return [ApiKey.model_validate(row) for row in store.list_keys(conn, name)]


@router.post(
    "/v1/identities/{name}/keys",
    tags=["identities"],
    status_code=status.HTTP_201_CREATED,
    responses=errors(401, 403, 404),
)
def mint_key(conn: Conn, who: Admin, name: str, body: KeyCreate) -> MintedKey:
    """Admin only. The token is in this response and nowhere else."""
    _found(store.get_identity(conn, name), "identity")
    key_id, token = auth.mint(conn, name, body.label)
    return MintedKey.model_validate({**_found(store.get_key(conn, key_id), "key"), "token": token})


@router.delete("/v1/identities/{name}/keys/{key_id}", tags=["identities"], responses=errors(401, 403, 404))
def revoke_key(conn: Conn, who: Who, name: str, key_id: str) -> ApiKey:
    """An admin revokes any key; an identity its own. Takes effect on the next request."""
    self_or_admin(who, name)
    key = store.get_key(conn, key_id)
    if key is None or key["identity"] != name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    store.revoke_key(conn, key_id)
    return ApiKey.model_validate(store.get_key(conn, key_id))


def create_app(
    db_path: str | Path,
    trusted_proxies: Sequence[str] = (),
    webhook_secrets: Mapping[str, Sequence[bytes]] | None = None,
    forge_tokens: Mapping[str, str] | None = None,
    forge_authors: Mapping[str, Sequence[str]] | None = None,
) -> FastAPI:
    """The service over one SQLite file, migrated before the first request.

    The client address and scheme come from X-Forwarded-For and X-Forwarded-Proto only when the
    request arrives from one of `trusted_proxies` (addresses or CIDRs); with none, never.
    `webhook_secrets` maps a forge adapter's name to its secrets; with none, every delivery is refused.
    `forge_tokens` maps it to an API token for permission checks and required checks; with none, all
    intake is held and no commit's CI is success.
    `forge_authors` maps it to accounts whose own subjects are trusted as a writer's: the app's bot.
    """
    from . import (
        ci,
        mcp,  # imported here: its tools call this module's routes
        webhooks,
    )
    from .forges import DEFAULT, FORGES
    from .mcp import routes as mcp_routes

    conn = store.connect(db_path)
    store.migrate(conn)
    conn.close()
    mcp_lifespan, mcp_route = mcp.install(db_path)
    app = FastAPI(
        title="Ashigaru Desk",
        version=VERSION,
        description="One queue for every source of change, one owner per item (docs/design/ashigaru-3.md).",
        lifespan=mcp_lifespan,
    )
    app.state.db_path = str(db_path)
    app.state.webhook_secrets = webhooks.secrets(webhook_secrets or {})
    app.state.forge_tokens = dict(forge_tokens or {})
    app.state.forge_authors = {forge: frozenset(names) for forge, names in (forge_authors or {}).items()}
    app.state.waiters = ci.Waiters(ci.from_forge(FORGES[DEFAULT], app.state.forge_tokens.get(DEFAULT, "")))
    app.include_router(router)
    app.include_router(webhooks.router)
    app.include_router(mcp_routes.router)
    app.state.mcp = mcp_route is not None
    if mcp_route is not None:
        app.router.routes.append(mcp_route)
    if trusted_proxies:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(trusted_proxies))
    return app
