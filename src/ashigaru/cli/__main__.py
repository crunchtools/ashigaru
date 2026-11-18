"""ashigaru: the Desk from a terminal, a script or a Claude Code hook.

Exit 0 on success, 1 when the Desk or a command refuses (or `wait` ends on
blocked, wontdo or abandoned, or a failed CI, or times out), 2 on a usage or
config error. A refusal that leaves an item a caller needs (a taken subject_key,
an item recorded but not assigned) still prints that item.
"""

import argparse
import json
import sys
from typing import Any

from . import commands, hooks, render
from .client import Desk, DeskError, Unreachable
from .config import ConfigError, load
from .wait import CI_SUCCEEDED, SUCCEEDED

FIELDS = (
    ("--kind", "outcome or story"),
    ("--parent", "the outcome this story serves"),
    ("--subject-key", "dedup key, owner/repo#N or RT#N, as the Desk stores it"),
    ("--done-when", None),
    ("--ref", "the issue or RT URL"),
    ("--repo", None),
    ("--lane", None),
    ("--mode", "auto or manual"),
    ("--state", None),
    ("--priority", None),
    ("--note", None),
    ("--filed-by", "admin: the identity that filed it"),
)
"""Item fields `create` and `update` both take."""
UPDATE_ONLY = (
    ("--task", "the one-line objective"),
    ("--result", None),
    ("--next", None),
    ("--learned", "the Learned: line"),
    ("--blocked-reason", None),
    ("--check-after", "when to look again"),
    ("--verified-by", None),
)
CLEARABLE = (
    "kind", "parent", "subject_key", "done_when", "ref", "repo", "lane", "note", "filed_by", "result",
    "next", "learned", "blocked_reason", "check_after", "verified_by", "wrap",
)  # fmt: skip


def _fields(command: argparse.ArgumentParser, options: tuple[tuple[str, str | None], ...]) -> None:
    for flag, text in options:
        command.add_argument(flag, type=int if flag == "--priority" else str, help=text)


