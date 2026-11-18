"""The command set the CLI, the /ashigaru skill and the MCP tools share: the same names and the same
arguments. Each returns plain data; rendering is the caller's."""

import os
from collections.abc import Callable
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from .client import Desk, DeskError, Unreachable
from .wait import waiter_for

CLOSED = frozenset({"done", "wontdo", "abandoned"})


class CommandError(Exception):
    """A command refused on the client's side, before anything changed."""


def _item(item_id: str) -> str:
    return f"/v1/items/{quote(item_id, safe='')}"


def _seat(name: str) -> str:
    return f"/v1/seats/{quote(name, safe='')}"


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
    """Queued items with the state of each item they wait on, and agent-filed `proposed` children
    with their expiry."""
    queued: list[dict[str, Any]] = desk.get("/v1/items", state="queued", limit=1000)
    proposed: list[dict[str, Any]] = desk.get("/v1/items", state="proposed", limit=1000)
    # One page of recent items answers most; only an older dependency costs a request of its own.
    states = {row["id"]: row["state"] for row in desk.get("/v1/items", limit=1000)}
    for after in sorted({after for item in queued for after in item.get("after") or []} - set(states)):
        try:
            states[after] = desk.get(_item(after))["state"]
        except DeskError as exc:
            if exc.status != HTTPStatus.NOT_FOUND:
                raise
            states[after] = "unknown"
    for item in queued:
        item["waits_on"] = [{"id": a, "state": states[a]} for a in item.get("after") or []]
    return {"queued": queued, "proposed": proposed}


def outcomes(desk: Desk) -> list[dict[str, Any]]:
    """Each outcome with its stories' total, done, blocked and open counts."""
    result: list[dict[str, Any]] = desk.get("/v1/outcomes")
    return result


# Actions


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
    done_when, ref, repo, after, mode), the item is recorded first, in `lane`."""
    if seat and lane:
        raise CommandError("name a seat or a lane, not both")
    if record.get("task"):
        fields = {"id": item, "lane": lane, **record}
        item = desk.post("/v1/items", {k: v for k, v in fields.items() if v is not None})["id"]
    elif not item:
        raise CommandError("name an item, or give --task to record a new one")
    body = {"seat": seat} if seat else {"lane": lane} if lane else {}
    result: dict[str, Any] = desk.post(f"{_item(item)}/assign", body)
    return result


def auto(desk: Desk, item: str) -> dict[str, Any]:
    """Hand an item to Josui."""
    result: dict[str, Any] = desk.request("PATCH", _item(item), body={"mode": "auto"})
    return result


def manual(desk: Desk, item: str) -> dict[str, Any]:
    """Hand an item to Scott."""
    result: dict[str, Any] = desk.request("PATCH", _item(item), body={"mode": "manual"})
    return result


def wait(desk: Desk, item: str, timeout: float | None = None) -> dict[str, Any]:
    """Block until the item settles (review, done, blocked, wontdo, abandoned); returns it."""
    return waiter_for(desk).wait(item, timeout)


def wrap(
    desk: Desk, item: str, learned: str | None = None, state: str = "done", force: bool = False
) -> dict[str, Any]:
    """Record the learnings, confirm the seat is idle and its learnings saved (CC-DONE), then clear
    the wrap and close the item, which frees the seat. `force` skips the two confirmations."""
    current = desk.get(_item(item))
    if not force:
        if not (current.get("memorized") or learned):
            raise CommandError(f"{item}: no CC-DONE yet; send /cc seat first, or pass --force")
        if current.get("seat") and desk.get(_seat(current["seat"]))["turn"] == "active":
            raise CommandError(f"{current['seat']} is mid-turn; wrap it once it is idle")
    fields: dict[str, Any] = {"wrap": "cleared"}
    if learned:
        fields["learned"] = learned
    if current["state"] not in CLOSED:
        fields["state"] = state
    result: dict[str, Any] = desk.request("PATCH", _item(item), body=fields)
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
            # A URL nobody holds is still a live credential: revoke it before reporting.
            desk.request("DELETE", f"{_codes_of(identity)}/{quote(minted['code_id'], safe='')}")
            os.unlink(out)
            raise CommandError(
                f"could not write {out} ({exc}); code {minted['code_id']} was revoked"
            ) from exc
    return {key: value for key, value in minted.items() if key != "url"} | {"out": out}


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
        *(usage, agents, work, queue, outcomes, assign, auto, manual, wait, wrap),
        *(mcp_mint, mcp_list, mcp_revoke),
    )
}
