"""The command set the CLI, the /ashigaru skill and the MCP tools share: the same names and the same
arguments. Each returns plain data; rendering is the caller's."""

import os
from collections.abc import Callable, Sequence
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from .client import Desk, DeskError, Send, Unreachable
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


def _match(if_version: int | None) -> dict[str, str]:
    """The If-Match header for a write that must find the item at `if_version`, else none. The Desk
    answers 412 (code "version") when the item has moved since."""
    return {} if if_version is None else {"If-Match": str(if_version)}


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


def status(desk: Desk) -> dict[str, Any]:
    """The board a person reads: each busy seat with its work in full, the free and missing seats, how
    many are up and how many items open, and NEEDS YOU. The MCP tool is desk_status."""
    result: dict[str, Any] = desk.get("/v1/status")
    return result


def changes(desk: Desk, since: str | None = None, ack: bool = False) -> dict[str, Any]:
    """Each item someone else changed since you last acked (else the last day, or from `since`): the
    fields that moved, who moved them and its NEXT now. With `ack`, your own seen mark then moves to
    the look's `until`, never from `since`, which could pass events you never read. The MCP tool is
    desk_changes."""
    if since and ack:
        raise CommandError("--since cannot go with --ack")
    if ack:
        acked: dict[str, Any] = desk.request("POST", "/v1/changes", body={})
        return acked
    result: dict[str, Any] = desk.get("/v1/changes", **({"since": since} if since else {}))
    return result


def work(desk: Desk, since: str | None = None, **flags: bool) -> dict[str, Any]:
    """The whole check round: open items blocked first then newest, the seats with how many are up
    and how many items are open, the queue, and the forge catch-up from `since` (default the sweep
    mark). `flags` are `all`, which adds closed and cleared items, and `advance` (admin), which then
    moves the mark to the round's `until`, never from `since`, which could pass events it never read.
    The MCP tool is desk_work."""
    if since and flags.get("advance"):
        raise CommandError("--since cannot go with --advance")
    if not since and not flags.get("all") and not flags.get("advance"):
        return _seat_board(desk)
    query = {"since": since} if since else {}
    if flags.get("all"):
        query["all"] = "true"
    if flags.get("advance"):
        moved: dict[str, Any] = desk.request("POST", "/v1/work", params=query, body={})
        return moved
    result: dict[str, Any] = desk.get("/v1/work", **query)
    return result


def _seat_board(desk: Desk) -> dict[str, Any]:
    """Bare `work`, what Scott reads, in one call: each busy seat's work, the free seats, NEEDS YOU and
    what changed since he looked; seen() moves his mark once it is shown. The full round is
    `work --advance`, `--all` or `--since`."""
    board: dict[str, Any] = desk.get("/v1/status")
    for row in board["busy"]:
        row["id"] = row["work"]
    for need in board["needs_you"]:
        need["id"] = need.get("work")
        need["step"] = need.get("text") or need.get("step") or need["why"]
    return {**board, "board": True}


def seen(desk: Desk, board: dict[str, Any], shown: int) -> None:
    """After the board is printed, move the reader's seen mark to the end of the window it showed,
    and only when it showed all of it: a list cut at `shown` stays unseen, so the next look has it."""
    found = board.get("changes") or {}
    if found.get("items") and len(found["items"]) <= shown and not found.get("truncated"):
        desk.request("POST", "/v1/changes", body={"until": found["until"]})


def queue(desk: Desk) -> dict[str, Any]:
    """Queued items with the state of each item they wait on (`waits_on`), and agent-filed `proposed`
    children with their expiry."""
    result: dict[str, Any] = desk.get("/v1/queue")
    return result


def sweep(desk: Desk, run: bool = False) -> dict[str, Any]:
    """Whether the Desk's sweep runs, how often, if a run is under way, and the last run's counts and
    error; with `run` (admin), start one now. It returns at once either way. The MCP tool is
    desk_sweep."""
    result: dict[str, Any] = desk.request("POST", "/v1/sweep", body={}) if run else desk.get("/v1/sweep")
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
    """One item: by id, by subject_key (unique), or the most recently updated with this ref. The MCP
    tool is desk_get, and desk_list finds one by subject_key or ref."""
    if sum(x is not None for x in (item, subject_key, ref)) != 1:
        raise CommandError("name one of: an item, --subject-key, --ref")
    if item is not None:
        one: dict[str, Any] = desk.get(_item(item))
        return one
    found: list[dict[str, Any]] = desk.get("/v1/items", subject_key=subject_key, ref=ref, limit=1)
    if not found:
        raise CommandError(f"no item with {'subject_key' if subject_key else 'ref'} {subject_key or ref}")
    return found[0]


def mark(desk: Desk, name: str, value: str | None = None) -> dict[str, Any]:
    """A named watermark, such as `sweep`; with a `value` (admin), set it to that. The MCP tool is
    desk_mark."""
    path = f"/v1/marks/{quote(name, safe='')}"
    result: dict[str, Any] = (
        desk.get(path) if value is None else desk.request("PUT", path, body={"value": value})
    )
    return result


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
        # A create's only conflicts, in the order the Desk checks them: the id is taken, else the
        # subject_key is.
        found = []
        if body.get("id"):
            try:
                found = [desk.get(_item(body["id"]))]
            except DeskError as missing:
                if missing.status != HTTPStatus.NOT_FOUND:
                    raise
        if not found and body.get("subject_key"):
            found = desk.get("/v1/items", subject_key=body["subject_key"], limit=1)
        if not found:
            raise
        raise Refused(f"{found[0]['id']} exists: {exc.detail}", found[0]) from exc
    return created


