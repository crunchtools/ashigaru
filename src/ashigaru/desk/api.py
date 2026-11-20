"""The Desk's REST API. Everything else (CLI, MCP, hooks, Josui) is a client of this.

Authority: an admin (Josui, Scott) writes anything. A seat reads everything and
writes only items whose `seat` is its own name, only SEAT_FIELDS, and only to
SEAT_STATES; Josui verifies and closes. A ci identity reads.
"""

import asyncio
import contextlib
import dataclasses
import sqlite3
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import auth, changes, dispatch, store, subjects, sweep
from . import outcomes as served
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
    Changes,
    ChangesAck,
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
    NextItem,
    NextWait,
    OutcomeRow,
    Problem,
    Queue,
    Report,
    Role,
    Round,
    Seat,
    SeatPatch,
    SessionStart,
    SessionStarted,
    State,
    Subject,
    Sweep,
    SweepRun,
    Usage,
    UsageReport,
    Verb,
    Wrap,
    WrapUp,
)
from .model import subject_key as model_subject_key

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
    request: Request, conn: Conn, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> auth.Caller:
    """The key's identity. A request from the identity a seat is named for renews that seat's lease
    (`dispatch.renew`), whatever it asks: the CLI's calls, a `next` the Desk answers with nothing."""
    found = auth.authenticate(conn, credentials.credentials) if credentials else None
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing, unknown or revoked API key")
    held = request.headers.get("X-Ashigaru-Epoch", "").strip()
    session = request.headers.get("X-Ashigaru-Session") or None
    dispatch.renew(conn, found.name, dispatch.Own(int(held) if held.isdigit() else None, session, sent=True))
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


class ProblemError(HTTPException):
    """A 4xx whose body carries a `code` beside its detail (Problem), for a client that acts on it,
    and for a stale-session one the seat's current `session`."""

    def __init__(self, status_code: int, detail: str, code: str | None, session: str | None = None) -> None:
        super().__init__(status_code, detail)
        self.code = code
        self.session = session


async def _problem(request: Request, error: Exception) -> JSONResponse:
    code = error.code if isinstance(error, ProblemError) else None
    status_code = (
        error.status_code if isinstance(error, HTTPException) else status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    body = {"detail": getattr(error, "detail", str(error)), "code": code}
    if code == "stale-session":
        body["session"] = error.session if isinstance(error, ProblemError) else None
    return JSONResponse(body, status_code=status_code)


def _conflict(error: store.Conflict) -> ProblemError:
    """409 with the Conflict's code; 412 when what conflicts is the version the caller named."""
    moved = error.code == "version"
    return ProblemError(
        status.HTTP_412_PRECONDITION_FAILED if moved else status.HTTP_409_CONFLICT,
        str(error),
        error.code,
        error.session,
    )


@dataclasses.dataclass(frozen=True)
class Guard:
    """What a write names about what it read: the seat session it comes from (X-Ashigaru-Session, and
    X-Ashigaru-Epoch from a 5.0 client; a seat's only) and the item version it was decided on
    (If-Match). Any may be absent; `sent` is whether it came over REST, where a seat naming neither
    session nor epoch is a 4.x client."""

    epoch: int | None = None
    version: int | None = None
    session: str | None = None
    sent: bool = False


def _number(value: str | None, what: str) -> int | None:
    """A header that names a number, or None when absent; 422 when it is not one."""
    if value is None:
        return None
    if not value.strip().isdigit():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{what} is not a number: {value}")
    return int(value)


def guard(
    if_match: Annotated[
        str | None, Header(description="The item's `version` the write was decided on.")
    ] = None,
    epoch: Annotated[
        str | None, Header(alias="X-Ashigaru-Epoch", description="The seat session's epoch, from a 5.0 seat.")
    ] = None,
    session: Annotated[
        str | None,
        Header(
            alias="X-Ashigaru-Session",
            description="The seat's Claude Code session id, from its hooks and its CLI.",
        ),
    ] = None,
) -> Guard:
    """The request's Guard. An If-Match may carry an ETag's quotes and W/; either header that is not a
    number is 422."""
    tag = None if if_match is None else if_match.strip().removeprefix("W/").strip('"')
    return Guard(_number(epoch, "X-Ashigaru-Epoch"), _number(tag, "If-Match"), session or None, sent=True)


Guarded = Annotated[Guard, Depends(guard)]
UNGUARDED = Guard()
"""The Guard of a call that names nothing: an MCP tool's, or an internal one."""


def writable_item(
    conn: sqlite3.Connection, item_id: str, who: auth.Caller, checked: Guard = UNGUARDED
) -> dict[str, Any]:
    """The item, if `who` may write it: admins any, a seat only its own and only while it is open.
    Once Josui closes an item (done, wontdo, abandoned) or clears it, a seat cannot reopen it. A seat
    naming its session (or a 5.0 client its epoch) must be in the current one, and the one that took
    the item; any caller
    naming a version must have read the item at it."""
    item = _found(store.get_item(conn, item_id), "item")
    try:
        dispatch.unmoved(item, checked.version)
        if who.admin:
            return item
        if who.role != Role.SEAT or item["seat"] != who.name:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"{item_id} belongs to {item['seat'] or 'no seat'}"
            )
        if item["state"] in CLOSED or item["wrap"] == Wrap.CLEARED:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"{item_id} is closed; only an admin may change it"
            )
        epoch = dispatch.fence(store.get_seat(conn, who.name), who.name, _own(who, who.name, checked))
        if epoch is not None and item["epoch"] not in (None, epoch):
            raise store.Conflict(
                f"{item_id} was taken in session {item['epoch']}, not this one ({epoch})",
                "stale-session",
            )
    except store.Conflict as error:
        raise _conflict(error) from error
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


