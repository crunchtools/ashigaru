"""The /ashigaru skill's commands are exactly the CLI's, in both directions.

Scott's rule: no skill-only or renamed verbs, and nothing the CLI has that the
skill leaves out. The skill names its commands in four places (the backticked
`## ` headings, the argument-hint, the description and the mcp actions); each
must equal the parser's set, and every `ashigaru ...` line in it must parse.
"""

import argparse
import re
import shlex
from pathlib import Path

import pytest
import yaml

from ashigaru.cli import hooks
from ashigaru.cli.__main__ import parser

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "skills" / "ashigaru" / "SKILL.md").read_text()
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
