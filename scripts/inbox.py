"""What is waiting on a maintainer: outside contributions and work Ashigaru stopped on.

The sweep already reads every enrolled repo's open issues and pull requests.
These functions pick out the ones only a person can move, choose which outside
pull requests are due a first-pass review, and render the list the sweep keeps
in one issue. Nothing here calls a model.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from common import (
    BRANCH_PREFIX,
    INBOX,
    NEEDS_HUMAN,
    NEEDS_INFO,
    STATE_LABELS,
    TRIAGED,
    TYPE_LABELS,
    api,
    ensure_labels,
    gh,
)

MARKER = "<!-- ashigaru:inbox -->"
TITLE = "Waiting on a maintainer"
REVIEWED = re.compile(r"<!-- ashigaru:review sha=(\w+) verdict=(\w+) fixed=(true|false) -->")
MAX_REVIEWS = 5
"""Outside pull requests reviewed per sweep."""
TITLE_LIMIT = 120
MAX_LISTED = 50
"""Rows per section; an issue body has a size limit and a longer list is not read anyway."""
INTRO = (
    "What is waiting on a maintainer across the organization. "
    "The hourly sweep rewrites this when something changes."
)
RECOMMENDATIONS = {"merge": "merge", "changes": "request changes", "close": "close"}
"""What a review may recommend, and the words shown for each. Any other value is not a verdict."""
RELEASE_BRANCH = f"{BRANCH_PREFIX}release-"
"""Where a release pull request comes from; the version follows."""
HANDED_OVER = "Labeled needs-human."
HELD_RELEASE = "Release pull request with auto-merge off; merging it publishes the release."
HOLDS = "holds further releases of this repo."
"""Why a blocked release pull request matters: the release job opens no other while one is open."""
SCANNER = "github-actions[bot]"
"""Who writes a repository's own security-scan issues: its workflow token."""
ALERT_LABELS = {"security", "dependencies"}
"""The labels the fleet's dependency scan puts on the issue it opens."""
ALERT_DAYS = 3
"""An alert open this long is listed; younger ones are Dependabot's to clear first."""
RECENT_DAYS = 7
"""How far back the fixed and triaged lists look."""
RED_ROLLUP = {"FAILURE", "ERROR"}
"""Rollup states that mean the default branch is red. PENDING and EXPECTED are not."""
FAILED_CHECKS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
"""Check run conclusions listed as failing. CANCELLED is left out: a matrix that fails fast
cancels its siblings, and listing them would bury the job that broke."""
FAILED_STATUSES = {"FAILURE", "ERROR"}
"""Commit status states listed as failing."""
FAILED_RUNS = {"failure", "timed_out", "startup_failure", "action_required"}
"""Workflow run conclusions, as REST spells them, that count as red."""
LINKABLE = re.compile(r"https://[^\s()<>\[\]`]+")
"""A check's link is set by whoever reported the check; anything else is shown without one."""
Writes = Callable[[str, str], bool]


def review_marker(sha: str, verdict: str, fixed: bool) -> str:
    """The hidden first line of a review comment: the commit reviewed and what was concluded."""
    return f"<!-- ashigaru:review sha={sha} verdict={verdict} fixed={'true' if fixed else 'false'} -->"


def last_review(comments: list[dict], bot: str) -> dict | None:
    """The newest first-pass review Ashigaru left on a pull request, if any.

    `comments` is oldest first, as the API lists them.
    """
    for comment in reversed(comments):
        found = REVIEWED.search(comment.get("body") or "")
        if found and comment["user"]["login"] == bot:
            return {"sha": found[1], "verdict": found[2], "fixed": found[3] == "true", "id": comment["id"]}
    return None


def names(item: dict) -> set[str]:
    """The label names on an issue or pull request."""
    return {label["name"] for label in item.get("labels", [])}


