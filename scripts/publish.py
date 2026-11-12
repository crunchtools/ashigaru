#!/usr/bin/env python3
"""Publish the code agent's patch: branch, commit, push, pull request, auto-merge.

Runs in a fresh checkout that holds the app token. The agent never ran here and
never saw that token; all it handed over is a patch file and a JSON result.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import inbox
from common import (
    BRANCH_PREFIX,
    NEEDS_HUMAN,
    api,
    can_write,
    gh,
    git,
    observing,
    set_output,
    set_state,
    touches_protected,
)
from findings import REBUILD_MARKER
from relock import relock
from tidy import tidy

BOT_NAME = "ashigaru-crunchtools[bot]"
BOT_EMAIL = "ashigaru-crunchtools[bot]@users.noreply.github.com"
TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
FOOTER = "Opened by Ashigaru. Merges automatically once the required checks pass."
CONFLICTING = "CONFLICTING"
REBASED = (
    "**Ashigaru rebased this pull request** onto the current default branch after a merge conflict. "
    "Its commits, fix rounds included, are kept."
)
REBUILT = (
    f"{REBUILD_MARKER}\n**Ashigaru rebuilt this pull request** from a fresh fix on the current default "
    "branch: its own commits no longer rebase cleanly. Earlier fix rounds are gone with them, so the "
    "round count starts again and the description now matches the rebuilt change."
)


def refusal(result: dict, patch: str) -> str | None:
    """Why this run must not become a pull request, or None when it may."""
    if result.get("status") != "fixed":
        reason = str(result.get("reason", "")).strip() or "the agent did not report a fix"
        return f"No fix was produced: {reason}"
    if not patch.strip():
        return "The agent reported a fix but changed no files."
    if touches_protected(patch):
        return "The change touches `.github/`, which Ashigaru does not modify."
    return None


def credit(repo: str, author: dict) -> str:
    """A thank-you line for an outside reporter, or "" for a bot or anyone with write access."""
    login = str(author.get("login", ""))
    if not login or author.get("type") == "Bot" or can_write(repo, login):
        return ""
    return f"Reported by @{login}."


def link_line(issue: int, thanks: str = "", partial: bool = False) -> str:
    """`Closes #N`, or `Refs #N` for a partial fix, so a merge closes only an issue it resolves."""
    link = f"Refs #{issue}" if partial else f"Closes #{issue}"
    return f"{thanks}\n\n{link}" if thanks else link


def commit_message(title: str, issue: int, summary: str, thanks: str = "", partial: bool = False) -> str:
    """The fix commit's message; a non-empty `thanks` line goes just before the issue link."""
    issue_link = link_line(issue, thanks, partial)
    return f"fix: {title} (#{issue})\n\n{summary}\n\n{issue_link}\n\n{TRAILER}\n"


def hand_off(repo: str, issue: int, why: str) -> None:
    set_state(repo, issue, NEEDS_HUMAN)
    gh("issue", "comment", str(issue), "--repo", repo, "--body", f"**Ashigaru stopped.** {why}")


def apply_patch(path: Path, patch: str) -> str | None:
    """Apply the patch and refresh its lockfile; the reason it cannot be published, or None."""
    try:
        git("apply", "--index", "--3way", str(path))
    except subprocess.CalledProcessError:
        return "The fix no longer applies to the default branch."
    why = relock(patch, Path.cwd())
    if why is None:
        print(tidy(patch, Path.cwd()))
    return why


def stale(open_prs: list[dict]) -> bool:
    """True when the branch may be rebuilt: no pull request is open, or the open one conflicts.

    A pull request that still merges is never overwritten; its review threads and
    fix rounds belong to the commits already there.
    """
    return not open_prs or open_prs[0].get("mergeable") == CONFLICTING


def carry_over(branch: str, start: str) -> bool:
    """Rebase the pull request's own commits onto `start`. False, with nothing changed, when they conflict.

    The fix rounds live in those commits, and the pull request's body, review
    threads and round count describe them, so they are kept whenever git can.
    """
    remote = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    shallow = git("rev-parse", "--is-shallow-repository").strip() == "true"
    git("fetch", "--no-tags", *(["--unshallow"] if shallow else []), "origin", remote)
    git("checkout", "-B", branch, f"origin/{branch}")
    try:
        git("rebase", start)
    except subprocess.CalledProcessError as error:
        print(f"publish: {branch} does not rebase, rebuilding it:\n{error.stderr.strip()}")
        git("rebase", "--abort")
        git("checkout", "-B", branch, start)
        return False
    return True


def landed(issue: dict, merged_prs: list[dict]) -> bool:
    """True when the issue is closed and Ashigaru's own pull request for it has merged.

    A run that started before that merge finds its patch already applied; that is
    success, so it must not hand the issue to a human.
    """
    return issue.get("state") == "closed" and bool(merged_prs)


