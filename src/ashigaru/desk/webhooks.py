"""POST /webhook/<forge>, the door every forge posts to, and GET /v1/ci/wait, the long-poll on what
it brings. Only /webhook/ is reachable from the internet; Cloudflare and the proxy block the rest.

A delivery carries no API key: the named forge's adapter checks its signature
against each of that forge's configured secrets, and a delivery none verifies
(or one to a forge with no adapter or no secret) gets 401 with nothing of it
echoed. A delivery is remembered by a digest of what was signed (the forge, the
event and the body), not by a delivery id the signature does not cover, in the
same transaction as what it changed: a replay or redelivery is a no-op, and a
delivery that failed half way is processed whole when the forge retries.

Intake is held (no lane, mode manual, for Josui to assign) unless the forge
says its author may write to the repo, or until a sender who may write adds a
lane, which is a maintainer's approval. The checks run before the transaction,
so no write lock waits on the forge's API.
"""

import dataclasses
import hashlib
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Query as Param
from starlette.concurrency import run_in_threadpool

from . import ci, intake, store
from .api import Conn, Who, errors
from .forges import FORGES, Delivery, Forge, IntakeItem
from .model import CIResult, CIWait, Problem, WebhookAck

MAX_BODY = 2 * 1024 * 1024
"""Issue, PR and CI payloads are tens of KB; nothing this Desk takes needs more."""

UNVERIFIED = "unverified delivery"

router = APIRouter()


def secrets(configured: Mapping[str, Sequence[bytes]]) -> dict[str, list[bytes]]:
    """Each adapter's secrets (more than one while a secret rotates), refusing a forge with no adapter."""
    unknown = sorted(set(configured) - set(FORGES))
    if unknown:
        raise ValueError(f"no forge adapter named {', '.join(unknown)}")
    return {name: [secret for secret in values if secret] for name, values in configured.items()}


async def _body(request: Request) -> bytes:
    too_large = HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "delivery too large")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        raise too_large
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def digest(source: str, event: str, body: bytes) -> str:
    """What a delivery is remembered by: only signed bytes, plus the route and event it came by."""
    return hashlib.sha256(b"\0".join((source.encode(), event.encode(), body))).hexdigest()


def _judge(adapter: Forge, token: str, item: IntakeItem, existing: dict[str, Any] | None) -> IntakeItem:
    """Ask the forge only what can change how the item is queued: for a new or still-held subject,
    whether its author writes (asked again while held, so a failed check is retried on the next
    delivery), then whether an event that adds a lane comes from a writer; for an approved one,
    whether the sender of an event that moves its head, or removes its lane, writes."""
    asked: dict[str, bool] = {}

    def writes(account: str | None) -> bool:
        if not (item.repo and account):
            return False
        if account not in asked:
            asked[account] = adapter.may_write(item.repo, account, token)
        return asked[account]

    if existing is None and item.closed:
        return item
    if existing is None or intake.held(existing):
        if (existing is None or not intake.approved(existing)) and writes(item.author):
            return dataclasses.replace(item, author_writes=True)
        approval = intake.lane(item.approves_lane)
        return dataclasses.replace(item, approved=True) if approval and writes(item.sender) else item
    if not intake.approved(existing):
        return item
    extra = existing["extra"]
    unlabeled = item.revokes_lane is not None and item.revokes_lane == extra.get("approved_lane")
    moved = item.head is not None and extra.get("approved_head") not in (None, item.head)
    if unlabeled and writes(item.sender):
        return dataclasses.replace(item, revoked=True)
    return dataclasses.replace(item, revoked=True) if moved and not writes(item.sender) else item


def _judged(db_path: str, adapter: Forge, token: str, signed: str, delivery: Delivery) -> Delivery:
    """The delivery with its intake judged, unless it was already processed."""
    conn = store.connect(db_path)
    try:
        if store.delivery_seen(conn, adapter.name, signed):
            return delivery
        existing = [store.item_by_subject(conn, item.subject_key) for item in delivery.intake]
    finally:
        conn.close()
    items = tuple(
        _judge(adapter, token, item, found) for item, found in zip(delivery.intake, existing, strict=True)
    )
    return dataclasses.replace(delivery, intake=items)


def _apply(db_path: str, source: str, signed: str, delivery: Delivery) -> tuple[list[ci.Key], bool]:
    """Record and apply one delivery in one transaction. Returns the commits whose CI changed, and
    whether the delivery was new."""
    conn = store.connect(db_path)
    try:
        with store.transaction(conn):
            if not store.record_delivery(conn, source, signed, delivery.event):
                return [], False
            commits = [ci.record(conn, update, source) for update in delivery.ci]
            for item in delivery.intake:
                intake.upsert(conn, item)
        return commits, True
    finally:
        conn.close()


@router.post(
    "/webhook/{forge}",
    tags=["webhooks"],
    responses={
        400: {"model": Problem, "description": "Verified, but not a delivery the adapter can read."},
        401: {"model": Problem, "description": "No secret configured for this forge verifies the signature."},
        413: {"model": Problem, "description": "Larger than any delivery the Desk takes."},
    },
)
async def webhook(request: Request, forge: str) -> WebhookAck:
    """A forge's webhook delivery, authenticated by its signature rather than an API key. CI events
    update the commit's result and wake its waiters; issue and PR events upsert queue items."""
    body = await _body(request)
    adapter: Forge | None = FORGES.get(forge)
    configured: list[bytes] = request.app.state.webhook_secrets.get(forge, [])
    if adapter is None or not any(adapter.verify(request.headers, body, secret) for secret in configured):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, UNVERIFIED)
    try:
        delivery = adapter.parse(request.headers, body)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    signed = digest(adapter.name, delivery.event, body)
    if delivery.intake:
        token: str = request.app.state.forge_tokens.get(forge, "")
        db_path: str = request.app.state.db_path
        delivery = await run_in_threadpool(_judged, db_path, adapter, token, signed, delivery)
    commits, new = await run_in_threadpool(_apply, request.app.state.db_path, adapter.name, signed, delivery)
    request.app.state.waiters.notify(commits)
    return WebhookAck(duplicate=not new, handled=bool(delivery.ci or delivery.intake))


@router.get("/v1/ci/wait", tags=["ci"], responses=errors(401))
async def ci_wait(request: Request, conn: Conn, who: Who, query: Annotated[CIWait, Param()]) -> CIResult:
    """desk_wait: the commit's CI result as soon as it is no longer pending, or as it stands when
    `timeout` runs out (status still `pending`; wait again). Nothing polls the forge: a CI webhook
    delivery for the commit is what ends the wait."""
    waiters: ci.Waiters = request.app.state.waiters
    return await waiters.wait(conn, query.repo, query.sha, query.timeout)
