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
from datetime import UTC, date, datetime, timedelta

import inbox
from common import (
    BRANCH_PREFIX,
    INBOX,
    NEEDS_HUMAN,
    NEEDS_INFO,
    READY_FOR_TRIAGE,
    READY_TO_CODE,
    RELEASES,
    STATE_LABELS,
    TRIAGED,
    api,
    api_pages,
    can_write,
    ensure_labels,
    gh,
    set_output,
)

CALLER = ".github/workflows/ashigaru.yml"
MAX_LIMIT = 20
"""No run queues more than this, whatever limit it is asked for."""
WINDOW = timedelta(hours=1)
"""The span the coding budget is counted over."""
SETTLE = timedelta(minutes=20)
"""An issue touched more recently than this may have a fix in flight; leave it for the next run."""
PULL_FIELDS = (
    "number,headRefName,headRefOid,baseRefName,mergeable,author,"
    "isCrossRepository,isDraft,labels,title,url,createdAt,autoMergeRequest"
)
OLDEST_OPEN = "issues?state=open&sort=created&direction=asc&per_page=100"
"""One page per repo: the globally oldest MAX_LIMIT are always within each repo's oldest 100."""


def candidates(repo: str, issues: list[dict]) -> list[dict]:
    """Open issues in one repo, written by a person, that carry none of Ashigaru's labels."""
    return [
        {"repo": repo, "number": issue["number"], "created_at": issue["created_at"]}
        for issue in issues
        if "pull_request" not in issue
        and issue["user"].get("type") == "User"
        and not {label["name"] for label in issue.get("labels", [])} & {*STATE_LABELS, INBOX, RELEASES}
    ]


def stale(repo: str, issues: list[dict], before: str, events, setters: set[str]) -> list[dict]:
    """Issues Ashigaru itself left `triaged` or `needs-info` before `before`, to triage again.

    For after a classifier fix: verdicts the old prompt got wrong are not
    revisited otherwise, since the label hides the issue from `candidates`. A
    state label anyone outside `setters` (Ashigaru's logins) set stays.
    `events(repo, number)` returns the issue's event history.
    """
    found = []
    for issue in issues:
        labels = {label["name"] for label in issue.get("labels", [])}
        held = labels & {TRIAGED, NEEDS_INFO}
        if "pull_request" in issue or not held or labels & {READY_FOR_TRIAGE, READY_TO_CODE}:
            continue
        set_by = [
            event for event in events(repo, issue["number"])
            if event.get("event") == "labeled" and event["label"]["name"] in held
        ]  # fmt: skip
        if (
            set_by
            and (set_by[-1].get("actor") or {}).get("login") in setters
            and set_by[-1]["created_at"] < before
        ):
            found.append({"repo": repo, "number": issue["number"], "created_at": issue["created_at"]})
    return found


def select(found: list[dict], limit: int) -> list[dict]:
    """Oldest first across every repo, at most `limit`.

    Outside authors are included: triage only reads, and its guard keeps their
    issues away from the code agent.
    """
    ordered = sorted(found, key=lambda item: (item["created_at"], item["repo"], item["number"]))
    return ordered[: min(max(limit, 0), MAX_LIMIT)]


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


SEARCH = """
query($q: String!, $since: DateTime!, $endCursor: String) {
  search(type: ISSUE, query: $q, first: 100, after: $endCursor) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue {
      number title url createdAt closedAt repository { nameWithOwner }
      labels(first: 20) { nodes { name } }
      timelineItems(last: 100, since: $since, itemTypes: [LABELED_EVENT]) {
        nodes { ... on LabeledEvent { createdAt label { name } } }
      }
    } }
  }
}
"""
"""Issues with their labels and the label events since the window opened, so triage dates cost no
request per issue."""