def _patch_item(
    conn: sqlite3.Connection, item_id: str, fields: dict[str, Any], who: auth.Caller, checked: Guard
) -> Item:
    """One write to an item. A move to another seat takes that seat's current session (`epoch`); the
    store refuses a seat that holds another open item (409 busy)."""
    with store.transaction(conn):
        item = writable_item(conn, item_id, who, checked)
        seat_may(who, fields)
        try:
            fields = with_outcome(conn, fields, who.name)
            if fields.get("parent") == item_id:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "an item cannot serve itself")
            if fields.get("seat") and fields["seat"] != item["seat"]:
                taker = store.get_seat(conn, fields["seat"])
                if taker is None:
                    raise store.Conflict(f"seat {fields['seat']!r} has not reported", "unseen")
                fields["epoch"] = taker["epoch"]
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
        " second child in one assignment, a seat that holds another open item, a seat session the Desk"
        " has ended (`code` stale-session), or an item or seat not in a state to assign, claim or wrap."
    ),
    412: "The item moved since the version named in If-Match (`code` version): read it again.",
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


def with_outcome(conn: sqlite3.Connection, fields: dict[str, Any], actor: str) -> dict[str, Any]:
    """The fields with `outcome`/`outcome_text` turned into the `parent` they name (served.resolve), or
    a null `parent` when both are sent as null; 422 for an outcome that is neither found nor given the
    text a new one needs."""
    if "outcome" not in fields and "outcome_text" not in fields:
        return fields
    name, text = fields.pop("outcome", None), fields.pop("outcome_text", None)
    if name is None and text is None:  # sent as null: the item serves no outcome
        return {**fields, "parent": None}
    try:
        return {**fields, "parent": served.resolve(conn, name, text, actor)}
    except served.Unknown as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error


@router.post(
    "/v1/items", tags=["items"], status_code=status.HTTP_201_CREATED, responses=errors(401, 403, 409)
)
def create_item(conn: Conn, who: Admin, body: ItemCreate) -> Item:
    with store.transaction(conn):
        try:
            fields = with_outcome(conn, body.model_dump(exclude_unset=True), who.name)
            item_id = store.insert_item(conn, fields, who.name)
        except store.Conflict as error:
            raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, item_id))


@router.get("/v1/items/{item_id}", tags=["items"], responses=errors(401, 404))
def get_item(conn: Conn, who: Who, item_id: str) -> Item:
    return Item.model_validate(_found(store.get_item(conn, item_id), "item"))


@router.patch("/v1/items/{item_id}", tags=["items"], responses=errors(401, 403, 404, 409, 412))
def patch_item(conn: Conn, who: Who, item_id: str, body: ItemPatch, checked: Guarded = UNGUARDED) -> Item:
    """Only the fields sent change. A seat change takes the new seat and frees the old; a move to
    queued or proposed frees the seat."""
    return _patch_item(conn, item_id, body.model_dump(exclude_unset=True), who, checked)