def update(
    desk: Desk, item: str, clear: Sequence[str] = (), *, if_version: int | None = None, **fields: Any
) -> dict[str, Any]:
    """Set the fields given; each name in `clear` is set to null; with `if_version`, only if the item
    is still at that version (else the Desk answers 412). A seat may set only state
    (working, review, blocked), result, next, learned, note, blocked_reason, memorized and
    check_after on its own open item. The MCP tool is desk_update."""
    body = {key: value for key, value in fields.items() if value is not None}
    body |= dict.fromkeys(clear)
    if not body:
        raise CommandError("nothing to update; give a field, or --clear one")
    result: dict[str, Any] = desk.request(
        "PATCH", _item(item), body=body, send=Send(headers=_match(if_version))
    )
    return result


def report(
    desk: Desk, item: str, verb: str, if_version: int | None = None, **lines: str | None
) -> dict[str, Any]:
    """The report contract, with `text`, `next` and `learned` in `lines`: DONE moves to review with
    the text as the result, BLOCKED to blocked with it as the reason, WORKING to working with it as
    the note; CC-DONE marks the learnings memorized. A `learned` of none is not recorded. With
    `if_version`, only if the item is still at that version. The MCP tool is desk_report."""
    body = {"verb": verb.upper(), **{k: v for k, v in lines.items() if v is not None}}
    result: dict[str, Any] = desk.post(f"{_item(item)}/report", body, _match(if_version))
    return result


def assign(
    desk: Desk,
    item: str | None = None,
    *,
    seat: str | None = None,
    lane: str | None = None,
    if_version: int | None = None,
    **record: str | list[str] | None,
) -> dict[str, Any]:
    """Assign an item to a seat: `seat` names one, else the Desk picks the seat in `lane` (default:
    the item's) free longest and not held by Scott. With a `task` in `record` (and optionally
    done_when, ref, repo, after, mode, subject_key), the item is recorded first, in `lane`; if the
    subject_key is taken, or the assign is refused, Refused carries the item so its id is not lost.
    An `outcome` (or `outcome_text`) given without a task, even empty, is set on the item before
    it is assigned. `if_version` guards the assign itself, so it needs an existing item."""
    if seat and lane:
        raise CommandError("name a seat or a lane, not both")
    if if_version is not None and record.get("task"):
        raise CommandError("--if-version is for an existing item, not a new one")
    if record.get("task"):
        item = create(desk, id=item, lane=lane, **record)["id"]
    elif not item:
        raise CommandError("name an item, or give --task to record a new one")
    elif serves := {key: record[key] for key in ("outcome", "outcome_text") if record.get(key) is not None}:
        desk.request("PATCH", _item(item), body=serves)
    body = {"seat": seat} if seat else {"lane": lane} if lane else {}
    try:
        result: dict[str, Any] = desk.post(f"{_item(item)}/assign", body, _match(if_version))
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


def wait(
    desk: Desk, target: Sequence[str], timeout: float | None = None, branch: str | None = None
) -> dict[str, Any]:
    """`[item]`: block until the item settles (review, done, blocked, wontdo, abandoned); returns it.
    `[repo, sha]`: block until the commit's CI is no longer pending, which includes every check
    `branch` (default the repo's) requires having a conclusion; returns its CI result. The MCP tool
    for a commit is desk_wait."""
    item_or_repo, *sha = target
    if len(sha) > 1:
        raise CommandError("wait takes an item, or a repo and a sha")
    if not sha:
        if branch is not None:
            raise CommandError("--branch is for a commit: wait <repo> <sha> --branch <branch>")
        return waiter_for(desk).wait(item_or_repo, timeout)
    return CIWaiter(desk).wait(item_or_repo, sha[0], timeout, branch)


def wrap(
    desk: Desk, item: str, if_version: int | None = None, **options: str | bool | None
) -> dict[str, Any]:
    """`options` are stage, learned, state and force; `if_version` makes it refuse (412) an item that
    has moved on. Stage `wrapping` records that /cc seat was sent.
    `cleared`, the default, refuses until CC-DONE (or a `learned`) and an idle turn, unless `force`;
    then clears the wrap and closes the item in `state` (default done), which frees the seat. The
    Desk decides, so the CLI and the MCP tool mean the same thing."""
    body = {k: v for k, v in options.items() if v is not None}
    result: dict[str, Any] = desk.post(f"{_item(item)}/wrap", body, _match(if_version))
    return result


def claim(desk: Desk, item: str, if_version: int | None = None) -> dict[str, Any]:
    """A seat takes a queued auto item in its lane whose after items are done, with `if_version` only
    if the item is still at that version. The MCP tool is desk_claim."""
    result: dict[str, Any] = desk.post(f"{_item(item)}/claim", {}, _match(if_version))
    return result


def file(
    desk: Desk, item: str, task: str, if_version: int | None = None, **lines: str | None
) -> dict[str, Any]:
    """File one proposed child of this story, under its outcome; one per assignment. `lines` are
    done_when and note; with `if_version`, only if the story is still at that version. The MCP tool is
    desk_file."""
    body = {"task": task, **{k: v for k, v in lines.items() if v is not None}}
    result: dict[str, Any] = desk.post(f"{_item(item)}/file", body, _match(if_version))
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
        *(usage, agents, status, changes, work, queue, sweep, outcomes, get, mark),
        *(create, update, report, assign, auto, manual, wait, wrap, claim, file),
        *(mcp_mint, mcp_list, mcp_revoke),
    )
} | {"list": items}
