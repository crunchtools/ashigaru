"""ashigaru: the Desk from a terminal, a script or a Claude Code hook.

Exit 0 on success, 1 when the Desk or a command refuses (or `wait` ends on
blocked, wontdo or abandoned, or times out), 2 on a usage or config error.
"""

import argparse
import json
import sys
from typing import Any

from . import commands, hooks, render
from .client import Desk, DeskError, Unreachable
from .config import ConfigError, load
from .wait import SUCCEEDED


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

    for name, text in (("auto", "hand an item to Josui"), ("manual", "hand an item to Scott")):
        sub.add_parser(name, parents=[common], help=text).add_argument("item")

    wait = sub.add_parser("wait", parents=[common], help="block until an item reaches review or closes")
    wait.add_argument("item")
    wait.add_argument("--timeout", type=float, help="give up after this many seconds")

    wrap = sub.add_parser("wrap", parents=[common], help="confirm idle and CC-DONE, clear, close the item")
    wrap.add_argument("item")
    wrap.add_argument("--learned", help="the report's Learned: line")
    wrap.add_argument("--state", choices=["done", "wontdo", "abandoned"], default="done")
    wrap.add_argument("--force", action="store_true", help="skip the idle and CC-DONE checks")

    hook = sub.add_parser("hook", help="Claude Code hooks: report this seat's turn and task; always exit 0")
    hook.add_argument("event", choices=hooks.EVENTS)
    return top


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    return {k: v for k, v in vars(args).items() if k not in ("command", "json")}


def execute(args: argparse.Namespace, desk: Desk) -> int:
    result = commands.COMMANDS[args.command](desk, **_arguments(args))
    print(json.dumps(result, indent=2) if args.json else render.view(args.command, result))
    if args.command == "wait" and result["state"] not in SUCCEEDED:
        return 1
    return 0


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