DEFAULT_BRANCHES = """
query($org: String!, $endCursor: String) {
  organization(login: $org) {
    repositories(first: 100, after: $endCursor, privacy: PUBLIC, isArchived: false,
                 orderBy: {field: NAME, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner isArchived
        defaultBranchRef { name target { ... on Commit { oid url statusCheckRollup { state
          contexts(first: 100) { totalCount nodes {
            ... on CheckRun { name conclusion detailsUrl }
            ... on StatusContext { context state targetUrl }
          } }
        } } } }
      }
    }
  }
}
"""
"""Each public repository's default branch head and its check rollup, a hundred repositories a
request. Public only: the inbox is a public issue."""
WATCHED_REPO = "constitution"
WATCHED_WORKFLOWS = ("fleet-drift.yml", "validate-cascade.yml")
"""Run by hand or by dispatch, never on a push, so their result is not in any branch's rollup."""


def graphql(query: str, **variables: str) -> list[dict]:
    """The `data` of every page of a GraphQL query that pages on `$endCursor`."""
    fields = [arg for name, value in variables.items() for arg in ("-f", f"{name}={value}")]
    pages = json.loads(gh("api", "graphql", "--paginate", "--slurp", "-f", f"query={query}", *fields))
    return [page["data"] for page in pages]


def search(query: str, since: str) -> list[dict]:
    """Every issue a search finds, up to the thousand search allows, with label events since `since`."""
    return [node for page in graphql(SEARCH, since=since, q=query) for node in page["search"]["nodes"]]


def last_run(repo: str, workflow: str) -> dict | None:
    """The latest completed run of one workflow, or None if it never completed."""
    runs = api(f"repos/{repo}/actions/workflows/{workflow}/runs?status=completed&per_page=1")
    return next(iter(runs["workflow_runs"]), None)


def red(org: str) -> tuple[str, int]:
    """The inbox part listing red default branches and watched runs, and how many are red.

    A failed read is named in the part, not raised: the rest of the inbox still updates.
    """
    unread: list[str] = []
    try:
        pages = graphql(DEFAULT_BRANCHES, org=org)
        branches = inbox.red_branches(
            [node for page in pages for node in page["organization"]["repositories"]["nodes"]]
        )
    except subprocess.CalledProcessError as error:
        print(f"::warning::could not read the default branches: {error.stderr.strip()}")
        branches, unread = [], ["the default branches"]
    runs = []
    repo = f"{org}/{WATCHED_REPO}"
    for workflow in WATCHED_WORKFLOWS:
        try:
            run = last_run(repo, workflow)
        except subprocess.CalledProcessError as error:
            print(f"::warning::could not read {repo} {workflow}: {error.stderr.strip()}")
            unread.append(f"{WATCHED_REPO} {workflow}")
            continue
        if failed := inbox.failed_run(repo, workflow, run):
            runs.append(failed)
    return inbox.red(branches, runs, unread), len(branches) + len(runs)


def past_week(org: str, since: str) -> tuple[list[dict], list[dict]]:
    """What Ashigaru fixed since `since`, and what it triaged since then that is still open.

    Public repositories only: the inbox is a public issue.
    """
    scope = f"org:{org} is:issue is:public"
    done = search(f"{scope} is:closed reason:completed label:{READY_TO_CODE} closed:>={since}", since)
    touched = search(f"{scope} is:open label:{TRIAGED} updated:>={since}", since)
    return inbox.fixed(done), inbox.recently_triaged(touched, since)


def requeue(item: dict) -> None:
    """Send the issue back to the code agent. The label must go away first to fire again."""
    issue, repo = str(item["number"]), item["repo"]
    ensure_labels(repo, [READY_TO_CODE])
    gh("issue", "edit", issue, "--repo", repo, "--remove-label", READY_TO_CODE)
    gh("issue", "edit", issue, "--repo", repo, "--add-label", READY_TO_CODE)


def setters(bot: str) -> set[str]:
    """Who applies Ashigaru's verdicts: the triage workflow's token, or the app's."""
    return {"github-actions[bot]", f"{bot.removeprefix('app/')}[bot]"}


def cutoff(value: str) -> str:
    """A `--retriage-before` date, checked: a malformed one would compare as any string."""
    return date.fromisoformat(value).isoformat()


