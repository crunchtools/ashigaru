#!/usr/bin/env python3
"""Collect what a fix round has to answer: unanswered review findings and failed checks.

Read-only. Decides between three actions: `none` (nothing to answer), `fix`
(run the agent), `cap` (the round limit is spent, hand the PR to a human).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from common import api_pages, can_write, gh, set_output

ROUND_MARKER = "<!-- ashigaru:fix-round -->"
REBUILD_MARKER = "<!-- ashigaru:rebuilt -->"
"""A fresh rebuild after a conflict: rounds before it fixed code the branch no longer holds."""
ANSWER_MARKER = "<!-- ashigaru:answered:{kind}:{id} -->"
BRANCH_ISSUE = re.compile(r"^ashigaru/issue-(\d+)")
IGNORED_CHECKS = ("Gatehouse triage",)
FAILED = ("failure", "timed_out")
JOB_URL = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
LOG_TAIL_LINES = 150
LOG_LINES_BEFORE = 40
LOG_LINES_AFTER = 10
LOG_EXCERPT_LINES = 300
FAILURE_MARKERS = (
    "GATE CANNOT RUN",
    "cannot(",
    "##[error]",
    "Error:",
    "error:",
    "FAILED",
    "Traceback (most recent call last)",
)
POLL_SECONDS = 30
WAIT_SECONDS = 900
MAX_ROUNDS = 5
ESCALATE_AT = 3


def unanswered(comments: list[dict], reviewer: str) -> list[dict]:
    """Inline findings by the reviewer that nobody else has replied to."""
    answered = {
        comment["in_reply_to_id"]
        for comment in comments
        if comment.get("in_reply_to_id") and comment["user"]["login"] != reviewer
    }
    return [
        {
            "id": comment["id"],
            "path": comment.get("path"),
            "line": comment.get("line") or comment.get("original_line"),
            "body": comment["body"],
            "origin": "reviewer",
            "kind": "inline",
            "author": reviewer,
        }
        for comment in comments
        if not comment.get("in_reply_to_id")
        and comment["user"]["login"] == reviewer
        and comment["id"] not in answered
    ]


def maintainer_check(reviewer: str, bot: str, writer: Callable[[str], bool]) -> Callable[[dict], bool]:
    """A test for an item: written by a person (not a bot, the reviewer or Ashigaru) with write access."""

    def check(item: dict) -> bool:
        user = item.get("user") or {}
        login = user.get("login", "")
        person = (
            bool(login)
            and user.get("type") != "Bot"
            and not login.endswith("[bot]")
            and login not in (reviewer, bot, f"{bot}[bot]")
        )
        return person and writer(login)

    return check


def _entry(item: dict, kind: str, body: str) -> dict:
    return {
        "id": item["id"],
        "path": item.get("path"),
        "line": item.get("line") or item.get("original_line"),
        "body": body,
        "origin": "maintainer",
        "kind": kind,
        "author": item["user"]["login"],
    }


def maintainer_findings(
    inline: list[dict], reviews: list[dict], pr_comments: list[dict], ok: Callable[[dict], bool], bot: str
) -> list[dict]:
    """What accounts with write access said on the pull request that nobody has answered.

    An inline comment is answered once someone else replied in its thread; a review
    body or pull-request comment once an answer marker names it. Accounts without
    write access are not findings, whatever they wrote. Only Ashigaru's own comments
    carry an answer marker, and only Ashigaru or a write-access account answers a thread.
    """
    bots = (bot, f"{bot}[bot]", f"app/{bot}")

    def trusted(comment: dict) -> bool:
        return (comment.get("user") or {}).get("login") in bots or ok(comment)

    replied_by: dict[int, set[str]] = {}
    for comment in inline:
        if comment.get("in_reply_to_id") and trusted(comment):
            replied_by.setdefault(comment["in_reply_to_id"], set()).add(comment["user"]["login"])
    marked = "\n".join(
        comment.get("body") or ""
        for comment in pr_comments
        if (comment.get("user") or {}).get("login") in bots
    )

    def open_item(kind: str, item: dict) -> bool:
        return ANSWER_MARKER.format(kind=kind, id=item["id"]) not in marked and ok(item)

    found = [
        _entry(c, "inline", c["body"])
        for c in inline
        if not c.get("in_reply_to_id") and not replied_by.get(c["id"], set()) - {c["user"]["login"]} and ok(c)
    ]
    found += [
        _entry(r, "review", r["body"].strip())
        for r in reviews
        if (r.get("body") or "").strip() and r.get("state") != "PENDING" and open_item("review", r)
    ]
    found += [
        _entry(c, "comment", c["body"])
        for c in pr_comments
        if "<!-- ashigaru:" not in (c.get("body") or "") and open_item("comment", c)
    ]
    return found


def issue_context(
    issue_comments: list[dict], pr_comments: list[dict], ok: Callable[[dict], bool], bot: str
) -> list[dict]:
    """Comments added to the linked issue since the last round or rebuild, from write-access accounts."""
    since = max(
        (c["created_at"] for c in pr_comments if ROUND_MARKER in (c.get("body") or "") or rebuilt(c, bot)),
        default="",
    )
    return [
        {"author": c["user"]["login"], "body": c["body"]}
        for c in issue_comments
        if c["created_at"] > since and ok(c)
    ]


def rebuilt(comment: dict, bot: str) -> bool:
    """A rebuild marker Ashigaru posted; anyone else's would reset the round cap."""
    return REBUILD_MARKER in (comment.get("body") or "") and opened_by(
        {"author": comment.get("user") or {}}, bot
    )


