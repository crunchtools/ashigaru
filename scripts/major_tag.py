#!/usr/bin/env python3
"""Move the major tag to a release that was just published.

Enrolled repositories call the workflows at the major tag (`v2`), so moving it
is what delivers a release to them. It moves only forward: publishing an older
release of the same major, a late patch to a previous minor, leaves it alone.
"""

from __future__ import annotations

import argparse
import re
import sys

from common import api, api_pages, gh

RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parts(tag: str) -> tuple[int, int, int] | None:
    """`v2.6.0` as (2, 6, 0); None for anything that is not a release tag."""
    found = RELEASE.match(tag)
    return tuple(int(number) for number in found.groups()) if found else None


def major_tag(tag: str, tags: list[str]) -> str | None:
    """The major tag this release should carry, or None when it is not the newest of its major."""
    release = parts(tag)
    if release is None:
        return None
    same_major = [other for other in map(parts, tags) if other and other[0] == release[0]]
    return f"v{release[0]}" if release >= max(same_major, default=release) else None


def move(repo: str, name: str, sha: str, exists: bool) -> None:
    """Point the tag at the commit, creating it the first time."""
    if exists:
        gh("api", "-X", "PATCH", f"repos/{repo}/git/refs/tags/{name}", "-f", f"sha={sha}", "-F", "force=true")
    else:
        gh("api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/tags/{name}", "-f", f"sha={sha}")


def deliver(repo: str, tag: str) -> str:
    """Move the major tag for a published release, and say what was done."""
    release = parts(tag)
    if release is None:
        return f"{tag} is not a release tag; no major tag moves"
    refs = api_pages(f"repos/{repo}/git/matching-refs/tags/v{release[0]}?per_page=100")
    tags = [ref["ref"].removeprefix("refs/tags/") for ref in refs]
    name = major_tag(tag, tags)
    if name is None:
        return f"{tag} is not the newest release of its major; the major tag stays where it is"
    sha = api(f"repos/{repo}/commits/{tag}")["sha"]
    move(repo, name, sha, exists=name in tags)
    return f"{name} now points at {tag} ({sha[:7]})"


def main() -> int:
    """Deliver the release named on the command line and report it on stdout."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True, help="the release that was published, as vX.Y.Z")
    args = parser.parse_args()
    print(deliver(args.repo, args.tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
