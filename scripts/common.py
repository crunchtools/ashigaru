"""Shared helpers: the `gh` CLI, step outputs, and the label vocabulary."""

from __future__ import annotations

import fnmatch
import functools
import json
import os
import posixpath
import re
import subprocess
import tomllib
from typing import Any

READY_FOR_TRIAGE = "ready-for-triage"
READY_TO_CODE = "ready-to-code"
TRIAGED = "triaged"
NEEDS_INFO = "needs-info"
NEEDS_HUMAN = "needs-human"

STATE_LABELS = {
    READY_FOR_TRIAGE: ("c5def5", "Ashigaru: queued for triage"),
    READY_TO_CODE: ("0e8a16", "Ashigaru: bug-class work, will be fixed automatically"),
    TRIAGED: ("bfd4f2", "Ashigaru: triaged, waiting for a human decision"),
    NEEDS_INFO: ("fbca04", "Ashigaru: cannot act without more information"),
    NEEDS_HUMAN: ("d93f0b", "Ashigaru: automation stopped, a human takes over"),
}

TYPE_LABELS = {
    "bug": ("d73a4a", "Something isn't working"),
    "documentation": ("0075ca", "Improvements or additions to documentation"),
    "enhancement": ("a2eeef", "New feature or request"),
    "performance": ("5319e7", "Slower or heavier than it should be"),
    "question": ("d876e3", "Further information is requested"),
}
"""What an issue is, next to the state label that says where it stands."""
INBOX = "ashigaru-inbox"
"""Marks the one issue the sweep keeps as the list of what waits on a maintainer."""
RELEASES = "ashigaru-releases"
"""Marks the one issue the sweep keeps as the record of what was released and what was not."""
RED = "ashigaru-red"
"""Marks the issue the sweep keeps in a repository while its default branch is red."""
OBSERVE = "ashigaru-observe"
"""The repository topic that puts a repository in watch-only mode, and the label of the one issue
that records what Ashigaru would have done there. Only a repository admin can set the topic."""
OBSERVE_TITLE = "Ashigaru is watching"
MANAGED_LABELS = (
    STATE_LABELS
    | TYPE_LABELS
    | {
        OBSERVE: ("ededed", "Ashigaru: watching this repository, acting on nothing"),
        INBOX: ("ededed", "Ashigaru: what is waiting on a maintainer"),
        RELEASES: ("ededed", "Ashigaru: what was released and what is waiting to be"),
        RED: ("b60205", "Ashigaru: the default branch is red"),
    }
)

WRITE_PERMISSIONS = {"admin", "write"}  # the API folds maintain into write, triage into read
BRANCH_PREFIX = "ashigaru/"
DENIED_DIRS = (".github/", ".sdlc/", ".semgrep/", ".gourmand-exceptions.d/")
"""Directories a patch may not touch: what CI runs, how the gates run, and their rules and waivers."""
DENIED_NAMES = (
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
    ".hadolint.y*ml",
    ".markdownlint*",
    ".gitignore",
    ".gitattributes",
)
"""File names, at any depth, that configure, waive or hide what a gate sees. An agent that edits
them grades its own work. .gitattributes is here because it can turn a file's diff into a binary
blob or mark it generated, which hides it from review."""
DENIED_PATHS = (".specify/memory/constitution.md",)
SYMLINK = "120000"
SECRET = re.compile(r"sk-ant-[\w-]{16,}|gh[pousr]_\w{20,}|github_pat_\w{20,}")
"""Anything shaped like a Claude or GitHub token. Blanked out of agent-written text before it
is posted: the agent jobs are denied the files that hold their token, and this is the second
lock, on the door the text leaves by."""
REMOVED = "[removed]"
# Applied to agent-written text before it is posted. An agent that read a stranger's words may
# repeat them, so the text gets no mentions, no HTML, and no Markdown links or images.
INERT = str.maketrans({"@": "@\u200b", "<": "&lt;", "[": "(", "]": ")"})
UNLINKED = ":\u200b//"


def publishable(text: str) -> str:
    """Agent-written text made safe to post.

    Mentions, HTML and Markdown links are made inert, a bare URL is broken so
    it does not become a link, and anything shaped like a credential is blanked.
    """
    return SECRET.sub(REMOVED, text.translate(INERT).replace("://", UNLINKED))


DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def gh(*args: str, stdin: str | None = None) -> str:
    """Run `gh` and return stdout. Raises CalledProcessError on a non-zero exit."""
    done = subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=stdin)
    return done.stdout


def api(path: str, *args: str) -> Any:
    return json.loads(gh("api", path, *args))


def api_pages(path: str, *args: str) -> list[Any]:
    """Every item of a paginated list endpoint."""
    pages = json.loads(gh("api", "--paginate", "--slurp", path, *args))
    return [item for page in pages for item in page]