def outside_pulls(repo: str, pulls: list[dict], writes: Writes) -> list[dict]:
    """Open pull requests from people without write access. Bots are not people."""
    return [
        {
            "repo": repo,
            "number": pull["number"],
            "title": pull["title"],
            "url": pull["url"],
            "author": pull["author"]["login"],
            "created_at": pull["createdAt"],
            "sha": pull["headRefOid"],
            "base": pull["baseRefName"],
            "draft": pull["isDraft"],
        }
        for pull in pulls
        # A deleted account leaves no author; there is nobody to answer, so it is not listed.
        if pull.get("author")
        and not pull["author"].get("is_bot")
        and not writes(repo, pull["author"]["login"])
    ]


def outside_issues(repo: str, issues: list[dict], writes: Writes) -> list[dict]:
    """Open issues from people without write access, with what triage made of them."""
    return [
        {
            "repo": repo,
            "number": issue["number"],
            "title": issue["title"],
            "url": issue["html_url"],
            "author": issue["user"]["login"],
            "created_at": issue["created_at"],
            "labels": sorted(names(issue) & (set(TYPE_LABELS) | set(STATE_LABELS))),
        }
        for issue in issues
        if "pull_request" not in issue
        and issue["user"].get("type") == "User"
        and not writes(repo, issue["user"]["login"])
    ]


def handed_over(repo: str, pulls: list[dict]) -> list[dict]:
    """Pull requests labeled needs-human: Ashigaru stopped and a person takes over."""
    return [
        {
            "repo": repo,
            "number": pull["number"],
            "title": pull["title"],
            "url": pull["url"],
            "created_at": pull["createdAt"],
        }  # fmt: skip
        for pull in pulls
        if NEEDS_HUMAN in names(pull)
    ]


def held_releases(repo: str, pulls: list[dict]) -> list[dict]:
    """Ashigaru's release pull requests that will not merge on their own.

    The repository asked for a pull request only, or the bump is above its limit.
    One labeled needs-human is already listed by `handed_over`.
    """
    return [
        {
            "repo": repo,
            "number": pull["number"],
            "title": pull["title"],
            "url": pull["url"],
            "created_at": pull["createdAt"],
            "note": HELD_RELEASE,
        }
        for pull in pulls
        if pull["headRefName"].startswith(RELEASE_BRANCH)
        and not pull.get("autoMergeRequest")
        and not pull["isCrossRepository"]
        and NEEDS_HUMAN not in names(pull)
    ]


def blocked_releases(repo: str, pulls: list[dict]) -> list[dict]:
    """Ashigaru's release pull requests with auto-merge on whose head has a failed check.

    Auto-merge waits on that check forever: a failed head stays failed until a push,
    and Ashigaru pushes nothing to its own release branch. mcp-metsuke#66 sat four
    days like this on one unanswered Gatehouse finding while the record said it
    would merge on its own. One labeled needs-human is already listed by `handed_over`.
    """
    return [
        {
            "repo": repo,
            "number": pull["number"],
            "title": pull["title"],
            "url": pull["url"],
            "created_at": pull["createdAt"],
            "note": blocked(checks),
        }
        for pull in pulls
        if pull["headRefName"].startswith(RELEASE_BRANCH)
        and pull.get("autoMergeRequest")
        and not pull["isCrossRepository"]
        and NEEDS_HUMAN not in names(pull)
        and (checks := failing(pull.get("statusCheckRollup") or []))
    ]


def blocked(checks: list[dict]) -> str:
    """What a release pull request's row says when its head has failed checks."""
    return f"Blocked: {', '.join(check_link(check) for check in checks)} failing; {HOLDS}"


def open_alerts(repo: str, issues: list[dict], before: str) -> list[dict]:
    """Security-scan issues the repository's own workflow opened before `before`, still open.

    The scan writes them with the workflow token, which starts no workflow, and the
    sweep triages only what people wrote, so nothing else ever looks at them.
    """
    return [
        {
            "repo": repo,
            "number": issue["number"],
            "title": issue["title"],
            "url": issue["html_url"],
            "created_at": issue["created_at"],
        }
        for issue in issues
        if "pull_request" not in issue
        and issue["user"].get("login") == SCANNER
        and names(issue) >= ALERT_LABELS
        and issue["created_at"] < before
    ]