def rounds_used(issue_comments: list[dict], bot: str) -> int:
    """Fix rounds since Ashigaru's last fresh rebuild; comments come oldest first."""
    used = 0
    for comment in issue_comments:
        if rebuilt(comment, bot):
            used = 0
        elif ROUND_MARKER in (comment.get("body") or ""):
            used += 1
    return used


def _own(run: dict, own_run_id: str) -> bool:
    match = JOB_URL.search(run.get("details_url") or "")
    return match is not None and match.group(1) == own_run_id


def pending(check_runs: list[dict], own_run_id: str) -> list[str]:
    return [run["name"] for run in check_runs if run["status"] != "completed" and not _own(run, own_run_id)]


def best_effort(run: dict) -> bool:
    """True when the job failed but the workflow run holding it did not.

    That is what `continue-on-error: true` looks like from outside: GitHub
    records the job's check run as a failure and concludes its check suite a
    success. A branch is not red because a job its own author marked best effort
    failed. developer's GHCR mirror is the case: the push cannot create an org
    package, so it fails on every merge, and the red-branch issue it held open
    was teaching the fleet's alert to be ignored.
    """
    return (run.get("check_suite") or {}).get("conclusion") == "success"


def failed(check_runs: list[dict], own_run_id: str) -> list[dict]:
    return [
        run
        for run in check_runs
        if run.get("conclusion") in FAILED
        and not _own(run, own_run_id)
        and not best_effort(run)
        and not any(name in run["name"] for name in IGNORED_CHECKS)
    ]


def opened_by(pr: dict, bot: str) -> bool:
    """True when the app opened this pull request. `gh` reports an app as `app/<slug>`."""
    return pr.get("author", {}).get("login") in (f"app/{bot}", f"{bot}[bot]")


def plan(findings: list[dict], failures: list[dict], used: int, max_rounds: int) -> str:
    with_work = "cap" if used >= max_rounds else "fix"
    return with_work if findings or failures else "none"


def model_for(round_number: int, model: str, escalation_model: str, escalate_at: int) -> str:
    return escalation_model if round_number >= escalate_at else model


def failure_log(repo: str, run: dict) -> str:
    match = JOB_URL.search(run.get("details_url") or "")
    if not match:
        return ""
    try:
        log = gh("run", "view", match.group(1), "--repo", repo, "--job", match.group(2), "--log-failed")
    except subprocess.CalledProcessError as error:
        print(f"::warning::no log for {run['name']}: {error.stderr.strip()}")
        return "(the log could not be retrieved)"
    return excerpt(log)


