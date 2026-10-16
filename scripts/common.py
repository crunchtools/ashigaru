"""Shared helpers: the `gh` CLI, step outputs, and the label vocabulary."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
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
MANAGED_LABELS = (
    STATE_LABELS
    | TYPE_LABELS
    | {
        INBOX: ("ededed", "Ashigaru: what is waiting on a maintainer"),
        RELEASES: ("ededed", "Ashigaru: what was released and what is waiting to be"),
    }
)

WRITE_PERMISSIONS = {"admin", "write"}  # the API folds maintain into write, triage into read
BRANCH_PREFIX = "ashigaru/"
PROTECTED_DIR = ".github/"
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


def touches_protected(patch: str) -> bool:
    """True when a git diff changes a file under .github/ (either side of a rename).

    Workflows, local actions, CODEOWNERS and Dependabot config decide what CI runs
    and who reviews; Ashigaru leaves all of it to people.
    """
    return any(
        path.startswith(PROTECTED_DIR) for match in DIFF_HEADER.finditer(patch) for path in match.groups()
    )