def found(node: dict, **extra: object) -> dict:
    """An issue from a search as a list item."""
    return {
        "repo": node["repository"]["nameWithOwner"],
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "created_at": node["createdAt"],
        **extra,
    }


def fixed(nodes: list[dict]) -> list[dict]:
    """Issues cleared for the code agent that closed as completed. Newest first."""
    done = [found(node, closed_at=node["closedAt"]) for node in nodes]
    return sorted(done, key=lambda item: (item["closed_at"], item["repo"], item["number"]), reverse=True)


def triaged_at(node: dict) -> str | None:
    """When the issue was last labeled triaged, from its label events. None if it never was."""
    events = node["timelineItems"]["nodes"]
    return max((event["createdAt"] for event in events if event["label"]["name"] == TRIAGED), default=None)


def recently_triaged(nodes: list[dict], since: str) -> list[dict]:
    """Open issues labeled triaged since `since`, oldest verdict first: the ones sitting longest.

    Search can only say an issue was updated in the window, which a comment does
    too, so the label events decide.
    """
    recent = [
        found(
            node,
            triaged_at=stamp,
            labels=sorted({label["name"] for label in node["labels"]["nodes"]} & set(TYPE_LABELS)),
        )
        for node in nodes
        if (stamp := triaged_at(node)) and stamp >= since
    ]
    return sorted(recent, key=lambda item: (item["triaged_at"], item["repo"], item["number"]))


def unanswered(issues: list[dict]) -> int:
    """Issues in one repo that triage could not act on without an answer."""
    return sum(1 for issue in issues if "pull_request" not in issue and NEEDS_INFO in names(issue))


def due(pulls: list[dict], reviews: dict[str, dict | None]) -> list[dict]:
    """The outside pull requests to review now: never reviewed, or changed since. Oldest first.

    `reviews` must cover every pull request passed in: one that is missing from it counts as
    never reviewed.
    """
    waiting = [
        pull
        for pull in pulls
        if not pull["draft"] and (reviews.get(pull["url"]) or {}).get("sha") != pull["sha"]
    ]
    return [
        {"repo": pull["repo"], "number": pull["number"], "sha": pull["sha"], "base": pull["base"]}
        for pull in sorted(waiting, key=by_age)[:MAX_REVIEWS]
    ]


def by_age(item: dict) -> tuple[str, str, int]:
    """Sort key: oldest first, then by repository and number so the order is stable."""
    return item["created_at"], item["repo"], item["number"]


def plain(text: str) -> str:
    """Someone else's text as one line for a code span, where links, images and mentions are inert."""
    return " ".join(text.replace("`", "'").split())[:TITLE_LIMIT]


def row(item: dict, note: str) -> str:
    """One line of the list.

    The title is a stranger's text, so it is shown as a code span. Logins carry
    no @, so an edit notifies nobody.
    """
    name = item["repo"].split("/", 1)[-1]
    author = f", by {item['author']}" if "author" in item else ""
    title = plain(item["title"])
    return (
        f"- [{name}#{item['number']}]({item['url']}) `{title}`{author}, "
        f"opened {item['created_at'][:10]}. {note}"
    )


def verdict(review: dict | None) -> str:
    """What the list says about a pull request's review."""
    said = RECOMMENDATIONS.get(review["verdict"]) if review else None
    if said:
        return f"Ashigaru: {said}{', already fixed on the base branch' if review['fixed'] else ''}."
    return "Ashigaru could not review it." if review else "Not reviewed yet."


def kinds(item: dict) -> str:
    """The type labels triage gave, after a colon, or nothing."""
    return f": {', '.join(item['labels'])}" if item["labels"] else ""


def section(title: str, rows: list[str], empty: str = "Nothing waiting.") -> list[str]:
    """A headed list with its count, cut to MAX_LISTED rows with a note of how many were left out."""
    shown = rows[:MAX_LISTED]
    extra = [f"- and {len(rows) - MAX_LISTED} more"] if len(rows) > MAX_LISTED else []
    return [f"## {title} ({len(rows)})", "", *(shown or [empty]), *extra, ""]


