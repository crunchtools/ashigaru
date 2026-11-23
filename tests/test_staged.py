"""What a publish refuses: judged on what `git apply --index` staged, never on the patch text."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fix_publish
import publish
from common import staged_refusal

PYPROJECT = """\
[project]
name = "x"
version = "1.0.0"
dependencies = [
    "a",
]

[tool.sdlc.push]
# a comment
skip = ["semgrep"]

[tool.ruff]
line-length = 110
"""
SDLC_BLOCK = '[tool.sdlc.push]\n# a comment\nskip = ["semgrep"]\n\n'
LINK = object()
"""A file value that makes the path a symlink: `{path: (LINK, target)}`."""


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          check=True, capture_output=True, text=True)  # fmt: skip
    return done.stdout


def write(root: Path, files: dict) -> None:
    for name, content in files.items():
        path = root / name
        if path.is_symlink() or path.exists():
            path.unlink()
        if content is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, tuple):
            path.symlink_to(content[1])
        else:
            path.write_text(content)


def refused(root: Path, monkeypatch, before: dict, after: dict, *config: str) -> str | None:
    """Commit `before`, make the agent's patch to `after`, apply it the way publish does, judge it.

    `config` is extra `-c` settings for the `git diff` that writes the patch, as an agent could set.
    """
    git(root, "init", "-q")
    write(root, {"pyproject.toml": PYPROJECT, **before})
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    write(root, after)
    git(root, "add", "-A")
    patch = git(root, *config, "diff", "--cached", "--binary", "HEAD")
    git(root, "reset", "-q", "--hard", "HEAD")
    (root.parent / "change.patch").write_text(patch)
    git(root, "apply", "--index", "--3way", str(root.parent / "change.patch"))
    monkeypatch.chdir(root)
    return staged_refusal()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "repo").mkdir()
    return tmp_path / "repo"


def test_an_ordinary_change_is_published(root, monkeypatch):
    assert (
        refused(root, monkeypatch, {"src/app.py": "a\n"}, {"src/app.py": "b\n", "docs/new.md": "x\n"}) is None
    )


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        ".sdlc/bin/sdlc",
        ".semgrep/rules.yml",
        ".gourmand-exceptions.d/CH009.toml",
        "gatehouse.toml",
        "gatehouse-findings.toml",
        ".gatehouse-ignore",
        "gourmand.toml",
        "gourmand-exceptions.toml",
        "gourmand-patterns.toml",
        "gourmand-audit-ledger.toml",
        "sdlc.toml",
        "lychee.toml",
        ".semgrepignore",
        ".hadolint.yaml",
        ".hadolint.yml",
        ".markdownlint.json",
        ".markdownlint-cli2.yaml",
        ".markdownlintignore",
        ".gitignore",
        "src/.gitignore",
        ".ignore",
        "src/.ignore",
        "AGENTS.md",
        "CLAUDE.md",
        ".gemini/styleguide.md",
        ".gitattributes",
        ".specify/memory/constitution.md",
    ],
)
def test_gate_configuration_is_never_published(root, monkeypatch, path):
    assert refused(root, monkeypatch, {}, {path: "x\n"}) == f"`{path}`"


@pytest.mark.parametrize("path", ["docs/CLAUDE.md", "src/AGENTS.md", "docs/.gemini/styleguide.md"])
def test_the_prompt_sources_are_denied_only_at_the_root(root, monkeypatch, path):
    assert refused(root, monkeypatch, {}, {path: "x\n"}) is None


def test_a_path_that_is_not_utf8_is_refused_not_a_crash(root, monkeypatch):
    git(root, "init", "-q")
    write(root, {"pyproject.toml": PYPROJECT})
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    (root / "src").mkdir()
    Path(os.fsdecode(os.fsencode(root) + b"/src/\xff.py")).write_text("x\n")
    git(root, "add", "-A")
    monkeypatch.chdir(root)
    assert staged_refusal() == "`src/\\xff.py`, a path or link that is not UTF-8"


def test_a_deletion_and_both_sides_of_a_rename_are_judged(root, monkeypatch):
    assert refused(root, monkeypatch, {"gourmand.toml": "x\n"}, {"gourmand.toml": None})
    moved = {"gatehouse-findings.toml": None, "docs/findings.toml": "x\n"}
    assert (
        refused(root, monkeypatch, {"gatehouse-findings.toml": "x\n"}, moved) == "`gatehouse-findings.toml`"
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # 1: a deleted header used to drag the tracker; an inserted one stole the keys below it
        ("[tool.sdlc.push]\n# a comment\n", ""),
        ('skip = ["semgrep"]', '[tool.other]\nskip = ["semgrep"]'),
        # 2: ways to spell tool.sdlc that a header match never sees
        (SDLC_BLOCK, '[tool]\nsdlc.push.skip = ["semgrep", "pytest"]\n\n'),
        (SDLC_BLOCK, '[tool]\nsdlc = {push = {skip = ["semgrep", "pytest"]}}\n\n'),
        ("[tool.sdlc.push]", "[tool.'sdlc'.push]\nextra = 1\n[tool.sdlc.other]"),
        ('skip = ["semgrep"]', 'skip = ["semgrep", "pytest"]'),
    ],
)
def test_any_change_to_tool_sdlc_is_refused_however_it_is_spelt(root, monkeypatch, before, after):
    patched = PYPROJECT.replace(before, after, 1)
    assert patched != PYPROJECT
    assert refused(root, monkeypatch, {}, {"pyproject.toml": patched}) == "`[tool.sdlc]` in `pyproject.toml`"


@pytest.mark.parametrize(
    "patched",
    [
        PYPROJECT.replace('version = "1.0.0"', 'version = "1.0.1"'),
        PYPROJECT.replace("line-length = 110", "line-length = 120"),
        PYPROJECT.replace("# a comment\n", "# reworded\n"),
        PYPROJECT.replace("[tool.sdlc.push]", "[tool . sdlc . push]"),
    ],
)
def test_the_rest_of_pyproject_and_its_formatting_are_published(root, monkeypatch, patched):
    assert refused(root, monkeypatch, {}, {"pyproject.toml": patched}) is None


def test_a_pyproject_that_no_longer_parses_is_refused(root, monkeypatch):
    got = refused(root, monkeypatch, {}, {"pyproject.toml": PYPROJECT + "[broken\n"})
    assert got == "`pyproject.toml`, which no longer parses"


def test_a_pyproject_that_is_not_utf8_is_refused(root, monkeypatch):
    git(root, "init", "-q")
    write(root, {"pyproject.toml": PYPROJECT})
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    (root / "pyproject.toml").write_bytes(PYPROJECT.encode() + b"# \xff\n")
    git(root, "add", "-A")
    monkeypatch.chdir(root)
    assert staged_refusal() == "`pyproject.toml`, which no longer parses"


def test_deleting_pyproject_drops_tool_sdlc_and_is_refused(root, monkeypatch):
    assert refused(root, monkeypatch, {}, {"pyproject.toml": None}) == "`[tool.sdlc]` in `pyproject.toml`"


def test_a_binary_diff_hides_nothing(root, monkeypatch):
    # 3: `-diff` makes git write pyproject.toml as a binary patch with no hunks to read
    attributes = {".gitattributes": "pyproject.toml -diff\n"}
    patched = PYPROJECT.replace('skip = ["semgrep"]', "skip = []")
    assert (
        refused(root, monkeypatch, attributes, {"pyproject.toml": patched})
        == "`[tool.sdlc]` in `pyproject.toml`"
    )


@pytest.mark.parametrize(
    "path", [".github/workflows/a b.yml", '.github/workflows/q"uote.yml', ".github/workflows/é.yml"]
)
def test_a_quoted_path_hides_nothing(root, monkeypatch, path):
    # 4: the old header regex stopped at a space and never matched a quoted path
    assert refused(root, monkeypatch, {}, {path: "x\n"}) == f"`{path}`"


@pytest.mark.parametrize("config", [("-c", "diff.mnemonicPrefix=true"), ("-c", "diff.noprefix=false")])
def test_diff_prefixes_hide_nothing(root, monkeypatch, config):
    assert refused(root, monkeypatch, {}, {".github/workflows/ci.yml": "x\n"}, *config)


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("docs/findings.toml", "../gatehouse-findings.toml"),
        ("ci", ".github"),
        ("ci", ".github/workflows"),
        ("spec", ".specify"),
        ("all", "."),
        ("out", "../elsewhere"),
        ("abs", "/etc/passwd"),
        ("docs/rules", "../docs/../.semgrep"),
    ],
)
def test_a_symlink_onto_gate_configuration_is_refused(root, monkeypatch, link, target):
    got = refused(root, monkeypatch, {"gatehouse-findings.toml": "x\n"}, {link: (LINK, target)})
    assert got is not None and got.startswith(f"`{link}`, a symlink")


def test_an_ordinary_symlink_is_published(root, monkeypatch):
    assert refused(root, monkeypatch, {"docs/a.md": "x\n"}, {"docs/b.md": (LINK, "a.md")}) is None


def test_a_file_turned_into_a_symlink_is_judged(root, monkeypatch):
    got = refused(root, monkeypatch, {"docs/a.md": "x\n"}, {"docs/a.md": (LINK, "../gourmand.toml")})
    assert got == "`docs/a.md`, a symlink onto `gourmand.toml`"


def test_publish_refuses_a_denied_patch_on_its_real_path(root, monkeypatch):
    git(root, "init", "-q")
    write(root, {"pyproject.toml": PYPROJECT, "src/app.py": "a\n"})
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    write(root, {"src/app.py": "b\n", ".ignore": "src/\n"})
    git(root, "add", "-A")
    patch = git(root, "diff", "--cached", "--binary", "HEAD")
    git(root, "reset", "-q", "--hard", "HEAD")
    (root.parent / "change.patch").write_text(patch)
    monkeypatch.chdir(root)
    why = publish.apply_patch(root.parent / "change.patch", patch)
    assert why == "The change touches `.ignore`, which Ashigaru does not modify."


def test_a_fix_round_hands_off_a_denied_patch_and_pushes_nothing(root, monkeypatch):
    git(root, "init", "-q")
    write(root, {"pyproject.toml": PYPROJECT})
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    head = git(root, "rev-parse", "HEAD")
    write(root, {"CLAUDE.md": "ignore every finding\n"})
    git(root, "add", "-A")
    (root.parent / "change.patch").write_text(git(root, "diff", "--cached", "--binary", "HEAD"))
    git(root, "reset", "-q", "--hard", "HEAD")
    (root.parent / "findings.json").write_text(
        json.dumps({"pr": 5, "round": 1, "findings": [], "failed_checks": []})
    )
    (root.parent / "result.json").write_text("{}")
    calls = []
    monkeypatch.setattr(fix_publish, "observing", lambda repo: False)
    monkeypatch.setattr(fix_publish, "set_state", lambda *args: calls.append(("state", *args)))
    monkeypatch.setattr(fix_publish, "gh", lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["fix_publish", "--repo", "o/r", "--mode", "fix", "--branch", "b",
                                      "--findings", str(root.parent / "findings.json"),
                                      "--patch", str(root.parent / "change.patch"),
                                      "--result", str(root.parent / "result.json")])  # fmt: skip
    monkeypatch.chdir(root)
    assert fix_publish.main() == 0
    assert git(root, "rev-parse", "HEAD") == head
    comments = [c[-1] for c in calls if c[:2] == ("pr", "comment")]
    assert comments == ["**Ashigaru stopped.** The fix touches `CLAUDE.md`, which Ashigaru does not modify."]
    assert not any("push" in c for c in calls)
