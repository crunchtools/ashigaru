#!/usr/bin/env python3
"""Turn the triage agent's structured verdict into labels and a comment."""

from __future__ import annotations

import argparse
import json
import sys

from common import NEEDS_INFO, READY_TO_CODE, TRIAGED, api, api_pages, gh, publishable, set_output, set_state

CATEGORIES = ("bug", "documentation", "performance", "feature", "question", "unclear")
TYPE_LABEL = {
    "bug": "bug",
    "documentation": "documentation",
    "performance": "performance",
    "feature": "enhancement",
    "question": "question",
}
OUTSIDE = "outside"
NEXT_STEP = {
    READY_TO_CODE: "Ashigaru will open a pull request for this.",
    TRIAGED: "Left for a maintainer. Add `ready-to-code` to have Ashigaru take it.",
    NEEDS_INFO: "Answer the question above, then add `ready-for-triage`.",
}
DECIDE = "Answer the question above in a comment, then add `ready-to-code`. Ashigaru reads the answer first."
VOUCH = "Ashigaru can fix this. It starts when a maintainer adds `ready-to-code`."
QUEUED = "Queued. Ashigaru starts a limited number of fixes each hour, oldest first."
MARKER = "<!-- ashigaru:triage -->"


def decide(result: dict, auto_categories: set[str], min_confidence: float) -> str:
    """The state label a maintainer's issue gets. Anything malformed goes to a human."""
    category = result.get("category")
    confidence = result.get("confidence")
    if category not in CATEGORIES or not isinstance(confidence, (int, float)):
        return TRIAGED
    if category in ("question", "unclear"):
        return NEEDS_INFO
    # A missing fact blocks only work Ashigaru would otherwise start. A feature waits for a
    # maintainer either way, and the open question is in the summary they will read.
    if result.get("needs_info") and category in auto_categories:
        return NEEDS_INFO
    # A bug whose fix needs a maintainer's choice is still a bug; it just does not start alone.
    if category in auto_categories and confidence >= min_confidence and not result.get("needs_decision"):
        return READY_TO_CODE
    return TRIAGED


def type_labels(
    category: str, history: list[dict], current: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The type labels to add and to remove.

    A type label a person put on the issue wins: Ashigaru adds none of its own, and
    drops any it added earlier that disagree. Without one, Ashigaru's reading
    replaces whatever type label a bot left there before.
    """
    types = set(TYPE_LABEL.values())
    last_actor: dict[str, str] = {}
    for event in history:
        if event.get("event") == "labeled" and event.get("label", {}).get("name") in types:
            last_actor[event["label"]["name"]] = (event.get("actor") or {}).get("type", "")
    present = types & current
    human = {name for name in present if last_actor.get(name, "User") != "Bot"}
    if human:
        return (), tuple(sorted(present - human))
    ours = TYPE_LABEL.get(category)
    return ((ours,) if ours else ()), tuple(sorted(present - {ours}))


def settle(state: str, trust: str) -> tuple[str, str | None]:
    """The label an issue really gets, and the note that replaces the usual next step.

    An issue from outside is classified like any other, but the decision to
    write code from a stranger's report belongs to a maintainer.
    """
    if trust == OUTSIDE and state == READY_TO_CODE:
        return TRIAGED, VOUCH
    return state, None


def starts_now(state: str, queue: str) -> bool:
    """Whether this run goes straight on to the code agent.

    A newly opened issue does. Anything triaged from the backlog waits for the
    sweep, which is what holds coding to its hourly budget.
    """
    return state == READY_TO_CODE and queue != "backlog"


def comment(result: dict, state: str, next_step: str) -> str:
    category = result.get("category", "unknown")
    confidence = result.get("confidence")
    shown = f"{confidence:.0%}" if isinstance(confidence, (int, float)) else "n/a"
    summary = publishable(str(result.get("summary", "")).strip()) or "No summary returned."
    decision = ", needs a maintainer's decision" if result.get("needs_decision") else ""
    return f"{MARKER}\n**Ashigaru triage:** {category} ({shown}){decision}\n\n{summary}\n\n{next_step}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--result", required=True, help="structured output JSON from the agent")
    parser.add_argument("--auto-categories", default="bug,documentation,performance")
    parser.add_argument("--min-confidence", default=0.7, type=float)
    parser.add_argument("--queue", default="fresh", choices=("fresh", "backlog"))
    parser.add_argument("--trust", default="maintainer", choices=("maintainer", OUTSIDE))
    args = parser.parse_args()

    try:
        result = json.loads(args.result)
    except json.JSONDecodeError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    auto = {name.strip() for name in args.auto_categories.split(",") if name.strip()}
    state, note = settle(decide(result, auto, args.min_confidence), args.trust)

    current = {label["name"] for label in api(f"repos/{args.repo}/issues/{args.issue}")["labels"]}
    # Who set a type label matters only when one is there; most issues arrive with none.
    history = (
        api_pages(f"repos/{args.repo}/issues/{args.issue}/events")
        if current & set(TYPE_LABEL.values())
        else []
    )
    add, remove = type_labels(str(result.get("category", "")), history, current)
    set_state(args.repo, args.issue, state, extra=add, remove=remove)
    now = starts_now(state, args.queue)
    if note:
        next_step = note
    elif state == READY_TO_CODE and not now:
        next_step = QUEUED
    elif state == TRIAGED and result.get("needs_decision"):
        next_step = DECIDE
    else:
        next_step = NEXT_STEP[state]
    gh("issue", "comment", str(args.issue), "--repo", args.repo, "--body", comment(result, state, next_step))
    set_output("state", state)
    set_output("auto_code", "true" if now else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
