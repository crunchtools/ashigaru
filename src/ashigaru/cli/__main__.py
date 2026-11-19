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

from .. import __version__
from . import commands, hooks, render
from .client import Desk, DeskError, Unreachable
from .config import ConfigError, load
from .wait import CI_SUCCEEDED, SUCCEEDED

CHOICES = {
    "--kind": ("outcome", "story"),
    "--lane": ("vertex", "dolza-max", "exedore-max", "prime", "rule"),
    "--mode": ("auto", "manual"),
    "--state": (
        "proposed", "queued", "assigned", "working", "review", "done", "blocked", "wontdo", "abandoned",
    ),
}  # fmt: skip
"""The Desk's enums, spelled out so the CLI stays stdlib-only; the drift test in test_skill.py keeps
them in step with ashigaru.desk.model."""
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
    ("--note", None),
    ("--filed-by", "admin: the identity that filed it"),
    ("--pr", "the PR carrying the work, owner/repo#N or its URL"),
    *(
        OUTCOME := (
            (
                "--outcome",
                "the outcome it serves: its id, RT#N or issue, or with --outcome-text a short name",
            ),
            ("--outcome-text", "Scott's instruction with no ticket, kept on a new one-off outcome"),
        )
    ),
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
    "next", "learned", "blocked_reason", "check_after", "verified_by", "wrap", "after", "pr",
)  # fmt: skip


def _fields(command: argparse.ArgumentParser, options: tuple[tuple[str, str | None], ...]) -> None:
    for flag, text in options:
        command.add_argument(flag, help=text, choices=CHOICES.get(flag))


VIEWS = (
    ("usage", "context, tokens, turn and spend per seat", ()),
    ("agents", "each seat: free or busy, its work, how long", ()),
    ("status", "the board: busy seats and their work, free seats, NEEDS YOU", ()),
    (
        "changes",
        "what others changed since you last acked: each item once, with its NEXT",
        (
            ("--since", "look from here, a time (default: your seen mark)"),
            ("--ack", "then move your seen mark to this look's end"),
        ),
    ),
    (
        "work",
        "the check round: open items, seats, queue and GitHub catch-up",
        (
            ("--wide", "every column: time, PR, done-when and check time too"),
            ("--all", "include closed and cleared items"),
            ("--since", "start the catch-up here, a time (default: the sweep mark)"),
            ("--advance", "admin: then move the sweep mark to the round's end"),
        ),
    ),
    ("queue", "queued items, what they wait on, proposed children", ()),
    (
        "sweep",
        "the timer that reads GitHub back: its last run; --run starts one",
        (("--run", "admin: start a run now and return at once"),),
    ),
    ("outcomes", "each outcome's stories: done, blocked, open", ()),
)
"""What the Desk shows, in the order the skill's headings follow, with each one's flags. Every flag is
a switch but --since, which takes a time."""


def _views(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]", common: argparse.ArgumentParser
) -> None:
    for name, text, flags in VIEWS:
        view = sub.add_parser(name, parents=[common], help=text)
        for flag, about in flags:
            view.add_argument(flag, help=about, action="store" if flag == "--since" else "store_true")


def _item_commands(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]", common: argparse.ArgumentParser
) -> None:
    """list, get, create, update, mark and report: the MCP's desk_* tools."""
    listing = sub.add_parser("list", parents=[common], help="items, newest first, by any filters")
    listing.add_argument(
        "--state", action="append", choices=CHOICES["--state"], help="any of these states (repeatable)"
    )
    for flag in ("--seat", "--lane", "--kind", "--mode", "--parent", "--subject-key", "--ref"):
        listing.add_argument(flag, choices=CHOICES.get(flag))
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
    key.add_argument("--ref", help="the most recently updated item with this ref")

    create = sub.add_parser(
        "create", parents=[common], help="admin: record an item without assigning it (queued by default)"
    )
    create.add_argument("--task", required=True, help="the one-line objective")
    create.add_argument("--id", help="default: the next a<N>")
    create.add_argument("--after", action="append", help="an item this one waits on (repeatable)")
    _fields(create, FIELDS)
    create.add_argument("--priority", type=int)

    update = sub.add_parser("update", parents=[common], help="set an item's fields; --clear nulls one")
    update.add_argument("item")
    update.add_argument("--after", action="append", help="replace what it waits on (repeatable)")
    update.add_argument("--memorized", action=argparse.BooleanOptionalAction)
    update.add_argument("--clear", action="append", default=[], choices=CLEARABLE, metavar="FIELD")
    _fields(update, FIELDS + UPDATE_ONLY)
    update.add_argument("--priority", type=int)

    mark = sub.add_parser("mark", parents=[common], help="a named watermark, e.g. sweep; --set to move it")
    mark.add_argument("name")
    mark.add_argument("--set", dest="value", metavar="VALUE", help="admin: set it to this, usually a time")

    report = sub.add_parser("report", parents=[common], help="DONE, BLOCKED, WORKING or CC-DONE on an item")
    report.add_argument("item")
    report.add_argument("verb", type=str.upper, choices=["DONE", "BLOCKED", "WORKING", "CC-DONE"])
    report.add_argument("--text", help="the result, the blocker, or the progress note")
    report.add_argument("--next")
    report.add_argument("--learned", help="the Learned: line; none records nothing")