def render(pulls: list[dict], reviews: dict[str, dict | None], issues: list[dict],
           stopped: list[dict], questions: dict[str, int]) -> str:  # fmt: skip
    """The body of the inbox issue. No timestamps: an unchanged list renders the same."""
    lines = [MARKER, INTRO, ""]
    lines += section(
        "Pull requests from outside contributors",
        [row(pull, verdict(reviews.get(pull["url"]))) for pull in sorted(pulls, key=by_age)],
    )
    lines += section(
        "Issues from outside contributors",
        [
            row(issue, f"{', '.join(issue['labels']) or 'not triaged yet'}.")
            for issue in sorted(issues, key=by_age)
        ],
    )
    lines += section(
        "Pull requests Ashigaru handed over",
        [row(pull, pull.get("note", HANDED_OVER)) for pull in sorted(stopped, key=by_age)],
    )
    asked = [f"- {repo.split('/', 1)[-1]}: {count}" for repo, count in sorted(questions.items()) if count]
    lines += section("Issues waiting on an answer, by repo", asked)
    return "\n".join(lines).rstrip() + "\n"


def security(alerts: list[dict]) -> str:
    """The inbox part for dependency-scan alerts that have stayed open, oldest first."""
    lines = section(
        f"Security alerts open more than {ALERT_DAYS} days",
        [row(alert, "From the repository's dependency scan.") for alert in sorted(alerts, key=by_age)],
        "None open.",
    )
    return "\n".join(lines).rstrip() + "\n"


def past_week(done: list[dict], still_open: list[dict]) -> str:
    """The tail of the inbox: the past week's fixes, and triage verdicts nobody has acted on.

    These change as items age out of the window, the only clock in the body.
    """
    lines = section(
        f"Fixed by Ashigaru, past {RECENT_DAYS} days",
        [row(item, f"Closed {item['closed_at'][:10]}.") for item in done],
        "Nothing fixed.",
    )
    lines += section(
        f"Triaged, past {RECENT_DAYS} days, still open",
        [row(item, f"Triaged {item['triaged_at'][:10]}{kinds(item)}.") for item in still_open],
        "Nothing triaged.",
    )
    return "\n".join(lines).rstrip() + "\n"


def newest(contexts: list[dict]) -> list[dict]:
    """A rollup with only the newest run of each check name, as a required check is judged.

    A check re-run by a later event (Gatehouse triage after a reply) is a new run beside the
    failed one, and the old failure would otherwise read as current.
    """
    runs: dict[str, dict] = {}
    rest = []
    for context in contexts:
        if "conclusion" not in context:
            rest.append(context)
        elif (context.get("startedAt") or "") >= (runs.get(context["name"], {}).get("startedAt") or ""):
            runs[context["name"]] = context
    return [*runs.values(), *rest]


def failing(contexts: list[dict]) -> list[dict]:
    """The failed checks and statuses of a rollup, newest run per check, as names and links."""
    found = []
    for context in newest(contexts):
        if context.get("conclusion") in FAILED_CHECKS:
            found.append({"name": context["name"], "url": context.get("detailsUrl")})
        elif context.get("state") in FAILED_STATUSES:
            found.append({"name": context["context"], "url": context.get("targetUrl")})
    return found


def red_branches(repos: list[dict]) -> list[dict]:
    """Repositories whose default branch head has a failed or errored check rollup.

    A repository with no default branch or no checks on its head is not red. The
    query reads one page of checks; `unread` counts those past it, so a failure
    there is not hidden.
    """
    red = []
    for repo in repos:
        branch = repo.get("defaultBranchRef") or {}
        commit = branch.get("target") or {}
        rollup = commit.get("statusCheckRollup")
        if repo.get("isArchived") or not rollup or rollup["state"] not in RED_ROLLUP:
            continue
        red.append(
            {
                "repo": repo["nameWithOwner"],
                "branch": branch["name"],
                "sha": commit["oid"],
                "url": commit["url"],
                "state": rollup["state"],
                "checks": failing(rollup["contexts"]["nodes"]),
                "unread": rollup["contexts"].get("totalCount", 0) - len(rollup["contexts"]["nodes"]),
                "committed_at": commit.get("committedDate", ""),
                "issues": repo.get("hasIssuesEnabled", True),
            }
        )
    return sorted(red, key=lambda item: item["repo"])


