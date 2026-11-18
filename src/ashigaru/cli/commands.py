"""The command set the CLI, the /ashigaru skill and the MCP tools share: the same names and the same
arguments. Each returns plain data; rendering is the caller's."""

import os
from collections.abc import Callable, Sequence
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from .client import Desk, DeskError, Unreachable
from .wait import CIWaiter, waiter_for


class CommandError(Exception):
    """A command refused on the client's side, before anything changed."""


class Refused(CommandError):
    """The Desk refused, and `item` is the one a caller wants anyway: the one holding a subject_key,
    or one recorded before its assign was refused. The CLI prints it, then exits 1."""

    def __init__(self, message: str, item: dict[str, Any]) -> None:
        super().__init__(message)
        self.item = item


def _item(item_id: str) -> str:
    return f"/v1/items/{quote(item_id, safe='')}"


def _codes_of(name: str) -> str:
    return f"/v1/identities/{quote(name, safe='')}/mcp"


# Views


def usage(desk: Desk) -> dict[str, Any]:
    """Per seat: model, context, session tokens and cost, turn; spend by billing source vs limits."""
    result: dict[str, Any] = desk.get("/v1/usage")
    return result


def agents(desk: Desk) -> list[dict[str, Any]]:
    """One row per seat: free or busy, owner, the item it holds, turn, since."""
    result: list[dict[str, Any]] = desk.get("/v1/seats")
    return result


def work(desk: Desk, all: bool = False) -> list[dict[str, Any]]:
    """The /agents check table, blocked first then newest; `all` adds cleared and closed items."""
    result: list[dict[str, Any]] = desk.get("/v1/check", all="true" if all else None)
    return result


def queue(desk: Desk) -> dict[str, list[dict[str, Any]]]:
    """Queued items with the state of each item they wait on (`waits_on`), and agent-filed `proposed`
    children with their expiry."""
    result: dict[str, list[dict[str, Any]]] = desk.get("/v1/queue")
    return result


def outcomes(desk: Desk) -> list[dict[str, Any]]:
    """Each outcome with its stories' total, done, blocked and open counts."""
    result: list[dict[str, Any]] = desk.get("/v1/outcomes")
    return result


def items(
    desk: Desk,
    state: Sequence[str] | None = None,
    learned: bool | None = None,
    memorized: bool | None = None,
    **filters: str | int | None,
) -> list[dict[str, Any]]:
    """Items, newest first, matching every filter given: any of `state`, seat, lane, kind, mode,
    parent, subject_key, ref, whether a Learned line is recorded, memorized; pages of limit from
    offset. The MCP tool is desk_list."""
    flags = {"learned": learned, "memorized": memorized}
    params = {**filters, **{k: str(v).lower() for k, v in flags.items() if v is not None}}
    result: list[dict[str, Any]] = desk.get("/v1/items", state=state or None, **params)
    return result


def get(
    desk: Desk, item: str | None = None, subject_key: str | None = None, ref: str | None = None
) -> dict[str, Any]:
    """One item: by id, by subject_key (unique), or the newest with this ref. The MCP tool is desk_get,
    and desk_list finds one by subject_key or ref."""
    if sum(x is not None for x in (item, subject_key, ref)) != 1:
        raise CommandError("name one of: an item, --subject-key, --ref")
    if item is not None:
        one: dict[str, Any] = desk.get(_item(item))
        return one
    found: list[dict[str, Any]] = desk.get("/v1/items", subject_key=subject_key, ref=ref, limit=1)
    if not found:
        raise CommandError(f"no item with {'subject_key' if subject_key else 'ref'} {subject_key or ref}")
    return found[0]


# Actions


def create(desk: Desk, **fields: Any) -> dict[str, Any]:
    """Admin: record an item, `queued` unless `state` says otherwise, without assigning it. A taken
    subject_key or id raises Refused carrying the item that holds it. The MCP tool is desk_create."""
    body = {key: value for key, value in fields.items() if value is not None}
    try:
        created: dict[str, Any] = desk.post("/v1/items", body)
    except DeskError as exc:
        if exc.status != HTTPStatus.CONFLICT:
            raise
        # A create's only conflicts: the subject_key is taken, else the id is.
        found = (
            desk.get("/v1/items", subject_key=body["subject_key"], limit=1) if body.get("subject_key") else []
        )
        if not found and body.get("id"):
            found = [desk.get(_item(body["id"]))]
        if not found:
            raise
        raise Refused(f"{found[0]['id']} exists: {exc.detail}", found[0]) from exc
    return created


def update(desk: Desk, item: str, clear: Sequence[str] = (), **fields: Any) -> dict[str, Any]:
    """Set the fields given; each name in `clear` is set to null. A seat may set only state
    (working, review, blocked), result, next, learned, note, blocked_reason, memorized and
    check_after on its own open item. The MCP tool is desk_update."""
    body = {key: value for key, value in fields.items() if value is not None}
    body |= dict.fromkeys(clear)
    if not body:
        raise CommandError("nothing to update; give a field, or --clear one")
    result: dict[str, Any] = desk.request("PATCH", _item(item), body=body)
    return result


def report(desk: Desk, item: str, verb: str, **lines: str | None) -> dict[str, Any]:
    """The report contract, with `text`, `next` and `learned` in `lines`: DONE moves to review with
    the text as the result, BLOCKED to blocked with it as the reason, WORKING to working with it as
    the note; CC-DONE marks the learnings memorized. A `learned` of none is not recorded. The MCP
    tool is desk_report."""
    body = {"verb": verb.upper(), **{k: v for k, v in lines.items() if v is not None}}
    result: dict[str, Any] = desk.post(f"{_item(item)}/report", body)
    return result