def _seat_commands(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]", common: argparse.ArgumentParser
) -> None:
    """claim and file: the MCP's seat tools, desk_claim and desk_file."""
    claim = sub.add_parser(
        "claim",
        parents=[common],
        help="a seat takes a queued auto item in its lane whose after items are done",
    )
    claim.add_argument("item")

    file = sub.add_parser(
        "file",
        parents=[common],
        help="file one proposed child of this story, under its outcome; one per assignment",
    )
    file.add_argument("item")
    file.add_argument("--task", required=True, help="the child's one-line objective")
    file.add_argument("--done-when", dest="done_when")
    file.add_argument("--note")


def _wait_parser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]", common: argparse.ArgumentParser
) -> None:
    wait = sub.add_parser(
        "wait", parents=[common], help="block until an item reaches review or closes, or a commit's CI ends"
    )
    wait.add_argument("target", nargs="+", metavar="item | repo sha")
    wait.add_argument("--timeout", type=float, help="give up after this many seconds")
    wait.add_argument("--branch", help="the branch a commit targets, whose required checks it must pass")


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the Desk's JSON instead of a table")
    common.add_argument("--markdown", action="store_true", help="print tables as Markdown, for a chat")
    top = argparse.ArgumentParser(
        prog="ashigaru",
        description="The Desk's client. The URL and key come from the environment "
        "(ASHIGARU_URL, ASHIGARU_API_KEY or ASHIGARU_API_KEY_FILE) or ~/.config/ashigaru/config.toml.",
    )
    top.add_argument("--version", action="version", version=__version__)
    sub = top.add_subparsers(dest="command", required=True, metavar="command")
    _views(sub, common)

    _item_commands(sub, common)

    assign = sub.add_parser("assign", parents=[common], help="assign an item to a seat, by name or lane")
    assign.add_argument("item", nargs="?", help="the item; with --task, the new item's id (default next)")
    where = assign.add_mutually_exclusive_group()
    where.add_argument("--seat", help="this seat")
    where.add_argument(
        "--lane",
        choices=CHOICES["--lane"],
        help="the seat in this lane free longest (default: the item's lane)",
    )
    assign.add_argument("--task", help="record a new item with this one-line objective first")
    assign.add_argument("--done-when", dest="done_when")
    assign.add_argument("--ref", help="the issue or RT URL")
    assign.add_argument("--repo")
    assign.add_argument("--after", action="append", help="an item this one waits on (repeatable)")
    assign.add_argument("--mode", choices=CHOICES["--mode"])
    assign.add_argument("--subject-key", dest="subject_key", help="with --task: the new item's dedup key")
    _fields(assign, OUTCOME)

    for name, text in (("auto", "hand an item to Josui"), ("manual", "hand an item to Scott")):
        sub.add_parser(name, parents=[common], help=text).add_argument("item")

    _wait_parser(sub, common)

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

    _seat_commands(sub, common)

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

    hook = sub.add_parser(
        "hook",
        help="Claude Code hooks and status line: report this seat's turn, task and usage; exit 0, next 2",
    )
    hook.add_argument("event", choices=hooks.EVENTS)
    return top


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    return {k: v for k, v in vars(args).items() if k not in ("command", "action", "json", "markdown", "wide")}


def _command(args: argparse.Namespace) -> str:
    """`mcp mint` is commands.mcp_mint; every other command is its own name."""
    return f"mcp_{args.action}" if args.command == "mcp" else str(args.command)


def _show(args: argparse.Namespace, command: str, result: Any) -> None:
    render.MARKDOWN = getattr(args, "markdown", False)
    render.WIDE_VIEW = getattr(args, "wide", False)
    print(json.dumps(result, indent=2) if args.json else render.view(command, result))


def execute(args: argparse.Namespace, desk: Desk) -> int:
    command = _command(args)
    try:
        result = commands.COMMANDS[command](desk, **_arguments(args))
    except commands.Refused as exc:
        _show(args, "get", exc.item)
        raise
    _show(args, command, result)
    if command == "work" and isinstance(result, dict) and result.get("board"):
        commands.seen(desk, result, render.CHANGED_SHOWN)
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