@router.post("/v1/items/{item_id}/report", tags=["items"], responses=errors(401, 403, 404, 409, 412))
def report(conn: Conn, who: Who, item_id: str, body: Report, checked: Guarded = UNGUARDED) -> Item:
    """The report contract. DONE moves to review (Josui verifies), BLOCKED to blocked, WORKING to
    working; CC-DONE marks the learnings memorized. Learned: none is not recorded."""
    return _patch_item(conn, item_id, report_fields(body), who, checked)


@router.post(
    "/v1/items/{item_id}/file",
    tags=["items"],
    status_code=status.HTTP_201_CREATED,
    responses=errors(401, 403, 404, 409, 412),
)
def file(conn: Conn, who: Who, item_id: str, body: FileChild, checked: Guarded = UNGUARDED) -> Item:
    """desk_file: record a child of this story as proposed, under the story's outcome. One per
    assignment (409 on the second); abandoned after 14 days unless Josui promotes it."""
    with store.transaction(conn):
        story = writable_item(conn, item_id, who, checked)
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


def mine(who: auth.Caller, seat: dict[str, Any]) -> dict[str, Any]:
    """A seat row as `who` may read it: `waiting_on` is what a seat said to Scott, so only an admin
    and the seat itself see it."""
    return seat if who.admin or seat["name"] == who.name else {**seat, "waiting_on": None}


def _round(conn: sqlite3.Connection, who: auth.Caller, show_all: bool, since: str | None) -> Round:
    try:
        found = dispatch.work(conn, show_all, since)
        return Round.model_validate({**found, "seats": [mine(who, seat) for seat in found["seats"]]})
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
    return _round(conn, who, show_all, since)


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
    found = _round(conn, who, show_all, since)
    moved = store.advance_mark(conn, dispatch.SWEEP, found.catch_up.until, who.name)
    return found.model_copy(update={"catch_up": found.catch_up.model_copy(update={"advanced": moved})})


@router.get("/v1/status", tags=["views"], responses=errors(401))
def board(conn: Conn, who: Who) -> Board:
    """`ashigaru status`, the board a person reads: each busy seat with its work, how long it has held
    it, its last check-in and its whole task; the free and missing seats by name; how many seats are
    up and items open; and NEEDS YOU: each manual seat idle on what it last said, Scott's blocked work,
    and subjects held for his approval. A seat other than an admin sees only its own question."""
    found = dispatch.status(conn)
    needs = [
        need
        for need in found["needs_you"]
        if need["why"] != "waiting" or who.admin or need["seat"] == who.name
    ]
    return Board.model_validate({**found, "needs_you": needs, "changes": changes.read(conn, who.name)})


@router.get("/v1/changes", tags=["views"], responses=errors(401, 422))
def changed(conn: Conn, who: Who, since: Since = None) -> Changes:
    """`ashigaru changes`: each item someone else changed after the caller's own seen mark (else the
    last day, or from `since`), once, last changed first, with the fields that moved, who moved them
    and its NEXT now. It leaves the mark alone."""
    try:
        return Changes.model_validate(changes.read(conn, who.name, since))
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"since is not a time: {error}") from error


@router.post("/v1/changes", tags=["views"], responses=errors(401))
def ack(conn: Conn, who: Who, body: ChangesAck | None = None) -> Changes:
    """`ashigaru changes --ack`: the same look, then the caller's seen mark moves to its `until`, only
    forward, so the next look starts after it; with `until`, no further than that event, the end of
    the look the caller was shown. `acked` is false when it was already there."""
    return Changes.model_validate(changes.ack(conn, who.name, body.until if body else None))


@router.get("/v1/subjects/{key:path}", tags=["views"], responses=errors(401, 404))
def subject(conn: Conn, who: Who, key: str) -> Subject:
    """The forge's own state of an issue or PR, by owner/repo#N or its URL (encode the `#`): open,
    closed or merged, head, draft, review, title and labels, as the webhook last said."""
    return Subject.model_validate(_found(subjects.get(conn, model_subject_key(key) or key), "subject"))