def events(repo: str, number: int) -> list[dict]:
    return api_pages(f"repos/{repo}/issues/{number}/events?per_page=100")


def open_pulls(repo: str) -> list[dict]:
    listing = gh("pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
                 "--json", PULL_FIELDS)  # fmt: skip
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


def reviews_of(pulls: list[dict], bot: str) -> dict[str, dict | None]:
    """Ashigaru's latest review of each pull request given, keyed by its URL.

    One comments request per pull request, which is why the sweep tracks only
    the oldest MAX_LISTED outside pull requests.
    """
    login = f"{bot.removeprefix('app/')}[bot]"
    return {
        pull["url"]: inbox.last_review(
            api_pages(f"repos/{pull['repo']}/issues/{pull['number']}/comments?per_page=100"), login
        )
        for pull in pulls
    }


def keep_inbox(args: argparse.Namespace, since: str, waiting_on_people: str) -> None:
    """Add the past week and what is red to the inbox, and publish it. A dry run prints the red part."""
    broken, count = red(args.org)
    print(f"sweep: {count} red on the default branch")
    if args.dry_run:
        print(broken)
        return
    done, still_open = past_week(args.org, since)
    body = "\n".join([waiting_on_people, inbox.past_week(done, still_open), broken])
    print(f"sweep: inbox {inbox.publish(args.inbox_repo, body)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", required=True)
    parser.add_argument("--limit", default=MAX_LIMIT, type=int, help="issues to queue for triage")
    parser.add_argument("--code-per-hour", default=5, type=int, help="fixes started per hour, fleet-wide")
    parser.add_argument("--bot", default="app/ashigaru-crunchtools")
    parser.add_argument("--inbox-repo", default="", help="repo whose inbox issue lists what waits on people")
    parser.add_argument(
        "--retriage-before",
        # None, not "": argparse runs `type` over a string default, and
        # cutoff("") refuses it, which failed every scheduled sweep.
        default=None,
        type=cutoff,
        help="also queue issues Ashigaru labeled triaged/needs-info before this date",
    )
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
    visitors: list[dict] = []
    reports: list[dict] = []
    stopped: list[dict] = []
    questions: dict[str, int] = {}
    for repo in repos:
        if not enrolled(repo):
            continue
        issues = api(f"repos/{repo}/{OLDEST_OPEN}")
        pulls = open_pulls(repo)
        untriaged += candidates(repo, issues)
        if args.retriage_before:
            untriaged += stale(repo, issues, args.retriage_before, events, setters(args.bot))
        stuck += conflicted(repo, pulls, args.bot)
        ready += waiting(repo, issues, {pull["headRefName"] for pull in pulls}, stamp(now - SETTLE))
        # can_write is cached per repo and account, so each author costs one lookup per sweep.
        visitors += inbox.outside_pulls(repo, pulls, can_write)
        reports += inbox.outside_issues(repo, issues, can_write)
        stopped += inbox.handed_over(repo, pulls) + inbox.held_releases(repo, pulls)
        questions[repo] = inbox.unanswered(issues)

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

    # The same set is looked up, reviewed and listed, so nothing is reviewed twice for want of a lookup.
    tracked = sorted(visitors, key=inbox.by_age)[: inbox.MAX_LISTED]
    reviews = reviews_of(tracked, args.bot)
    to_review = inbox.due(tracked, reviews)
    print(f"sweep: {len(visitors)} outside pull request(s), {len(reports)} outside issue(s), "
          f"reviewing {len(to_review)}")  # fmt: skip
    for item in to_review:
        print(f"  review {item['repo']}#{item['number']}")
    set_output("reviews", json.dumps([] if args.dry_run else to_review))
    if args.inbox_repo:
        waiting_on_people = inbox.render(visitors, reviews, reports, stopped, questions)
        keep_inbox(args, stamp(now - timedelta(days=inbox.RECENT_DAYS)), waiting_on_people)
    return 0


if __name__ == "__main__":
    sys.exit(main())
