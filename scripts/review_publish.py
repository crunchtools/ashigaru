#!/usr/bin/env python3
"""Post the first-pass review of an outside pull request as one comment, kept current.

The agent's verdict is opinion; the facts beside it (does it merge, do the
checks pass) are read from the API here. The comment carries a marker with the
commit that was reviewed, which is how the sweep knows not to review it again
until the contributor pushes. Nothing here labels, merges or closes anything.
"""

from __future__ import annotations

import argparse
import json
import sys

from common import api, api_pages, gh, publishable
from inbox import RECOMMENDATIONS, last_review, review_marker

UNKNOWN = "unknown"
MAX_POINTS = 6
POINT_LIMIT = 400
SUMMARY_LIMIT = 2_000
FAILED = "Ashigaru could not produce a review of this pull request. A maintainer should look at it directly."
FOOTER = (
    "_An automated first pass for the maintainers. A person decides; nothing here merges or closes anything._"
)
MERGEABLE = {"MERGEABLE": "yes", "CONFLICTING": "no, it conflicts with the base branch"}
FACT_FIELDS = "state,mergeable,statusCheckRollup"
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def parse(raw: str) -> dict | None:
    """The agent's verdict, or None when it is missing or not in the agreed shape."""
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        print(f"::warning::the agent returned {'nothing' if not raw.strip() else 'text that is not JSON'}")
        return None
    if not isinstance(result, dict) or result.get("recommendation") not in RECOMMENDATIONS:
        print("::warning::the agent's verdict has no valid recommendation")
        return None
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        print("::warning::the agent's verdict has no summary")
        return None
    return result


def points(result: dict, key: str) -> list[str]:
    """Up to MAX_POINTS non-empty entries of a list field, scrubbed and cut; none if it is not a list."""
    found = result.get(key)
    if not isinstance(found, list):
        return []
    return [publishable(str(item).strip())[:POINT_LIMIT] for item in found if str(item).strip()][:MAX_POINTS]


def checks(rollup: list[dict]) -> str:
    """Counts of finished checks. A check still running is not counted either way."""
    done = [item.get("conclusion") or item.get("state") for item in rollup]
    done = [state for state in done if state and state not in ("PENDING", "EXPECTED")]
    if not done:
        return "none finished"
    failed = sum(1 for state in done if state not in PASSED)
    return f"{len(done) - failed} passed, {failed} failed"


def comment(result: dict | None, sha: str, facts: dict) -> str:
    """The comment body for a verdict, or for a review that failed when `result` is None.

    `facts` is the pull request as the API reports it (`mergeable`,
    `statusCheckRollup`). Agent text is scrubbed and cut to length here.
    """
    recommendation = result["recommendation"] if result else UNKNOWN
    fixed = bool(result and result.get("already_fixed") is True)
    lines = [review_marker(sha, recommendation, fixed), "**Ashigaru first-pass review**", ""]
    if result is None:
        lines += [FAILED, ""]
    else:
        summary = publishable(result["summary"].strip())[:SUMMARY_LIMIT]
        lines += [f"Recommendation: **{RECOMMENDATIONS[recommendation]}**", "", summary, ""]
        if fixed:
            lines += ["The base branch already contains this change or an equivalent one.", ""]
        for title, key in (("Blocking", "blockers"), ("Worth a look", "concerns")):
            found = points(result, key)
            if found:
                lines += [f"{title}:", *(f"- {point}" for point in found), ""]
    lines += [
        f"Merges cleanly: {MERGEABLE.get(facts.get('mergeable', ''), 'not known yet')}. "
        f"Checks: {checks(facts.get('statusCheckRollup') or [])}. Reviewed at `{sha[:7]}`.",
        "",
        FOOTER,
    ]
    return "\n".join(lines)


def post(repo: str, pull: int, body: str, bot: str) -> str:
    """Edit Ashigaru's earlier review comment, or leave the first one."""
    previous = last_review(api_pages(f"repos/{repo}/issues/{pull}/comments?per_page=100"), bot)
    if previous:
        api(f"repos/{repo}/issues/comments/{previous['id']}", "-X", "PATCH", "-f", f"body={body}")
        return "updated"
    gh("pr", "comment", str(pull), "--repo", repo, "--body", body)
    return "posted"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pull", required=True, type=int)
    parser.add_argument("--sha", required=True, help="the head commit the agent was shown")
    parser.add_argument("--result", required=True, help="structured output JSON from the agent")
    parser.add_argument("--bot", default="ashigaru-crunchtools[bot]")
    args = parser.parse_args()

    facts = json.loads(gh("pr", "view", str(args.pull), "--repo", args.repo, "--json", FACT_FIELDS))
    done = "is not open, nothing posted"
    if facts.get("state") == "OPEN":
        done = post(args.repo, args.pull, comment(parse(args.result), args.sha, facts), args.bot)
    print(f"review: {args.repo}#{args.pull} {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