@functools.cache
def can_write(repo: str, login: str) -> bool:
    """True when the account's effective permission on the repo is write or higher.

    Asked of the API, not read off `author_association`: MEMBER and COLLABORATOR
    say how an account relates to the repo, not what it may do there.
    """
    try:
        found = api(f"repos/{repo}/collaborators/{login}/permission")
    except subprocess.CalledProcessError as error:
        if "404" not in error.stderr:  # 404 is the API's answer for "not a collaborator"
            print(f"::warning::permission lookup for {login} on {repo} failed: {error.stderr.strip()}")
        return False
    return found.get("permission") in WRITE_PERMISSIONS


def observing(repo: str) -> bool:
    """True when the repository carries the observe topic. A failed read raises: a publish step that
    cannot tell whether it is being watched must not act."""
    return OBSERVE in (api(f"repos/{repo}/topics").get("names") or [])


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return done.stdout


def set_output(name: str, value: str) -> None:
    """Write a step output; print it when not running in Actions."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        print(f"{name}={value}")
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{name}<<ASHIGARU_EOF\n{value}\nASHIGARU_EOF\n")


def ensure_labels(repo: str, names: list[str]) -> set[str]:
    """Create the Ashigaru labels a repo is missing and return every label it has."""
    existing = {label["name"] for label in api_pages(f"repos/{repo}/labels?per_page=100")}
    for name in names:
        if name in existing or name not in MANAGED_LABELS:
            continue
        color, description = MANAGED_LABELS[name]
        try:
            gh("label", "create", name, "--repo", repo, "--color", color, "--description", description)
        except subprocess.CalledProcessError as error:
            if "already exists" not in error.stderr:  # another run created it first
                raise
        existing.add(name)
    return existing


def set_state(
    repo: str,
    number: int,
    state: str,
    *,
    extra: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> None:
    """Give an issue or PR exactly one state label, plus the type labels in `extra`, minus `remove`."""
    existing = ensure_labels(repo, [state, *extra])
    add = [state, *(name for name in extra if name in existing)]
    current = {label["name"] for label in api(f"repos/{repo}/issues/{number}")["labels"]}
    args = ["issue", "edit", str(number), "--repo", repo, "--add-label", ",".join(add)]
    drop = [
        name for name in [*STATE_LABELS, *remove] if name != state and name not in add and name in current
    ]
    if drop:
        args += ["--remove-label", ",".join(drop)]
    gh(*args)


def denied(path: str) -> bool:
    """True when a repository path is gate configuration that Ashigaru leaves to people."""
    name = posixpath.basename(path)
    return (
        path in DENIED_PATHS
        or f"{path}/".startswith(DENIED_DIRS)
        or any(fnmatch.fnmatchcase(name, pattern) for pattern in DENIED_NAMES)
    )


def _holds_denied(target: str) -> bool:
    """True when a symlink target is, or is an ancestor of, gate configuration, or leaves the tree."""
    if posixpath.isabs(target) or target == ".." or target.startswith("../"):
        return True
    return (
        denied(target)
        or target == "."
        or any(d.startswith(f"{target}/") for d in (*DENIED_DIRS, *DENIED_PATHS))
    )


def _sdlc_table(spec: str) -> object:
    """`tool.sdlc` of the pyproject.toml at a git object spec; None when the file is absent.

    Raises ValueError when it does not decode or parse.
    """
    try:
        text = git("show", spec)
    except subprocess.CalledProcessError:
        return None
    return tomllib.loads(text).get("tool", {}).get("sdlc")


def staged_refusal() -> str | None:
    """What in the index, against HEAD, Ashigaru may not publish, or None.

    Run after `git apply --index`, so it judges what git staged rather than the
    patch text: a quoted path, a rename, a binary diff or a diff config cannot
    hide a file. Symlinks onto gate configuration are refused, and pyproject's
    `tool.sdlc` table is compared parsed, so how it is spelt does not matter.
    """
    raw = git("-c", "core.quotePath=false", "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev",
              "--no-ext-diff", "--no-textconv", "HEAD")  # fmt: skip
    fields = raw.split("\0")
    changed = set()
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        if not meta.startswith(":"):
            continue
        _, new_mode, _, new_sha, _ = meta[1:].split(" ")
        changed.add(path)
        if denied(path):
            return f"`{path}`"
        if new_mode == SYMLINK:
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(path), git("cat-file", "blob", new_sha))
            )
            if _holds_denied(target):
                return f"`{path}`, a symlink onto `{target}`"
    if "pyproject.toml" in changed:
        try:
            moved = _sdlc_table("HEAD:pyproject.toml") != _sdlc_table(":pyproject.toml")
        except ValueError:  # TOMLDecodeError, or bytes that are not UTF-8
            return "`pyproject.toml`, which no longer parses"
        if moved:
            return "`[tool.sdlc]` in `pyproject.toml`"
    return None