def sweep_view(sweeper: sweep.Sweeper | None, conn: sqlite3.Connection, requested: bool = False) -> Sweep:
    """The sweep as `ashigaru sweep` shows it, for the REST route and the MCP tool alike."""
    return Sweep(
        configured=sweeper is not None,
        every=sweeper.settings.every if sweeper else None,
        running=bool(sweeper and sweeper.running),
        requested=requested,
        last=SweepRun.model_validate(found) if (found := sweep.last(conn)) else None,
    )


def sweep_start(sweeper: sweep.Sweeper | None, conn: sqlite3.Connection) -> Sweep:
    """Wake the timer; call it on the event loop. 409 when no timer runs."""
    if sweeper is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no sweep runs here: set the forge's token and org")
    sweeper.request()
    return sweep_view(sweeper, conn, requested=True)


@router.get("/v1/sweep", tags=["views"], responses=errors(401))
def sweep_state(request: Request, conn: Conn, who: Who) -> Sweep:
    """`ashigaru sweep`: whether the timer that reads the forge back runs here, how often, whether a
    run is under way, and the last run's counts and error."""
    return sweep_view(request.app.state.sweeper, conn)


@router.post(
    "/v1/sweep",
    tags=["views"],
    responses={
        **errors(401, 403),
        409: {"model": Problem, "description": "No sweep runs here: the forge's token or org is not set."},
    },
)
async def sweep_now(request: Request, conn: Conn, who: Admin) -> Sweep:
    """`ashigaru sweep --run`, admin: wake the timer for a run now, and return at once; the run's
    counts land in `last` when it finishes. 409 when no timer runs (no forge token or org)."""
    return sweep_start(request.app.state.sweeper, conn)


@router.get("/v1/queue", tags=["views"], responses=errors(401))
def queue(conn: Conn, who: Who) -> Queue:
    """`ashigaru queue`: queued items with the state of each item they wait on, then proposed children
    with their expiry."""
    return Queue.model_validate(dispatch.queue(conn))


@router.get("/v1/seats", tags=["seats"], responses=errors(401))
def list_seats(conn: Conn, who: Who) -> list[Seat]:
    """Every seat, then each seat identity that never checked in. `missing` marks those, and any seat
    whose row has not changed in a day."""
    return [Seat.model_validate(mine(who, row)) for row in dispatch.seats(conn)]


@router.get("/v1/seats/{name}", tags=["seats"], responses=errors(401, 404))
def get_seat(conn: Conn, who: Who, name: str) -> Seat:
    return Seat.model_validate(mine(who, _found(dispatch.seat(conn, name), "seat")))


OCCUPANCY = ("status", "item", "owner")
"""A seat row's fields its items decide: no one writes them, so they never disagree with the items."""


def _own(who: auth.Caller, name: str, checked: Guard) -> dispatch.Own | None:
    """The request is the seat's own, from the session named, or None for an admin's."""
    if who.role != Role.SEAT or who.name != name:
        return None
    return dispatch.Own(checked.epoch, checked.session, checked.sent)


@router.patch(
    "/v1/seats/{name}",
    tags=["seats"],
    responses={
        **errors(401, 403),
        409: {
            "model": Problem,
            "description": "`derived`: the write would change status, item or owner, which follow the seat's"
            " items; `stale-session`: the seat's own write came from a session the Desk has ended.",
        },
    },
)
def patch_seat(conn: Conn, who: Who, name: str, body: SeatPatch, checked: Guarded = UNGUARDED) -> Seat:
    """Created on first report. A seat writes only turn, task and waiting_on on its own row, which
    renews its lease; Josui sets its lane. Status, item and owner (Scott's manual hold) are what the
    seat's open item says: a write that would change one is 409 (assign, claim, update or wrap the
    item instead). `since` is the Desk's: it moves when status does. The Desk infers what happened in
    the seat from the report: a turn starting acknowledges Josui's item, unless it is Scott's first
    typed prompt (turn active with a task), which hands an unstarted item back and opens his manual
    item, holding the seat; a /clear (task null) ends what the session held."""
    own_seat(who, name)
    fields = body.model_dump(exclude_unset=True)
    refused = sorted(set(fields) - SEAT_ROW_FIELDS)
    if refused and not who.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"a seat may not set: {', '.join(refused)}")
    current = store.get_seat(conn, name) or {**store.SEAT_DEFAULTS, "status": "free"}
    derived = sorted(key for key in OCCUPANCY if key in fields and fields[key] != current[key])
    if derived:
        raise ProblemError(
            status.HTTP_409_CONFLICT,
            f"{', '.join(derived)} follow the seat's items: change the item",
            "derived",
        )
    fields = {key: value for key, value in fields.items() if key not in OCCUPANCY}
    try:
        return Seat.model_validate(
            dispatch.report_seat(conn, name, fields, who.name, _own(who, name, checked))
        )
    except store.Conflict as error:
        raise _conflict(error) from error


