#!/usr/bin/env python3
"""Nightly backlog drain: queue the oldest untriaged issues in enrolled repos.

The per-run limit is the budget: it bounds how much agent work a night can
start, which is what keeps the factory out of the maintainer's rate limit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", required=True)
    parser.add_argument("--limit", default=5, type=int)
    parser.add_argument("--bot", default="app/ashigaru-crunchtools")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repos = [
        repo["full_name"]
        for repo in api_pages(f"orgs/{args.org}/repos?type=public&per_page=100")
        if not repo["archived"] and repo["has_issues"]
    ]
    found: list[dict] = []
    stuck: list[dict] = []
    for repo in repos:
        if enrolled(repo):
            found += candidates(repo, api(f"repos/{repo}/{OLDEST_OPEN}"))
            stuck += conflicted(repo, open_pulls(repo), args.bot)

    # Rebuilds come out of the same budget, first: finishing work beats starting it.
    budget = min(max(args.limit, 0), MAX_LIMIT)
    rebuilds = stuck[:budget]
    print(f"sweep: {len(stuck)} conflicted pull request(s), rebuilding {len(rebuilds)}")
    for item in rebuilds:
        print(f"  {item['repo']}#{item['pull']} from issue #{item['number']}")
        if not args.dry_run:
            requeue(item)

    chosen = select(found, budget - len(rebuilds))
    print(f"sweep: {len(found)} untriaged issue(s) in enrolled repos, queueing {len(chosen)}")
    for item in chosen:
        print(f"  {item['repo']}#{item['number']} ({item['created_at'][:10]})")
        if args.dry_run:
            continue
        ensure_labels(item["repo"], [READY_FOR_TRIAGE])
        gh("issue", "edit", str(item["number"]), "--repo", item["repo"],
           "--add-label", READY_FOR_TRIAGE)  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