def _item_commands(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]", common: argparse.ArgumentParser
) -> None:
    """list, get, create, update and report: the MCP's desk_* item tools."""
    listing = sub.add_parser("list", parents=[common], help="items, newest first, by any filters")
    listing.add_argument("--state", action="append", help="any of these states (repeatable)")
    for flag in ("--seat", "--lane", "--kind", "--mode", "--parent", "--subject-key", "--ref"):
        listing.add_argument(flag)
    listing.add_argument(
        "--learned", action=argparse.BooleanOptionalAction, help="with (or without) a Learned line"
    )
    listing.add_argument("--memorized", action=argparse.BooleanOptionalAction)
    listing.add_argument("--limit", type=int)
    listing.add_argument("--offset", type=int)

    get = sub.add_parser("get", parents=[common], help="one item, by id, subject_key or ref")
    get.add_argument("item", nargs="?")
    key = get.add_mutually_exclusive_group()
    key.add_argument("--subject-key", help="the item with this subject_key")
    key.add_argument("--ref", help="the newest item with this ref")

    create = sub.add_parser(
        "create", parents=[common], help="admin: record an item without assigning it (queued by default)"
    )
    create.add_argument("--task", required=True, help="the one-line objective")
    create.add_argument("--id", help="default: the next a<N>")
    create.add_argument("--after", action="append", help="an item this one waits on (repeatable)")
    _fields(create, FIELDS)

    update = sub.add_parser("update", parents=[common], help="set an item's fields; --clear nulls one")
    update.add_argument("item")
    update.add_argument("--after", action="append", help="replace what it waits on (repeatable)")
    update.add_argument("--memorized", action=argparse.BooleanOptionalAction)
    update.add_argument("--clear", action="append", default=[], choices=CLEARABLE, metavar="FIELD")
    _fields(update, FIELDS + UPDATE_ONLY)

    report = sub.add_parser("report", parents=[common], help="DONE, BLOCKED, WORKING or CC-DONE on an item")
    report.add_argument("item")
    report.add_argument("verb", type=str.upper, choices=["DONE", "BLOCKED", "WORKING", "CC-DONE"])
    report.add_argument("--text", help="the result, the blocker, or the progress note")
    report.add_argument("--next")
    report.add_argument("--learned", help="the Learned: line; none records nothing")


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the Desk's JSON instead of a table")
    top = argparse.ArgumentParser(
        prog="ashigaru",
        description="The Desk's client. The URL and key come from the environment "
        "(ASHIGARU_URL, ASHIGARU_API_KEY or ASHIGARU_API_KEY_FILE) or ~/.config/ashigaru/config.toml.",
    )
    sub = top.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("usage", parents=[common], help="context, tokens, turn and spend per seat")
    sub.add_parser("agents", parents=[common], help="each seat: free or busy, its item, how long")
    sub.add_parser("work", parents=[common], help="the check table: blocked first, then newest").add_argument(
        "--all", action="store_true", help="include cleared, wontdo and abandoned items"
    )
    sub.add_parser("queue", parents=[common], help="queued items, what they wait on, proposed children")
    sub.add_parser("outcomes", parents=[common], help="each outcome's stories: done, blocked, open")

    _item_commands(sub, common)

    assign = sub.add_parser("assign", parents=[common], help="assign an item to a seat, by name or lane")
    assign.add_argument("item", nargs="?", help="the item; with --task, the new item's id (default next)")
    where = assign.add_mutually_exclusive_group()
    where.add_argument("--seat", help="this seat")
    where.add_argument("--lane", help="the seat in this lane free longest (default: the item's lane)")
    assign.add_argument("--task", help="record a new item with this one-line objective first")
    assign.add_argument("--done-when", dest="done_when")
    assign.add_argument("--ref", help="the issue or RT URL")
    assign.add_argument("--repo")
    assign.add_argument("--after", action="append", help="an item this one waits on (repeatable)")
    assign.add_argument("--mode", choices=["auto", "manual"])
    assign.add_argument("--subject-key", dest="subject_key", help="with --task: the new item's dedup key")

    for name, text in (("auto", "hand an item to Josui"), ("manual", "hand an item to Scott")):
        sub.add_parser(name, parents=[common], help=text).add_argument("item")

    wait = sub.add_parser(
        "wait", parents=[common], help="block until an item reaches review or closes, or a commit's CI ends"
    )
    wait.add_argument("target", nargs="+", metavar="item | repo sha")
    wait.add_argument("--timeout", type=float, help="give up after this many seconds")

    wrap = sub.add_parser(
        "wrap", parents=[common], help="mark wrapping, or confirm idle and CC-DONE, clear, close the item"
    )
    wrap.add_argument("item")
    wrap.add_argument(
        "--stage", choices=["wrapping", "cleared"], default="cleared", help="wrapping: /cc seat is sent"
    )
    wrap.add_argument("--learned", help="the report's Learned: line")
    wrap.add_argument("--state", choices=["done", "wontdo", "abandoned"], default="done")
    wrap.add_argument("--force", action="store_true", help="skip the idle and CC-DONE checks")

    mcp = sub.add_parser("mcp", help="hosted MCP URLs: mint, list, revoke")
    action = mcp.add_subparsers(dest="action", required=True, metavar="action")
    mint = action.add_parser(
        "mint", parents=[common], help="admin: a new MCP URL for an identity, shown once"
    )
    mint.add_argument("identity")
    mint.add_argument("--label", help="what the URL is for")
    mint.add_argument("--out", help="write the URL to this new file (mode 0600) instead of printing it")
    listing = action.add_parser("list", parents=[common], help="MCP codes: id, identity, created, last used")
    listing.add_argument(
        "identity", nargs="?", help="this identity's (default: all for an admin, else yours)"
    )
    revoke = action.add_parser("revoke", parents=[common], help="revoke an MCP code by its id")
    revoke.add_argument("code_id", metavar="id")
    revoke.add_argument("--identity", help="the code's identity, if the key sees several")

    hook = sub.add_parser("hook", help="Claude Code hooks: report this seat's turn and task; always exit 0")
    hook.add_argument("event", choices=hooks.EVENTS)
    return top


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    return {k: v for k, v in vars(args).items() if k not in ("command", "action", "json")}


def _command(args: argparse.Namespace) -> str:
    """`mcp mint` is commands.mcp_mint; every other command is its own name."""
    return f"mcp_{args.action}" if args.command == "mcp" else str(args.command)


def _show(args: argparse.Namespace, command: str, result: Any) -> None:
    print(json.dumps(result, indent=2) if args.json else render.view(command, result))


def execute(args: argparse.Namespace, desk: Desk) -> int:
    command = _command(args)
    try:
        result = commands.COMMANDS[command](desk, **_arguments(args))
    except commands.Refused as exc:
        _show(args, "get", exc.item)
        raise
    _show(args, command, result)
    if args.command == "wait" and not succeeded(result):
        return 1
    return 0


def succeeded(waited: dict[str, Any]) -> bool:
    """An item in review or done, or a commit whose CI passed."""
    return waited["status"] == CI_SUCCEEDED if "status" in waited else waited["state"] in SUCCEEDED


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "hook":
        return hooks.run(args.event, sys.stdin)
    try:
        desk = Desk(load())
    except ConfigError as exc:
        print(f"ashigaru: {exc}", file=sys.stderr)
        return 2
    try:
        return execute(args, desk)
    except (DeskError, Unreachable, commands.CommandError, TimeoutError) as exc:
        print(f"ashigaru: {exc}", file=sys.stderr)
        return 1
    finally:
        desk.close()


if __name__ == "__main__":
    sys.exit(main())
