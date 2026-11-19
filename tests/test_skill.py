"""The /ashigaru skill, the CLI and the Desk's MCP tools are one command set, in every direction.

Scott's rule: no skill-only or renamed verbs, and nothing the CLI has that the
skill leaves out. The skill names its commands in four places (the backticked
`## ` headings, the argument-hint, the description and the mcp actions); each
must equal the parser's set, and every `ashigaru ...` line in it must parse.
Each CLI command but `mcp` and `hook` is the MCP tool `desk_<command>`, taking
the same arguments, and no tool is anything else.
"""

import argparse
import asyncio
import re
import shlex
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml
from pydantic import BaseModel

from ashigaru.cli import hooks
from ashigaru.cli.__main__ import parser
from ashigaru.desk.mcp.server import mcp
from ashigaru.desk.model import ItemCreate, ItemFilter, ItemPatch, NewItem

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "skills" / "ashigaru" / "SKILL.md").read_text()
CLI_DOC = (ROOT / "docs" / "desk-cli.md").read_text()
API_DOC = (ROOT / "docs" / "desk-api.md").read_text()
_, FRONT, BODY = SKILL.split("---\n", 2)
META = yaml.safe_load(FRONT)


SUB = argparse._SubParsersAction
CLI = next(a for a in parser()._actions if isinstance(a, SUB)).choices
MCP = list(next(a for a in CLI["mcp"]._actions if isinstance(a, SUB)).choices)


def _top_level(hint: str) -> list[str]:
    """Split the argument-hint's alternatives on the `|`s outside any brackets."""
    inner = hint.strip()[1:-1]
    parts, depth, start = [], 0, 0
    for i, char in enumerate(inner):
        depth += (char in "[<") - (char in "]>")
        if char == "|" and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
    return [p.strip() for p in [*parts, inner[start:]]]


HINT = {part.split()[0]: part for part in _top_level(META["argument-hint"])}


def test_the_headings_are_the_cli_commands() -> None:
    assert re.findall(r"^## `([a-z-]+)`$", BODY, re.M) == list(CLI)


def test_the_argument_hint_names_every_command_and_no_other() -> None:
    assert list(HINT) == list(CLI)


def test_the_description_names_every_command_and_no_other() -> None:
    named = re.search(r"commands \(([^)]*)\)", META["description"])
    assert named and named.group(1).split(", ") == list(CLI)


def test_the_mcp_actions_are_the_cli_actions() -> None:
    hinted = re.search(r"mcp <([a-z|]+)>", HINT["mcp"])
    assert hinted and hinted.group(1).split("|") == MCP
    section = BODY.split("## `mcp`")[1].split("\n## ")[0]
    assert sorted(set(re.findall(r"ashigaru mcp ([a-z]+)", section))) == sorted(MCP)


def test_the_hook_section_names_every_event() -> None:
    section = BODY.split("## `hook`")[1].split("\n## ")[0]
    assert all(f"`{event}`" in section or f"hook {event}" in section for event in hooks.EVENTS)


def _invocations() -> list[str]:
    spans = re.findall(r"`(/?ashigaru [^`]+)`", BODY)
    fenced = re.findall(r"^\s*(ashigaru .+)$", BODY, re.M)
    return [line.split(" | ")[0] for line in spans + fenced]


@pytest.mark.parametrize("line", _invocations())
def test_every_command_line_in_the_skill_parses(line: str) -> None:
    args = shlex.split(line)
    assert args[0].lstrip("/") == "ashigaru"
    try:
        parser().parse_args(args[1:])
    except SystemExit as exc:
        pytest.fail(f"the CLI refuses {line!r} (exit {exc.code})")


def test_the_invocations_cover_every_command() -> None:
    used = {shlex.split(line)[1] for line in _invocations()}
    assert used == set(CLI)


# The MCP tools

CLI_ONLY = {
    "mcp": "a hosted MCP URL is a credential, minted only from a terminal",
    "hook": "runs inside a seat's Claude Code, never over MCP",
}
TOOLS = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
SHARED = [command for command in CLI if command not in CLI_ONLY]
MODELS: dict[str, type[BaseModel]] = {
    "filters": ItemFilter,
    "record": NewItem,
    "desk_create": ItemCreate,
    "desk_update": ItemPatch,
}
"""A tool argument that carries a model's fields, as the CLI's flags do one by one: by its name, or
for `fields`, by the tool's."""
RENAMED = {"item": {"item_id"}, "target": {"item_id", "repo", "sha"}}
"""The CLI's positional `item` is the tool's `item_id`; `wait`'s `item | repo sha` is all three."""


SEAT = "a seat's own report fields, set by report and update, not at birth"
MCP_ONLY = {
    "desk_create": {
        "seat": "recorded without assign's checks; assign gives an item to a seat",
        "wrap": "the wrap stage is wrap's, never set at creation",
        **dict.fromkeys(
            ("result", "next", "learned", "blocked_reason", "check_after", "memorized", "verified_by"), SEAT
        ),
    },
    "desk_update": {
        "seat": "an item is handed to a seat by assign, not patched in",
        "wrap": "the wrap stage moves only through wrap (`--clear wrap` is the CLI's way out)",
    },
}
"""Fields of a carried model the CLI deliberately has no flag for, by tool: a new one fails the test."""


