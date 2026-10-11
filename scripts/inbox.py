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


def row(item: dict, note: str) -> str:
    """One line of the list.

    The title is a stranger's text, so it is shown as a code span, where links,
    images and mentions are inert. Logins carry no @, so an edit notifies nobody.
    """
    name = item["repo"].split("/", 1)[-1]
    author = f", by {item['author']}" if "author" in item else ""
    title = " ".join(item["title"].replace("`", "'").split())[:TITLE_LIMIT]
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


def publish(repo: str, body: str, label: str = INBOX, title: str = TITLE) -> str:
    """Create the issue that carries `label`, or edit it when its list changed. Returns what was done."""
    ensure_labels(repo, [label])
    found = api(f"repos/{repo}/issues?state=open&labels={label}&per_page=1")
    if not found:
        gh("issue", "create", "--repo", repo, "--title", title, "--label", label, "--body", body)
        return "created"
    if (found[0].get("body") or "").replace("\r\n", "\n").strip() == body.strip():
        return "unchanged"
    gh("issue", "edit", str(found[0]["number"]), "--repo", repo, "--body", body)
    return "updated"
