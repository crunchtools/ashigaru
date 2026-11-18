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
from typing import Any

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


def _cli_args(command: str) -> set[str]:
    dests = {a.dest for a in CLI[command]._actions if a.dest not in ("help", "json")}
    # `--clear F` is a null for F in the tool's fields.
    return {name for dest in dests - {"clear"} for name in RENAMED.get(dest, {dest})}


def _mcp_args(tool: str) -> tuple[set[str], set[str]]:
    """The tool's own arguments, and the fields any of them carries as a model."""
    params = set(TOOLS[tool].parameters.get("properties", {}))
    carried = {"fields", "filters", "record"} & params
    fields: set[str] = set()
    for name in carried:
        fields |= set(MODELS[tool if name == "fields" else name].model_fields)
    return params - carried, fields


def test_every_cli_command_is_a_tool_and_every_tool_a_cli_command() -> None:
    assert sorted(TOOLS) == sorted(f"desk_{command}" for command in SHARED)


@pytest.mark.parametrize("command", SHARED)
def test_each_tool_takes_its_commands_arguments(command: str) -> None:
    flat, carried = _mcp_args(f"desk_{command}")
    cli = _cli_args(command)
    if carried:
        assert flat <= cli and cli - flat <= carried, f"desk_{command}: {sorted(cli - flat - carried)}"
    else:
        assert flat == cli, f"desk_{command}: CLI only {sorted(cli - flat)}, MCP only {sorted(flat - cli)}"


@pytest.mark.parametrize("command", SHARED)
def test_each_tool_says_which_command_it_is(command: str) -> None:
    description: Any = TOOLS[f"desk_{command}"].description
    assert f"`ashigaru {command}`" in description


@pytest.mark.parametrize("text", [SKILL, CLI_DOC], ids=["skill", "desk-cli.md"])
def test_every_tool_the_docs_name_exists(text: str) -> None:
    named = set(re.findall(r"\bdesk_[a-z]+\b", text))
    assert named and named <= set(TOOLS), sorted(named - set(TOOLS))


def test_the_skill_and_the_cli_doc_name_the_cli_only_commands() -> None:
    for text in (SKILL, CLI_DOC):
        assert "desk_<command>" in text
        assert all(f"`{command}`" in text for command in CLI_ONLY)