def excerpt(log: str) -> str:
    """The lines around every failure marker in the whole log, or its tail when none match.

    Windows that touch are merged. Dropped lines are counted inline, never cut silently.
    """
    lines = log.splitlines()
    hits = [i for i, line in enumerate(lines) if any(marker in line for marker in FAILURE_MARKERS)]
    if not hits:
        return "\n".join(lines[-LOG_TAIL_LINES:])
    spans: list[list[int]] = []  # [start, end, first hit]
    for i in hits:
        start, end = max(0, i - LOG_LINES_BEFORE), min(len(lines), i + LOG_LINES_AFTER + 1)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end, i])
    # The cap is shared out per window so every failure keeps some context. A window that
    # is cut keeps the lines around its first hit; cut lines are counted in the next gap.
    spans = spans[:LOG_EXCERPT_LINES]
    share = max(1, LOG_EXCERPT_LINES // len(spans))
    out: list[str] = []
    previous = 0
    for start, end, hit in spans:
        if end - start > share:
            start = max(start, min(hit - share // 2, end - share))
            end = start + share
        if start > previous:
            out.append(f"[… {start - previous} lines omitted …]")
        out.extend(lines[start:end])
        previous = end
    if previous < len(lines):
        out.append(f"[… {len(lines) - previous} lines omitted …]")
    return "\n".join(out)


def suite_conclusions(repo: str, sha: str) -> dict[int, str | None]:
    """Every check suite on the commit, by id. One call, not one per suite.

    Empty when the listing cannot be read: unknown is not best effort, so each
    check still counts.
    """
    try:
        pages = json.loads(
            gh("api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/check-suites?per_page=100")
        )
    except subprocess.CalledProcessError as error:
        print(f"::warning::no check suites for {sha}: {error.stderr.strip()}")
        return {}
    return {suite["id"]: suite.get("conclusion") for page in pages for suite in page["check_suites"]}


def with_suites(runs: list[dict], conclusions: dict[int, str | None]) -> list[dict]:
    """Fill in each run's check suite conclusion, which the check-runs listing returns as null."""
    filled = []
    for run in runs:
        suite = run.get("check_suite") or {}
        if suite.get("id") in conclusions:
            run = run | {"check_suite": suite | {"conclusion": conclusions[suite["id"]]}}
        filled.append(run)
    return filled


def check_runs(repo: str, sha: str) -> list[dict]:
    pages = json.loads(
        gh("api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    )
    runs = [run for page in pages for run in page["check_runs"]]
    # Nothing failed, so no conclusion is consulted and the listing is not read.
    if not any(run.get("conclusion") in FAILED for run in runs):
        return runs
    return with_suites(runs, suite_conclusions(repo, sha))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--reviewer", default="github-actions[bot]")
    parser.add_argument("--bot", default="ashigaru-crunchtools")
    parser.add_argument("--max-rounds", default=MAX_ROUNDS, type=int)
    parser.add_argument("--wait-seconds", default=WAIT_SECONDS, type=int)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--escalation-model", default="opus")
    parser.add_argument("--escalate-at", default=ESCALATE_AT, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    prs = json.loads(
        gh("pr", "list", "--repo", args.repo, "--head", args.branch, "--state", "open",
           "--json", "number,headRefOid,author")
    )  # fmt: skip
    if not prs or not opened_by(prs[0], args.bot):
        print(f"findings: no open pull request by {args.bot} for {args.branch}")
        set_output("action", "none")
        return 0
    number, sha = prs[0]["number"], prs[0]["headRefOid"]
    own_run_id = os.environ.get("GITHUB_RUN_ID", "")

    deadline = time.monotonic() + args.wait_seconds
    runs = check_runs(args.repo, sha)
    while pending(runs, own_run_id) and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        runs = check_runs(args.repo, sha)

    # The wait above can outlast the pull request: auto-merge lands and deletes the branch, and the
    # checkout that follows a `fix` would fail on work that succeeded.
    state = json.loads(gh("pr", "view", str(number), "--repo", args.repo, "--json", "state,mergeCommit"))
    if state.get("state") != "OPEN":
        merge = (state.get("mergeCommit") or {}).get("oid", "none")
        print(f"findings: PR #{number} is {state.get('state')} (merge commit {merge}); nothing to do")
        set_output("action", "none")
        return 0

    inline = api_pages(f"repos/{args.repo}/pulls/{number}/comments?per_page=100")
    pr_comments = api_pages(f"repos/{args.repo}/issues/{number}/comments?per_page=100")
    reviews = api_pages(f"repos/{args.repo}/pulls/{number}/reviews?per_page=100")

    ok = maintainer_check(args.reviewer, args.bot, lambda login: can_write(args.repo, login))
    findings = unanswered(inline, args.reviewer) + maintainer_findings(
        inline, reviews, pr_comments, ok, args.bot
    )
    linked = BRANCH_ISSUE.match(args.branch)
    context: list[dict] = []
    if linked:
        issue_comments = api_pages(f"repos/{args.repo}/issues/{linked.group(1)}/comments?per_page=100")
        context = issue_context(issue_comments, pr_comments, ok, args.bot)
    failures = failed(runs, own_run_id)
    used = rounds_used(pr_comments, args.bot)
    action = plan(findings, failures, used, args.max_rounds)
    round_number = used + 1

    args.out.write_text(
        json.dumps(
            {
                "pr": number,
                "round": round_number,
                "findings": findings,
                "issue_comments": context,
                "failed_checks": [
                    {"name": run["name"], "log": failure_log(args.repo, run)} for run in failures
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"findings: PR #{number} round {round_number}: {len(findings)} finding(s), "
          f"{len(failures)} failed check(s) -> {action}")  # fmt: skip
    set_output("action", action)
    set_output("pr", str(number))
    set_output("round", str(round_number))
    set_output("model", model_for(round_number, args.model, args.escalation_model, args.escalate_at))
    return 0


if __name__ == "__main__":
    sys.exit(main())