@router.post(
    "/v1/seats/{name}/session",
    tags=["seats"],
    responses={
        **errors(401, 403),
        409: {
            "model": Problem,
            "description": "`stale-session`: a resume of a session the Desk has ended (its lease ran out, or"
            " a newer session started); the session must not act on what it remembers.",
        },
    },
)
def start_session(conn: Conn, who: Who, name: str, body: SessionStart) -> SessionStarted:
    """The seat's SessionStart hook. A new session (startup, clear) becomes the seat's current one,
    which every later request names as X-Ashigaru-Session, at the next epoch, and ends the one
    before: Josui's item it was working is blocked (cleared before its wrap), Scott's hold ends on a
    clear, and the task is dropped for the new session's first typed prompt to set. A resume stays
    the seat's current session while its lease is live; any other resume is 409 stale-session, and
    the session must not act on what it remembers. The epoch answered is for 5.0 clients."""
    own_seat(who, name)
    try:
        epoch, seat = dispatch.start_session(conn, name, body.session_id, body.source, who.name)
    except store.Conflict as error:
        raise _conflict(error) from error
    return SessionStarted(epoch=epoch, seat=Seat.model_validate(dispatch.seat(conn, name)))


NEXT_TICK = 2.0
"""Seconds between a waiting seat's looks at the queue."""


@router.post(
    "/v1/seats/{name}/next",
    tags=["seats"],
    response_model=NextItem,
    responses={**errors(401, 403, 409), 204: {"description": "No work yet, or the switch is off."}},
)
async def next_item(
    request: Request, conn: Conn, who: Who, name: str, query: Annotated[NextWait, Query()]
) -> NextItem | Response:
    """The seat's `next` hook: claim the oldest queued auto item in the seat's lane whose `after` items
    are done, waiting up to `wait` seconds for one while the seat is idle. 204 when none came, the
    `autodispatch` mark is not `on`, or the client went away (nothing is claimed for a seat no longer
    listening, and a claim made as it left is handed back); 409 when the seat cannot take work, its
    `code` saying why: busy or held (for now: ask again later), unseen or no-lane, or stale-session for
    a session the Desk has ended. Asking renews the seat's lease. The seat's next turn acknowledges a
    claim; one not acknowledged in ACK_AFTER goes back to the queue."""
    own_seat(who, name)
    headers = request.headers
    own = _own(
        who, name, guard(epoch=headers.get("X-Ashigaru-Epoch"), session=headers.get("X-Ashigaru-Session"))
    )
    try:
        if own:
            await run_in_threadpool(dispatch.check_in, conn, name, own)
    except store.Conflict as error:
        raise _conflict(error) from error
    loop = asyncio.get_running_loop()
    until = loop.time() + query.wait
    while True:
        if await request.is_disconnected():
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        try:
            item = await run_in_threadpool(dispatch.next_item, conn, name, own)
        except store.Conflict as error:
            raise _conflict(error) from error
        if item is not None and await request.is_disconnected():
            await run_in_threadpool(dispatch.unclaim, conn, item["id"], name)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if item is not None:
            return NextItem(item=Item.model_validate(item), brief=dispatch.brief(item))
        if loop.time() + NEXT_TICK > until:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await asyncio.sleep(NEXT_TICK)


@router.post(
    "/v1/seats/{name}/usage",
    tags=["seats"],
    responses={
        **errors(401, 403),
        409: {
            "model": Problem,
            "description": "`stale-session`: the seat's own report came from a session the Desk has ended;"
            " nothing is booked and the lease is not renewed.",
        },
    },
)
def report_usage(conn: Conn, who: Who, name: str, body: UsageReport, checked: Guarded = UNGUARDED) -> Seat:
    """From the seat's hooks and status line: model, billing source, context, session tokens, and the
    session's running cost, of which the increase since the last report is booked to today's spend.
    A seat's lane decides its billing over the report's, so a seat key cannot pick what it bills. A
    seat's own report renews its lease; from a session the Desk has ended it is 409 stale-session."""
    own_seat(who, name)
    try:
        found = dispatch.report_usage(conn, name, body.model_dump(), _own(who, name, checked))
    except store.Conflict as error:
        raise _conflict(error) from error
    return Seat.model_validate(found)


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


