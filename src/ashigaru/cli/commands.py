"""The command set the CLI, the /ashigaru skill and the MCP tools share: the same names and the same
arguments. Each returns plain data; rendering is the caller's."""

from collections.abc import Callable
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from .client import Desk, DeskError
from .wait import waiter_for

CLOSED = frozenset({"done", "wontdo", "abandoned"})


class CommandError(Exception):
    """A command refused on the client's side, before anything changed."""


def _item(item_id: str) -> str:
    return f"/v1/items/{quote(item_id, safe='')}"


def _seat(name: str) -> str:
    return f"/v1/seats/{quote(name, safe='')}"


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


COMMANDS: dict[str, Callable[..., Any]] = {
    command.__name__: command
    for command in (usage, agents, work, queue, outcomes, assign, auto, manual, wait, wrap)
}