def assign(
    desk: Desk,
    item: str | None = None,
    *,
    seat: str | None = None,
    lane: str | None = None,
    **record: str | list[str] | None,
) -> dict[str, Any]:
    """Assign an item to a seat: `seat` names one, else the Desk picks the seat in `lane` (default:
    the item's) free longest and not held by Scott. With a `task` in `record` (and optionally
    done_when, ref, repo, after, mode, subject_key), the item is recorded first, in `lane`; if the
    subject_key is taken, or the assign is refused, Refused carries the item so its id is not lost."""
    if seat and lane:
        raise CommandError("name a seat or a lane, not both")
    if record.get("task"):
        item = create(desk, id=item, lane=lane, **record)["id"]
    elif not item:
        raise CommandError("name an item, or give --task to record a new one")
    body = {"seat": seat} if seat else {"lane": lane} if lane else {}
    try:
        result: dict[str, Any] = desk.post(f"{_item(item)}/assign", body)
    except DeskError as exc:
        if not record.get("task") or exc.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise
        raise Refused(f"recorded {item}, not assigned: {exc.detail}", desk.get(_item(item))) from exc
    return result


def auto(desk: Desk, item: str) -> dict[str, Any]:
    """Hand an item to Josui."""
    result: dict[str, Any] = desk.request("PATCH", _item(item), body={"mode": "auto"})
    return result


def manual(desk: Desk, item: str) -> dict[str, Any]:
    """Hand an item to Scott."""
    result: dict[str, Any] = desk.request("PATCH", _item(item), body={"mode": "manual"})
    return result


def wait(desk: Desk, target: Sequence[str], timeout: float | None = None) -> dict[str, Any]:
    """`[item]`: block until the item settles (review, done, blocked, wontdo, abandoned); returns it.
    `[repo, sha]`: block until the commit's CI is no longer pending; returns its CI result. The MCP
    tool for a commit is desk_wait."""
    item_or_repo, *sha = target
    if not sha:
        return waiter_for(desk).wait(item_or_repo, timeout)
    if len(sha) > 1:
        raise CommandError("wait takes an item, or a repo and a sha")
    return CIWaiter(desk).wait(item_or_repo, sha[0], timeout)


def wrap(desk: Desk, item: str, **options: str | bool | None) -> dict[str, Any]:
    """`options` are stage, learned, state and force. Stage `wrapping` records that /cc seat was sent.
    `cleared`, the default, refuses until CC-DONE (or a `learned`) and an idle turn, unless `force`;
    then clears the wrap and closes the item in `state` (default done), which frees the seat. The
    Desk decides, so the CLI and the MCP tool mean the same thing."""
    body = {k: v for k, v in options.items() if v is not None}
    result: dict[str, Any] = desk.post(f"{_item(item)}/wrap", body)
    return result


# Hosted MCP URLs. The CLI's alone: no MCP tool mints a credential.


def mcp_mint(desk: Desk, identity: str, label: str | None = None, out: str | None = None) -> dict[str, Any]:
    """Admin: a new hosted MCP URL for `identity`. The URL ends in the code and is the credential;
    the Desk shows it once. With `out` it goes only to that new file, mode 0600, and the result
    carries the code's metadata instead."""
    if out is None:
        minted: dict[str, Any] = desk.post(_codes_of(identity), {"label": label})
        return minted
    try:
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CommandError(f"{out} exists; mcp mint writes only a new file") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        try:
            minted = desk.post(_codes_of(identity), {"label": label})
        except (DeskError, Unreachable):
            os.unlink(out)
            raise
        try:
            file.write(minted["url"] + "\n")
            file.flush()
            os.fsync(file.fileno())
        except OSError as exc:
            os.unlink(out)
            raise CommandError(f"could not write {out} ({exc}); {_revoke_unheld(desk, minted)}") from exc
    return {key: value for key, value in minted.items() if key != "url"} | {"out": out}


def _revoke_unheld(desk: Desk, minted: dict[str, Any]) -> str:
    """A URL nobody holds is still a live credential: revoke it, and say whether that worked."""
    code_id = minted["code_id"]
    try:
        desk.request("DELETE", f"{_codes_of(minted['identity'])}/{quote(code_id, safe='')}")
    except (DeskError, Unreachable) as exc:
        return f"revoking code {code_id} failed too ({exc}); run: ashigaru mcp revoke {code_id}"
    return f"code {code_id} was revoked"


def mcp_list(desk: Desk, identity: str | None = None) -> list[dict[str, Any]]:
    """The MCP codes of `identity`; with none named, every identity's for an admin, else the
    caller's own. Metadata only: the Desk keeps no code it could show."""
    if identity:
        names = [identity]
    else:
        me = desk.get("/v1/me")
        names = [row["name"] for row in desk.get("/v1/identities")] if me["role"] == "admin" else [me["name"]]
    return [code for name in names for code in desk.get(_codes_of(name))]


def mcp_revoke(desk: Desk, code_id: str, identity: str | None = None) -> dict[str, Any]:
    """Revoke a code by its id, found among the codes `mcp_list` would show. Effective on the
    URL's next request."""
    owner = next((code["identity"] for code in mcp_list(desk, identity) if code["code_id"] == code_id), None)
    if owner is None:
        raise CommandError(f"no MCP code {code_id} that this key can see")
    result: dict[str, Any] = desk.request("DELETE", f"{_codes_of(owner)}/{quote(code_id, safe='')}")
    return result


COMMANDS: dict[str, Callable[..., Any]] = {
    command.__name__: command
    for command in (
        *(usage, agents, work, queue, outcomes, get),
        *(create, update, report, assign, auto, manual, wait, wrap),
        *(mcp_mint, mcp_list, mcp_revoke),
    )
} | {"list": items}