def pr_body(summary: str, issue: int, thanks: str = "", partial: bool = False) -> str:
    """The pull request description: the summary, then `Closes #N`, or `Refs #N` when `partial`."""
    issue_link = link_line(issue, thanks, partial)
    return f"{summary}\n\n{issue_link}\n\n{FOOTER}\n"


def rearm(url: str) -> None:
    """Turn auto-merge back on: GitHub turns it off when the branch is force-pushed."""
    try:
        gh("pr", "merge", url, "--auto", "--squash")
    except subprocess.CalledProcessError as error:
        gh("pr", "comment", url, "--body",
           f"**Ashigaru could not enable auto-merge**, so this needs a manual merge.\n\n"
           f"```\n{error.stderr.strip()}\n```")  # fmt: skip


def announce(repo: str, branch: str, base: str, title: str, body: str) -> str:
    """Open the pull request and turn on auto-merge. Returns its URL."""
    url = gh(
        "pr", "create", "--repo", repo, "--head", branch, "--base", base,
        "--title", title, "--body", body,
    ).strip()  # fmt: skip
    rearm(url)
    return url


def watch(repo: str, issue: int, branch: str, title: str, why: str | None) -> None:
    """A watched repository gets no branch and no pull request: the outcome is recorded instead."""
    would = f"Would push `{branch}` and open a pull request: {title}"
    if why:
        would = f"Would stop and hand it to a human: {why}"
    inbox.observe(repo, f"code #{issue}", [would])
    print(f"publish: {repo}#{issue} is watched; recorded, pushed and opened nothing")
    set_output("pr", "")


def resume(url: str, note: str) -> None:
    """Say on the pull request what the push did, then re-arm the auto-merge it turned off."""
    gh("pr", "comment", url, "--body", note)
    rearm(url)


def rebuild(args: argparse.Namespace, found: dict, result: dict, open_prs: list[dict]) -> str:
    """Commit the applied patch and push it: a new pull request, or the conflicted one rewritten to match."""
    repo, issue = args.repo, args.issue
    title = found["title"].strip()
    thanks = credit(repo, found.get("user") or {})
    summary = str(result.get("summary", "")).strip() or "Automated fix."
    partial = result.get("partial") is True
    branch = f"{BRANCH_PREFIX}issue-{issue}"
    git("commit", "-m", commit_message(title, issue, summary, thanks, partial))
    git("push", "--force", "origin", branch)

    pr_title = f"fix: {title} (#{issue})"
    body = pr_body(summary, issue, thanks, partial)
    if not open_prs:
        return announce(repo, branch, args.base, pr_title, body)
    url = open_prs[0]["url"]
    gh("pr", "edit", url, "--title", pr_title, "--body", body)
    resume(url, REBUILT)
    return url


def hand_off_rest(repo: str, issue: int, url: str, result: dict) -> None:
    """After a partial fix, give the still-open issue to a human with what is left."""
    if result.get("partial") is True:
        summary = str(result.get("summary", "")).strip() or "The agent did not say; read the pull request."
        hand_off(
            repo, issue, f"{url} fixes part of this and leaves the issue open. What is left:\n\n{summary}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()

    patch = args.patch.read_text(encoding="utf-8") if args.patch.exists() else ""
    try:
        result = json.loads(args.result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result = {}
    branch = f"{BRANCH_PREFIX}issue-{args.issue}"

    listing = gh("pr", "list", "--repo", args.repo, "--head", branch, "--state", "open",
                 "--json", "number,mergeable,url")  # fmt: skip
    open_prs = json.loads(listing)
    if not stale(open_prs):
        print(f"publish: PR #{open_prs[0]['number']} is already open for {branch}; nothing to do")
        return 0

    # Merged PRs first: the merge closes the issue, so an issue read after it is never stale.
    merged = json.loads(gh("pr", "list", "--repo", args.repo, "--head", branch, "--state", "merged",
                           "--json", "number"))  # fmt: skip
    found = api(f"repos/{args.repo}/issues/{args.issue}")
    if landed(found, merged):
        print(f"publish: issue #{args.issue} was already closed by a merged {branch}; nothing to do")
        set_output("pr", "")
        return 0

    why = refusal(result, patch)
    if observing(args.repo):
        watch(args.repo, args.issue, branch, found["title"].strip(), why)
        return 0

    git("config", "user.name", BOT_NAME)
    git("config", "user.email", BOT_EMAIL)
    start = git("rev-parse", "HEAD").strip()
    if open_prs and carry_over(branch, start):
        # The lease refuses the push if a fix round landed on the branch meanwhile.
        git("push", f"--force-with-lease={branch}:origin/{branch}", "origin", branch)
        resume(open_prs[0]["url"], REBASED)
        set_output("pr", open_prs[0]["url"])
        return 0
    if not why:
        git("checkout", "-B", branch, start)
        why = apply_patch(args.patch, patch)
    if why:
        hand_off(args.repo, args.issue, why)
        set_output("pr", "")
        return 0
    url = rebuild(args, found, result, open_prs)
    hand_off_rest(args.repo, args.issue, url, result)
    print(f"publish: published {url}")
    set_output("pr", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