TYPE_DIFFERS = {
    (
        "desk_wait",
        "timeout",
    ): "the CLI's is its whole wait, fractional seconds; the tool's one poll, whole, at most 90",
}
"""A tool argument deliberately not the CLI flag's type, with why."""
CHOICES_DIFFER: dict[tuple[str, str], str] = {}
"""A tool argument whose choices the CLI flag does not declare, with why."""
REQUIRED_DIFFERS = {
    ("desk_assign", "task"): "the record's task is for a new item; assign by `item` needs none",
}
"""An argument or carried field required on one side only, with why."""
OMITTED = "the CLI leaves it out when unset and the Desk fills it in; the tool states the Desk's value"
NULL_DIFFERS = {
    ("desk_list", "limit"): OMITTED,
    ("desk_list", "offset"): OMITTED,
    ("desk_assign", "task"): "the record's task is for a new item; assign by `item` needs none",
    ("desk_assign", "after"): "the CLI's unset is None; the record's is an empty list",
    (
        "desk_wait",
        "timeout",
    ): "the CLI's None goes to the waiter, which waits as long as it takes; the tool's is 90",
}
"""An argument `T | None` on one side and `T` on the other, with why."""
DEFAULT_DIFFERS = {
    ("desk_list", "limit"): OMITTED,
    ("desk_list", "offset"): OMITTED,
    ("desk_wait", "timeout"): "the CLI waits as long as it takes; the tool polls once, 90 seconds at most",
}
"""An argument whose default deliberately differs between the CLI and the tool, with why."""


class Arg(NamedTuple):
    """One argument as JSON schema sees it: its type, choices (if any), whether it may be null, whether
    it is required, and its default (None for "none")."""

    type: str
    choices: frozenset[str] | None
    nullable: bool
    required: bool
    default: Any


_TYPES: dict[object, str] = {int: "integer", float: "number"}


def _cli_args(command: str) -> dict[str, Arg]:
    """Each CLI argument, by the MCP name; `--clear F` is a null for F in the tool's fields."""
    found = {}
    for a in CLI[command]._actions:
        if a.dest in ("help", "json", "markdown", "wide", "clear"):
            continue
        scalar = _TYPES.get(a.type, "string")
        if isinstance(
            a, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse.BooleanOptionalAction)
        ):
            kind = "boolean"
        elif isinstance(a, argparse._AppendAction) or a.nargs in ("+", "*"):
            kind = f"array<{scalar}>"
        else:
            kind = scalar
        required = a.required if a.option_strings else a.nargs not in ("?", "*")
        nullable = not required and a.default is None
        arg = Arg(kind, frozenset(a.choices) if a.choices else None, nullable, required, a.default)
        if a.dest == "target":  # item | repo sha: each name is one string, none required alone
            arg = Arg("string", None, True, False, None)
        for name in RENAMED.get(a.dest, {a.dest}):
            found[name] = arg
    return found


def _type(schema: dict[str, Any], defs: dict[str, Any]) -> tuple[str, frozenset[str] | None, bool]:
    """A property's non-null type, enum and nullability as `_cli_args` names them: $ref, anyOf resolved."""
    if "$ref" in schema:
        return _type(defs[schema["$ref"].rsplit("/", 1)[1]], defs)
    if "anyOf" in schema:
        branches = [branch for branch in schema["anyOf"] if branch.get("type") != "null"]
        kinds = {_type(branch, defs)[:2] for branch in branches}
        assert len(kinds) == 1, schema
        return *kinds.pop(), len(branches) < len(schema["anyOf"])
    if "enum" in schema:
        return "string", frozenset(map(str, schema["enum"])), False
    if schema["type"] == "array":
        kind, choices, _ = _type(schema["items"], defs)
        return f"array<{kind}>", choices, False
    return schema["type"], None, False


def _mcp_args(tool: str) -> tuple[dict[str, Arg], dict[str, Arg]]:
    """The tool's own arguments, and the fields any of them carries as a model."""
    schema = TOOLS[tool].parameters
    props, required, defs = (
        schema.get("properties", {}),
        set(schema.get("required", [])),
        schema.get("$defs", {}),
    )
    carried = {"fields", "filters", "record"} & set(props)
    flat = {
        name: Arg(*_type(prop, defs), name in required, prop.get("default"))
        for name, prop in props.items()
        if name not in carried
    }
    fields = {}
    for name in carried:
        model = MODELS[tool if name == "fields" else name].model_json_schema()
        for field, prop in model["properties"].items():
            required_here = field in model.get("required", [])
            fields[field] = Arg(*_type(prop, model.get("$defs", {})), required_here, prop.get("default"))
    return flat, fields


def test_every_cli_command_is_a_tool_and_every_tool_a_cli_command() -> None:
    assert sorted(TOOLS) == sorted(f"desk_{command}" for command in SHARED)


