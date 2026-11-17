"""The Desk's REST API. Everything else (CLI, MCP, hooks, Josui) is a client of this.

Authority: an admin (Josui, Scott) writes anything. A seat reads everything and
writes only items whose `seat` is its own name, only SEAT_FIELDS, and only to
SEAT_STATES; Josui verifies and closes. A ci identity reads.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import auth, store
from .model import (
    SEAT_FIELDS,
    SEAT_STATES,
    ApiKey,
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
    MintedKey,
    Problem,
    Report,
    Role,
    Seat,
    SeatPatch,
    State,
    Verb,
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
    """The item, if `who` may write it: admins any, a seat only its own."""
    item = _found(store.get_item(conn, item_id), "item")
    if who.admin or (who.role == Role.SEAT and item["seat"] == who.name):
        return item
    raise HTTPException(status.HTTP_403_FORBIDDEN, f"{item_id} belongs to {item['seat'] or 'no seat'}")


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
    409: "A taken id or subject_key, an existing identity, or a second child in one assignment.",
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
    """The /agents check table: blocked first, then newest. `all` includes wrapped and dead items."""
    store.expire_proposed(conn)
    return [CheckRow.model_validate(row) for row in store.check(conn, show_all)]


@router.get("/v1/seats", tags=["seats"], responses=errors(401))
def list_seats(conn: Conn, who: Who) -> list[Seat]:
    return [Seat.model_validate(row) for row in store.list_seats(conn)]


@router.get("/v1/seats/{name}", tags=["seats"], responses=errors(401, 404))
def get_seat(conn: Conn, who: Who, name: str) -> Seat:
    return Seat.model_validate(_found(store.get_seat(conn, name), "seat"))


@router.patch("/v1/seats/{name}", tags=["seats"], responses=errors(401, 403))
def patch_seat(conn: Conn, who: Who, name: str, body: SeatPatch) -> Seat:
    """Created on first report. A seat writes only its own row; `since` moves when status does."""
    if not (who.admin or (who.role == Role.SEAT and who.name == name)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{who.name} may not write seat {name}")
    return Seat.model_validate(store.upsert_seat(conn, name, body.model_dump(exclude_unset=True)))


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


def create_app(db_path: str | Path) -> FastAPI:
    """The service over one SQLite file, migrated before the first request."""
    conn = store.connect(db_path)
    store.migrate(conn)
    conn.close()
    app = FastAPI(
        title="Ashigaru Desk",
        version=VERSION,
        description="One queue for every source of change, one owner per item (docs/design/ashigaru-3.md).",
    )
    app.state.db_path = str(db_path)
    app.include_router(router)
    return app
