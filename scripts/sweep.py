#!/usr/bin/env python3
"""Hourly dispatcher: triage the backlog quickly, start fixes on a budget.

Triage is cheap, so many issues are queued for it per run. Coding is not, so
the sweep starts only as many fixes as the hourly budget has left after
counting the pull requests Ashigaru already opened in the past hour. That
budget is what keeps the factory out of the maintainer's rate limit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from common import (
    BRANCH_PREFIX,
    NEEDS_HUMAN,
    READY_FOR_TRIAGE,
    READY_TO_CODE,
    STATE_LABELS,
    api,
    api_pages,
    can_write,
    ensure_labels,
    gh,
)

CALLER = ".github/workflows/ashigaru.yml"
MAX_LIMIT = 20
"""No run queues more than this, whatever limit it is asked for."""
WINDOW = timedelta(hours=1)
"""The span the coding budget is counted over."""
SETTLE = timedelta(minutes=20)
"""An issue touched more recently than this may have a fix in flight; leave it for the next run."""
OLDEST_OPEN = "issues?state=open&sort=created&direction=asc&per_page=100"
"""One page per repo: the globally oldest MAX_LIMIT are always within each repo's oldest 100."""


def candidates(repo: str, issues: list[dict]) -> list[dict]:
    """Open issues in one repo that carry none of Ashigaru's state labels."""
    return [
        {
            "repo": repo,
            "number": issue["number"],
            "created_at": issue["created_at"],
            "author": issue["user"]["login"],
        }
        for issue in issues
        if "pull_request" not in issue
        and not {label["name"] for label in issue.get("labels", [])} & set(STATE_LABELS)
    ]


def select(found: list[dict], limit: int, writes: Callable[[str, str], bool] = can_write) -> list[dict]:
    """Oldest first across every repo, authors with write access only, at most `limit`."""
    chosen: list[dict] = []
    for item in sorted(found, key=lambda item: (item["created_at"], item["repo"], item["number"])):
        if len(chosen) >= min(max(limit, 0), MAX_LIMIT):
            break
        if writes(item["repo"], item["author"]):
            chosen.append(item)
    return chosen


def conflicted(repo: str, pulls: list[dict], bot: str) -> list[dict]:
    """Ashigaru's own open pull requests that no longer merge, as the issues to rebuild them from.

    One a human has taken over (`needs-human`) is left alone.
    """
    prefix = f"{BRANCH_PREFIX}issue-"
    return [
        {"repo": repo, "number": int(pull["headRefName"].removeprefix(prefix)), "pull": pull["number"]}
        for pull in pulls
        if pull["mergeable"] == "CONFLICTING"
        and pull["author"]["login"] == bot
        and NEEDS_HUMAN not in {label["name"] for label in pull["labels"]}
        and not pull["isCrossRepository"]
        and pull["headRefName"].removeprefix(prefix).isdigit()
    ]


def waiting(repo: str, issues: list[dict], branches: set[str], before: str) -> list[dict]:
    """Open issues labeled ready-to-code that no pull request has picked up yet."""
    return [
        {"repo": repo, "number": issue["number"], "created_at": issue["created_at"]}
        for issue in issues
        if "pull_request" not in issue
        and READY_TO_CODE in {label["name"] for label in issue.get("labels", [])}
        and f"{BRANCH_PREFIX}issue-{issue['number']}" not in branches
        and issue["updated_at"] < before
    ]


def remaining(per_hour: int, used: int) -> int:
    """Fixes this run may still start: the hourly budget less what the past hour already used."""
    return max(min(max(per_hour, 0), MAX_LIMIT) - used, 0)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def opened_since(org: str, bot: str, since: str) -> int:
    """Pull requests Ashigaru opened across the organization since `since`."""
    query = f"org:{org} is:pr author:{bot} created:>={since}"
    return int(gh("api", "-X", "GET", "search/issues", "-f", f"q={query}", "--jq", ".total_count"))


def requeue(item: dict) -> None:
    """Send the issue back to the code agent. The label must go away first to fire again."""
    issue, repo = str(item["number"]), item["repo"]
    ensure_labels(repo, [READY_TO_CODE])
    gh("issue", "edit", issue, "--repo", repo, "--remove-label", READY_TO_CODE)
    gh("issue", "edit", issue, "--repo", repo, "--add-label", READY_TO_CODE)


def open_pulls(repo: str) -> list[dict]:
    listing = gh("pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
                 "--json", "number,headRefName,mergeable,author,isCrossRepository,labels")  # fmt: skip
    return json.loads(listing)


def enrolled(repo: str) -> bool:
    try:
        gh("api", f"repos/{repo}/contents/{CALLER}", "--silent")
    except subprocess.CalledProcessError as error:
        if "404" not in error.stderr:
            print(f"::warning::could not check {repo} for {CALLER}: {error.stderr.strip()}")
        return False
    return True


def start_fixes(items: list[dict], budget: int, dry_run: bool) -> None:
    for item in items[:budget]:
        print(f"  start {item['repo']}#{item['number']}")
        if not dry_run:
            requeue(item)


def queue_triage(items: list[dict], dry_run: bool) -> None:
    for item in items:
        print(f"  triage {item['repo']}#{item['number']} ({item['created_at'][:10]})")
        if dry_run:
            continue
        ensure_labels(item["repo"], [READY_FOR_TRIAGE])
        gh("issue", "edit", str(item["number"]), "--repo", item["repo"],
           "--add-label", READY_FOR_TRIAGE)  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", required=True)
    parser.add_argument("--limit", default=MAX_LIMIT, type=int, help="issues to queue for triage")
    parser.add_argument("--code-per-hour", default=5, type=int, help="fixes started per hour, fleet-wide")
    parser.add_argument("--bot", default="app/ashigaru-crunchtools")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(UTC)
    repos = [
        repo["full_name"]
        for repo in api_pages(f"orgs/{args.org}/repos?type=public&per_page=100")
        if not repo["archived"] and repo["has_issues"]
    ]
    untriaged: list[dict] = []
    stuck: list[dict] = []
    ready: list[dict] = []
    for repo in repos:
        if not enrolled(repo):
            continue
        issues = api(f"repos/{repo}/{OLDEST_OPEN}")
        pulls = open_pulls(repo)
        untriaged += candidates(repo, issues)
        stuck += conflicted(repo, pulls, args.bot)
        ready += waiting(repo, issues, {pull["headRefName"] for pull in pulls}, stamp(now - SETTLE))

    used = opened_since(args.org, args.bot, stamp(now - WINDOW))
    budget = remaining(args.code_per_hour, used)
    # Rebuilds first: finishing work beats starting it. Then the oldest waiting issues.
    fixes = [*stuck, *sorted(ready, key=lambda item: (item["created_at"], item["repo"], item["number"]))]
    print(f"sweep: {len(stuck)} conflicted, {len(ready)} waiting, {used} opened in the past hour, "
          f"starting {min(len(fixes), budget)}")  # fmt: skip
    start_fixes(fixes, budget, args.dry_run)

    chosen = select(untriaged, args.limit)
    print(f"sweep: {len(untriaged)} untriaged issue(s) in enrolled repos, queueing {len(chosen)}")
    queue_triage(chosen, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
