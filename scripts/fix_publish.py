#!/usr/bin/env python3
"""Publish a fix round: push the patch and answer each finding in its thread.

A reply is `fixed in <sha>` or `not a bug: <reason>`, the two forms Gatehouse
triage accepts. A finding the agent did not account for gets no reply, so the
required check stays red rather than being cleared by default.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import inbox
from common import NEEDS_HUMAN, gh, git, observing, set_state, touches_protected
from findings import ANSWER_MARKER, MAX_ROUNDS, ROUND_MARKER
from publish import BOT_EMAIL, BOT_NAME, TRAILER
from relock import relock
from tidy import tidy


def replies(findings: list[dict], dispositions: list[dict], sha: str | None) -> dict[int, str]:
    """The reply for each finding that was accounted for, keyed by comment id."""
    known = {finding["id"]: finding for finding in findings}
    out: dict[int, str] = {}
    for item in dispositions:
        finding_id = item.get("id")
        if finding_id is None or finding_id not in known:
            continue
        maintainer = known[finding_id].get("origin") == "maintainer"
        reason = str(item.get("reason", "")).strip()
        if item.get("disposition") == "fixed" and sha:
            out[finding_id] = f"fixed in {sha}"
        elif item.get("disposition") == "not_a_bug" and reason and not maintainer:
            out[finding_id] = f"not a bug: {reason}"  # a person's comment is never dismissed
        elif item.get("disposition") == "question" and reason and maintainer:
            out[finding_id] = reason
    return out


def asked(findings: list[dict], answered: dict[int, str], dispositions: list[dict]) -> bool:
    """True when a maintainer finding was answered with a question, which hands the PR to a person."""
    questions = {i["id"] for i in dispositions if i.get("disposition") == "question"}
    return any(
        f["id"] in answered and f["id"] in questions for f in findings if f.get("origin") == "maintainer"
    )


def post_reply(repo: str, number: int, finding: dict, body: str) -> None:
    """Answer in the inline thread; elsewhere a pull-request comment carrying the answer marker."""
    if finding.get("kind", "inline") == "inline":
        gh("api", f"repos/{repo}/pulls/{number}/comments/{finding['id']}/replies", "-f", f"body={body}")
        return
    marker = ANSWER_MARKER.format(kind=finding["kind"], id=finding["id"])
    gh("pr", "comment", str(number), "--repo", repo, "--body",
       f"{marker}\n@{finding['author']} {body}")  # fmt: skip


def next_step(sha: str | None, left: int, failed_checks: bool) -> str | None:
    """What follows a round: a human, re-armed auto-merge, or nothing.

    GitHub turns auto-merge off when the branch is pushed, so a round that
    pushed and answered every finding turns it back on. One that pushed with
    findings left waits for the next round.
    """
    if sha:
        return None if left else "rearm"
    if left or failed_checks:
        return "hand_off"
    return None


def hand_off(repo: str, number: int, why: str) -> None:
    set_state(repo, number, NEEDS_HUMAN)
    gh("pr", "merge", str(number), "--repo", repo, "--disable-auto")
    gh("pr", "comment", str(number), "--repo", repo, "--body", f"**Ashigaru stopped.** {why}")


def watch(repo: str, number: int, round_number: int, mode: str) -> None:
    """A watched repository gets no round: what it would have got is recorded instead."""
    what = f"push fix round {round_number} and answer its findings"
    if mode == "cap":
        what = "hand it to a human"
    inbox.observe(repo, f"fix #{number}", [f"Would {what}."])
    print(f"fix: {repo}#{number} is watched; recorded, pushed and answered nothing")


def close_round(repo: str, collected: dict, result: dict, answered: dict[int, str], sha: str | None) -> None:
    """Hand the pull request to a person, or re-arm auto-merge, as the round's outcome calls for."""
    number = collected["pr"]
    left = len(collected["findings"]) - len(answered)
    step = next_step(sha, left, bool(collected["failed_checks"]))
    if asked(collected["findings"], answered, result.get("dispositions") or []):
        hand_off(repo, number, "A maintainer comment was answered with a question, so a person is needed.")
    elif step == "hand_off":
        hand_off(repo, number, "The round changed no code and work remains, so another round would not help.")
    elif step == "rearm":
        gh("pr", "merge", str(number), "--repo", repo, "--auto", "--squash")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--mode", required=True, choices=("fix", "cap"))
    parser.add_argument("--findings", required=True, type=Path)
    parser.add_argument("--patch", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--branch")
    parser.add_argument("--max-rounds", default=MAX_ROUNDS, type=int)
    args = parser.parse_args()

    collected = json.loads(args.findings.read_text(encoding="utf-8"))
    number, round_number = collected["pr"], collected["round"]
    if observing(args.repo):
        watch(args.repo, number, round_number, args.mode)
        return 0
    if args.mode == "cap":
        hand_off(args.repo, number,
                 f"{args.max_rounds} fix rounds are spent and the PR is still not green.")  # fmt: skip
        return 0

    patch = args.patch.read_text(encoding="utf-8") if args.patch and args.patch.exists() else ""
    try:
        result = json.loads(args.result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        result = {}
    if touches_protected(patch):
        hand_off(args.repo, number, "The fix touches `.github/`, which Ashigaru does not modify.")
        return 0

    sha = None
    if patch.strip():
        git("config", "user.name", BOT_NAME)
        git("config", "user.email", BOT_EMAIL)
        try:
            git("apply", "--index", "--3way", str(args.patch))
        except subprocess.CalledProcessError:
            hand_off(args.repo, number, "The fix no longer applies to the pull request branch.")
            return 0
        stale_lock = relock(patch, Path.cwd())
        if stale_lock:
            hand_off(args.repo, number, stale_lock)
            return 0
        print(tidy(patch, Path.cwd()))
        git("commit", "-m", f"fix: address review findings (round {round_number})\n\n{TRAILER}\n")
        git("push", "origin", f"HEAD:{args.branch}")
        sha = git("rev-parse", "HEAD").strip()

    answered = replies(collected["findings"], result.get("dispositions") or [], sha)
    by_id = {finding["id"]: finding for finding in collected["findings"]}
    for finding_id, body in answered.items():
        post_reply(args.repo, number, by_id[finding_id], body)

    left = len(collected["findings"]) - len(answered)
    summary = str(result.get("summary", "")).strip() or "No summary returned."
    gh(
        "pr", "comment", str(number), "--repo", args.repo, "--body",
        f"{ROUND_MARKER}\n**Ashigaru fix round {round_number}:** {summary}\n\n"
        f"{len(answered)} finding(s) answered, {left} left, "
        f"{'pushed ' + sha if sha else 'no code change'}.",
    )  # fmt: skip
    close_round(args.repo, collected, result, answered, sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