def _lower(choices: frozenset[str] | None) -> frozenset[str] | None:
    return choices and frozenset(c.lower() for c in choices)


@pytest.mark.parametrize("command", SHARED)
def test_each_tool_takes_its_commands_arguments(command: str) -> None:
    """Names, types, choices, required-ness, nullability and defaults agree, bar the listed differences; a
    carried model's fields are the CLI's flags and the tool's listed gaps."""
    flat, carried = _mcp_args(f"desk_{command}")
    cli = _cli_args(command)
    gaps = MCP_ONLY.get(f"desk_{command}", {})
    assert set(flat) <= set(cli), f"MCP only: {sorted(set(flat) - set(cli))}"
    assert set(cli) - set(flat) == set(carried) - set(gaps) if carried else set(cli) == set(flat), (
        f"CLI only {sorted(set(cli) - set(flat) - set(carried))}, "
        f"MCP only {sorted(set(carried) - set(cli) - set(gaps))}"
    )
    assert not set(gaps) & set(cli), f"{sorted(set(gaps) & set(cli))} is on the CLI after all"
    for name, tool in (flat | carried).items():
        if name not in cli:
            continue
        flag = cli[name]
        if (f"desk_{command}", name) not in TYPE_DIFFERS:
            assert tool.type == flag.type, f"{name}: MCP {tool.type}, CLI {flag.type}"
        key = (f"desk_{command}", name)
        if key not in CHOICES_DIFFER:
            assert _lower(tool.choices) == _lower(flag.choices), (
                f"{name}: choices MCP {_lower(tool.choices)}, CLI {_lower(flag.choices)}"
            )
        if key not in REQUIRED_DIFFERS:
            assert tool.required == flag.required, (
                f"{name}: required MCP {tool.required}, CLI {flag.required}"
            )
        if key not in NULL_DIFFERS:
            assert tool.nullable == flag.nullable, f"{name}: null MCP {tool.nullable}, CLI {flag.nullable}"
        if key not in DEFAULT_DIFFERS:
            assert tool.default == flag.default, f"{name}: default MCP {tool.default!r}, CLI {flag.default!r}"


@pytest.mark.parametrize(
    ("listed", "field"),
    [(CHOICES_DIFFER, "choices"), (REQUIRED_DIFFERS, "required"), (NULL_DIFFERS, "nullable"),
     (DEFAULT_DIFFERS, "default")],
)  # fmt: skip
def test_every_listed_difference_still_differs(listed: dict[tuple[str, str], str], field: str) -> None:
    """An entry whose difference was fixed is stale: it would hide the next drift."""
    for tool, name in listed:
        flat, carried = _mcp_args(tool)
        mcp, cli = (flat | carried)[name], _cli_args(tool.removeprefix("desk_"))[name]
        assert getattr(mcp, field) != getattr(cli, field), f"{tool} {name}: {field} agrees now"


def test_every_listed_gap_is_still_a_gap() -> None:
    """A stale TYPE_DIFFERS, MCP_ONLY, RENAMED or CLI_ONLY entry would hide the next drift."""
    for tool, name in TYPE_DIFFERS:
        flat, carried = _mcp_args(tool)
        mcp, cli = (flat | carried)[name], _cli_args(tool.removeprefix("desk_"))[name]
        assert mcp.type != cli.type, f"{tool} {name}: type agrees now"
    for tool, gaps in MCP_ONLY.items():
        flags = _cli_args(tool.removeprefix("desk_"))
        _, carried = _mcp_args(tool)
        assert set(gaps) <= set(carried), f"{tool}: {sorted(set(gaps) - set(carried))} is not a field"
        assert not set(gaps) & set(flags), f"{tool}: {sorted(set(gaps) & set(flags))} is on the CLI"
    for command in CLI_ONLY:
        assert f"desk_{command}" not in TOOLS, f"{command} is a tool now"
    for dest, names in RENAMED.items():
        used = {a.dest for command in SHARED for a in CLI[command]._actions}
        assert dest in used, f"no CLI argument is named {dest}"
        flats = set().union(*(_mcp_args(f"desk_{command}")[0] for command in SHARED))
        assert names <= flats, f"{dest}: no tool argument {sorted(names - flats)}"


@pytest.mark.parametrize("command", SHARED)
def test_each_tool_says_which_command_it_is(command: str) -> None:
    description: Any = TOOLS[f"desk_{command}"].description
    assert f"`ashigaru {command}`" in description


@pytest.mark.parametrize("text", [SKILL, CLI_DOC, API_DOC], ids=["skill", "desk-cli.md", "desk-api.md"])
def test_every_tool_the_docs_name_exists(text: str) -> None:
    named = set(re.findall(r"\bdesk_[a-z0-9_]+\b", text))
    assert named and named <= set(TOOLS), sorted(named - set(TOOLS))


def test_the_skill_and_the_cli_doc_name_the_cli_only_commands() -> None:
    for text in (SKILL, CLI_DOC):
        assert "desk_<command>" in text
        assert all(f"`{command}`" in text for command in CLI_ONLY)