def failed_run(repo: str, workflow: str, run: dict | None) -> dict | None:
    """The latest completed run of a workflow, when it did not pass. None when it did or never ran."""
    if not run or run.get("conclusion") not in FAILED_RUNS:
        return None
    return {
        "repo": repo,
        "workflow": workflow,
        "url": run["html_url"],
        "conclusion": run["conclusion"],
        "created_at": run["created_at"],
    }


def check_link(check: dict) -> str:
    """A check's name as inert text, linked when its link is a plain https URL."""
    label = f"`{plain(check['name'])}`"
    return f"[{label}]({check['url']})" if LINKABLE.fullmatch(check.get("url") or "") else label


def red_row(item: dict) -> str:
    """One red default branch as a line: the commit, its state, and the checks that failed."""
    name = item["repo"].split("/", 1)[-1]
    checks = ", ".join(check_link(check) for check in item["checks"])
    failed = f"Failing: {checks}." if checks else "No check failed outright; see the commit."
    if item.get("unread", 0) > 0:
        failed += f" {item['unread']} more checks were not read; see the commit."
    return (
        f"- [{name}]({item['url']}) `{plain(item['branch'])}` at `{item['sha'][:7]}`: "
        f"{item['state'].lower()}. {failed}"
    )


RED_MARKER = "<!-- ashigaru:red -->"
RED_TITLE = "Default branch is red"


def red_issue(item: dict) -> str:
    """The body of the issue a red repository carries. No timestamps: the same failure renders the same."""
    return "\n".join([
        RED_MARKER,
        "The default branch head has failed checks. The hourly Ashigaru sweep keeps this issue "
        "current and closes it once the branch is green again.",
        "",
        red_row(item),
        "",
    ])  # fmt: skip


def red(branches: list[dict], runs: list[dict], unread: list[str]) -> str:
    """The inbox part for CI that is red on a default branch, and for watched workflow runs.

    Check names come from other people's workflows, so they are code spans.
    `unread` names what could not be read, so a gap is not taken for green.
    """
    rows = [red_row(item) for item in branches]
    for item in runs:
        name = item["repo"].split("/", 1)[-1]
        rows.append(
            f"- [{name}]({item['url']}) `{item['workflow']}`, last run {item['created_at'][:10]}: "
            f"{item['conclusion']}."
        )
    rows += [f"- Could not read {what}; see the sweep log." for what in unread]
    return "\n".join(section("Red on the default branch", rows, "Nothing red.")).rstrip() + "\n"


def publish(repo: str, body: str, label: str = INBOX, title: str = TITLE) -> str:
    """Create the issue that carries `label`, or edit it when its list changed. Returns what was done.

    The oldest open issue with the label is the record, so its number never moves. The label
    listing once came back empty for an hour-old issue, and the sweep opened a second one; every
    later run edited the newest, leaving #90 and #94 stale under readers that open them by number.
    So an empty listing is asked again before anything is created, and a duplicate is closed in
    favour of the record.
    """
    ensure_labels(repo, [label])
    query = f"repos/{repo}/issues?state=open&labels={label}&per_page=100&sort=created&direction=asc"
    found = sorted(
        (issue for issue in api(query) or api(query) if "pull_request" not in issue),
        key=lambda issue: issue["number"],
    )
    if not found:
        gh("issue", "create", "--repo", repo, "--title", title, "--label", label, "--body", body)
        return "created"
    record, duplicates = found[0], found[1:]
    for extra in duplicates:
        gh("issue", "close", str(extra["number"]), "--repo", repo, "--reason", "not planned",
           "--comment", f"Superseded by #{record['number']}, which the sweep keeps.")  # fmt: skip
    if (record.get("body") or "").replace("\r\n", "\n").strip() == body.strip():
        return "unchanged"
    gh("issue", "edit", str(record["number"]), "--repo", repo, "--body", body)
    return "updated"
