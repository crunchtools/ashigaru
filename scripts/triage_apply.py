#!/usr/bin/env python3
"""Turn the triage agent's structured verdict into labels and a comment."""

from __future__ import annotations

import argparse
import json
import sys

from common import NEEDS_INFO, READY_TO_CODE, TRIAGED, gh, publishable, set_output, set_state

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
    if category in auto_categories and confidence >= min_confidence:
        return READY_TO_CODE
    return TRIAGED


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
    return f"{MARKER}\n**Ashigaru triage:** {category} ({shown})\n\n{summary}\n\n{next_step}"


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

    type_label = TYPE_LABEL.get(result.get("category", ""))
    set_state(args.repo, args.issue, state, extra=(type_label,) if type_label else ())
    now = starts_now(state, args.queue)
    next_step = note or (QUEUED if state == READY_TO_CODE and not now else NEXT_STEP[state])
    gh("issue", "comment", str(args.issue), "--repo", args.repo, "--body", comment(result, state, next_step))
    set_output("state", state)
    set_output("auto_code", "true" if now else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