@router.post("/v1/items/{item_id}/assign", tags=["items"], responses=errors(401, 403, 404, 409, 412))
def assign(conn: Conn, who: Admin, item_id: str, body: Assign, checked: Guarded = UNGUARDED) -> Item:
    """Admin: give an open item whose `after` items are all done to a seat, in the seat's current
    session. With no seat named, picks the seat in the lane that has been free longest, is idle and
    up (its lease live) and is not held by Scott; 409 when none is, or the seat named is missing."""
    try:
        dispatch.assign(conn, item_id, body.model_dump(), who.name, checked.version)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item") from error
    except store.Conflict as error:
        raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, item_id))


@router.post("/v1/items/{item_id}/wrap", tags=["items"], responses=errors(401, 403, 404, 409, 412))
def wrap(conn: Conn, who: Admin, item_id: str, body: WrapUp, checked: Guarded = UNGUARDED) -> Item:
    """Admin: `wrapping` records that /cc seat was sent. `cleared` (the default) refuses with 409
    until the item is memorized (CC-DONE) or `learned` is sent, and while its seat is mid-turn,
    unless `force`; then it clears the wrap and closes an open item in `state`, freeing the seat."""
    try:
        dispatch.wrap(conn, item_id, body.model_dump(), who.name, checked.version)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item") from error
    except dispatch.Refused as error:
        raise ProblemError(status.HTTP_409_CONFLICT, str(error), "not-ready") from error
    except store.Conflict as error:
        raise _conflict(error) from error
    return Item.model_validate(store.get_item(conn, item_id))


@router.post("/v1/items/{item_id}/claim", tags=["items"], responses=errors(401, 403, 404, 409, 412))
def claim(conn: Conn, who: Who, item_id: str, checked: Guarded = UNGUARDED) -> Item:
    """desk_claim: the calling seat takes a queued, unowned, auto item in its lane whose `after` items
    are done. Atomic: of two seats claiming one item, one gets 409. The seat must be free, unheld and
    in a lane; an item with no lane is never claimed, only assigned."""
    if who.role != Role.SEAT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only a seat claims; an admin assigns")
    try:
        dispatch.claim(conn, item_id, who.name, _own(who, who.name, checked), checked.version)
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
    A `sweep.Sweeper` set as `app.state.sweeper` before startup runs while the app does (serve sets
    one when the forge has a token and an org); with none, nothing is swept. A `reconcile.Reconciler`
    set as `app.state.reconciler` runs the same way (serve always sets one).
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

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        timers = [
            asyncio.create_task(each.loop()) for each in (app.state.sweeper, app.state.reconciler) if each
        ]
        try:
            if mcp_lifespan is None:
                yield
            else:
                async with mcp_lifespan(app):
                    yield
        finally:
            for timer in timers:
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer

    app = FastAPI(
        title="Ashigaru Desk",
        version=VERSION,
        description="One queue for every source of change, one owner per item (docs/design/ashigaru-3.md).",
        lifespan=lifespan,
    )
    app.state.sweeper = None
    app.state.reconciler = None
    app.state.db_path = str(db_path)
    app.state.webhook_secrets = webhooks.secrets(webhook_secrets or {})
    app.state.forge_tokens = dict(forge_tokens or {})
    app.state.forge_authors = {forge: frozenset(names) for forge, names in (forge_authors or {}).items()}
    app.state.waiters = ci.Waiters(ci.from_forge(FORGES[DEFAULT], app.state.forge_tokens.get(DEFAULT, "")))
    app.add_exception_handler(ProblemError, _problem)
    app.include_router(router)
    app.include_router(webhooks.router)
    app.include_router(mcp_routes.router)
    app.state.mcp = mcp_route is not None
    if mcp_route is not None:
        app.router.routes.append(mcp_route)
    if trusted_proxies:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(trusted_proxies))
    return app
