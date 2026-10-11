#!/usr/bin/env python3
"""Decide how far an issue may go: to the code agent, to triage only, or nowhere.

Content written by an account without write access never reaches the code
agent unless a maintainer acted on the item. Triage is different: its agent can
only read, and a script turns its verdict into labels, so an outside issue may
be classified. The decision is plain code over the event payload, the effective
repository permission of two accounts (the issue's author and whoever
triggered the event) and, for a re-queue, the issue's label history. No LLM is
involved.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from collections.abc import Callable

from common import READY_FOR_TRIAGE, READY_TO_CODE, api_pages, can_write, set_output

MAINTAINER = "maintainer"
"""A maintainer wrote the issue or acted on it: triage and code may both run."""
OUTSIDE = "outside"
"""Written by someone without write access and not yet vouched for: triage only."""
NONE = "none"
STAGES = {"triage": {MAINTAINER, OUTSIDE}, "code": {MAINTAINER}}


def acted(event: dict, writes: Callable[[str], bool]) -> str | None:
    """Why a maintainer stands behind this event, or None when none does.

    On an issue someone else wrote, only `ready-to-code` from a maintainer counts.
    Any other label a maintainer adds there, `ready-for-triage` included, says
    nothing about whether the report should become code.
    """
    author = event["issue"].get("user", {}).get("login", "")
    sender = event.get("sender", {})
    label = event.get("label", {}).get("name")
    if author and writes(author):
        return "written by an account with write access"
    if event["action"] != "labeled" or label != READY_TO_CODE:
        return None
    if sender.get("type") == "User" and writes(sender.get("login", "")):
        return "a maintainer added ready-to-code"
    return None


def stranger(event: dict, vouched: Callable[[], bool]) -> tuple[str, str]:
    """The trust level of an event on an issue no maintainer wrote or just labeled."""
    label = event.get("label", {}).get("name")
    if event["issue"].get("user", {}).get("type") != "User":
        return NONE, "written by a bot without write access"
    if event["action"] == "labeled" and label == READY_TO_CODE and vouched():
        return MAINTAINER, "re-queued by automation after a maintainer added ready-to-code"
    if event["action"] == "opened" or label == READY_FOR_TRIAGE:
        return OUTSIDE, "the author has no write access and no maintainer has acted"
    return NONE, "neither the author nor the labeler has write access"


def decide(event: dict, writes: Callable[[str], bool], vouched: Callable[[], bool]) -> tuple[str, str]:
    """The trust level of an issue event, and why.

    `writes(login)` says whether an account has write access to the repository.
    `vouched()` says whether a maintainer ever put `ready-to-code` on the issue;
    it is asked only when automation re-adds that label to an outside issue.
    """
    issue = event.get("issue", {})
    if issue.get("state") != "open" or "pull_request" in issue:
        return NONE, "not an open issue"
    if event.get("action") not in ("opened", "labeled"):
        return NONE, f"action {event.get('action')} is not handled"
    reason = acted(event, writes)
    return (MAINTAINER, reason) if reason else stranger(event, vouched)


def maintainer_added(repo: str, number: int, label: str) -> bool:
    """True when the issue's history shows a person with write access adding the label."""
    return any(
        entry.get("event") == "labeled"
        and entry.get("label", {}).get("name") == label
        and (entry.get("actor") or {}).get("type") == "User"
        and can_write(repo, entry["actor"]["login"])
        for entry in api_pages(f"repos/{repo}/issues/{number}/events?per_page=100")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=sorted(STAGES))
    args = parser.parse_args()

    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    trust, reason = NONE, "not an issue event"
    if os.environ.get("GITHUB_EVENT_NAME") == "issues":
        repo = os.environ["GITHUB_REPOSITORY"]
        number = event.get("issue", {}).get("number", 0)
        trust, reason = decide(
            event,
            functools.partial(can_write, repo),
            functools.partial(maintainer_added, repo, number, READY_TO_CODE),
        )
    allowed = trust in STAGES[args.stage]
    print(f"guard: {args.stage} {'allowed' if allowed else 'refused'}, trust {trust} ({reason})")
    set_output("allowed", "true" if allowed else "false")
    set_output("trust", trust)
    return 0


if __name__ == "__main__":
    sys.exit(main())
