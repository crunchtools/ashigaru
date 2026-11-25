#!/usr/bin/env python3
"""Publish a release the moment its pull request merges, instead of at the next hourly sweep.

Runs in the released repository, from sdlc's `write` job, on the release merge's push.
It reads the pull request from the API, not from the event, and publishes only
Ashigaru's own release pull request merged into the default branch: the tag at the
merge commit, the changelog entry as the notes. The sweep publishes whatever this
missed; whichever comes first makes the tag, and the other finds it there.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime

import inbox
from common import OBSERVE, api
from release import OFF, act, configured, current, fetch, notes, tagged


def merged_release(pull: dict, repo: str, login: str) -> str | None:
    """The version a pull request released, if it is Ashigaru's release pull request and merged
    into the default branch. Anything else, whoever opened it, is not this step's to publish."""
    head, base = pull["head"], pull["base"]
    ours = (
        pull["user"]["login"] == login
        and bool(pull["merged_at"])
        and head["ref"].startswith(inbox.RELEASE_BRANCH)
        and (head["repo"] or {}).get("full_name") == repo
        and base["ref"] == base["repo"]["default_branch"]
    )
    return head["ref"].removeprefix(inbox.RELEASE_BRANCH) if ours else None


def wanted(topics: list[str]) -> bool:
    """The repository still asks for releases and is not only watched, as the sweep reads it."""
    return configured(topics)[0] != OFF and OBSERVE not in topics


def publish(repo: str, version: str, sha: str) -> int:
    """Tag `sha` as `version` and publish its changelog entry, unless someone already tagged it."""
    text = fetch(repo, "CHANGELOG.md", sha) or ""
    if current(text) != version:
        print(f"::error::{repo} at {sha[:7]}: CHANGELOG.md is at {current(text)}, not {version}")
        return 1
    if tagged(repo, version):
        print(f"release: {repo} v{version} is already tagged")
        return 0
    item = {"action": "publish", "repo": repo, "current": version, "sha": sha, "notes": notes(text, version)}
    try:
        act(item, datetime.now(UTC).strftime("%Y-%m-%d"))
    except subprocess.CalledProcessError:
        # The sweep may have published it in the same minute; only a tag nobody made is a failure.
        if not tagged(repo, version):
            raise
        print(f"release: {repo} v{version} was published by the sweep meanwhile")
    return 0


def released(repo: str, number: int, login: str) -> int:
    """Publish what pull request `number` released, if it is Ashigaru's to publish."""
    pull = api(f"repos/{repo}/pulls/{number}")
    version = merged_release(pull, repo, login)
    if not version or not wanted(api(f"repos/{repo}/topics").get("names") or []):
        print(f"release: nothing for Ashigaru to publish from {repo}#{number}")
        return 0
    return publish(repo, version, pull["merge_commit_sha"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pull", required=True, type=int, help="the pull request that closed")
    parser.add_argument("--bot", default="app/ashigaru-crunchtools-com")
    args = parser.parse_args()
    # The API names an app's account `<slug>[bot]`; the workflows name it `app/<slug>`.
    login = f"{args.bot.removeprefix('app/')}[bot]"
    return released(args.repo, args.pull, login)


if __name__ == "__main__":
    sys.exit(main())
