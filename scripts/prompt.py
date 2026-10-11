#!/usr/bin/env python3
"""Build an agent prompt from a template plus the issue, the pull request or the round's findings.

Issue comments from accounts without write access are dropped here, before the
agent runs: the guard decides whether the issue may be read at all, and this
keeps a stranger's later comment from riding along.

Screenshots are downloaded here too. The agents have no network, so an image in
the issue reaches them as a file they can open, or not at all.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from common import api, api_pages, can_write, gh

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
BODY_LIMIT = 20_000
COMMENT_LIMIT = 4_000
COMMENTS_KEPT = 20
LOG_LIMIT = 20_000
DIFF_LIMIT = 150_000
FILES_KEPT = 100
NO_DIFF = "[GitHub could not render this diff, usually because it is too large. Review the files listed.]"
CUT = "\n[diff cut here: too long to show in full]"
SLOT = re.compile(r"\{\{(\w+)\}\}")
IMAGES_KEPT = 5
IMAGE_LIMIT = 5_000_000
IMAGE_TIMEOUT = 30  # seconds
# With this media type the API also returns `body_html`, where an uploaded image has a URL
# that answers without a login, in a private repository as well.
RENDERED = ("-H", "Accept: application/vnd.github.full+json")
# Images GitHub hosts or proxies. Anything else in the HTML is left where it is.
IMAGE_SOURCE = re.compile(
    r'<img\b[^>]*?\ssrc="(https://(?:github\.com/user-attachments/assets/|(?:[\w-]+\.)*githubusercontent\.com/)[^"]+)"'
)
IMAGE_KINDS = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif"}
NONE = "(none)"


def trusted(repo: str, comments: list[dict]) -> list[dict]:
    """The latest comments by people with write access. Bots and everyone else are dropped."""
    kept = [
        comment
        for comment in comments
        if comment["user"].get("type") == "User" and can_write(repo, comment["user"]["login"])
    ]
    return kept[-COMMENTS_KEPT:]


def trusted_comments(repo: str, comments: list[dict]) -> str:
    kept = [
        f"[{comment['user']['login']}] {comment['body'][:COMMENT_LIMIT]}"
        for comment in trusted(repo, comments)
    ]
    return "\n\n".join(kept) or NONE


def image_sources(rendered: str | None) -> list[str]:
    """The URLs of the images in rendered issue HTML that GitHub itself serves."""
    return [html.unescape(source) for source in IMAGE_SOURCE.findall(rendered or "")]


def image_kind(image: bytes) -> str | None:
    """The file extension for image bytes, read from the bytes and not from the URL or a header."""
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "webp"
    return next((kind for magic, kind in IMAGE_KINDS.items() if image.startswith(magic)), None)


def fetch(url: str) -> bytes:
    """Up to one byte past the limit, so a caller can tell an image that is too large."""
    with urllib.request.urlopen(url, timeout=IMAGE_TIMEOUT) as response:
        return response.read(IMAGE_LIMIT + 1)


def attachments(number: int, sources: list[str], dest: Path) -> str:
    """Save the first few images to `dest` and return their paths, one per line.

    A download that fails, is too large or is not an image is skipped with a
    warning: a missing screenshot never stops the agent from running. The URL
    is kept out of the log because it can carry a signed token.
    """
    saved: list[str] = []
    for position, url in enumerate(dict.fromkeys(sources), start=1):
        if len(saved) == IMAGES_KEPT:
            print(f"::warning::issue {number} has more than {IMAGES_KEPT} images; the rest are not shown")
            break
        try:
            image = fetch(url)
        except (OSError, ValueError) as error:
            print(f"::warning::image {position} of issue {number} could not be downloaded: {error}")
            continue
        kind = image_kind(image)
        if kind is None or len(image) > IMAGE_LIMIT:
            print(f"::warning::image {position} of issue {number} is too large or not an image; skipped")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        path = dest.resolve() / f"issue-{number}-{len(saved) + 1}.{kind}"
        path.write_bytes(image)
        saved.append(str(path))
    return "\n".join(saved) or NONE


def render(template: str, values: dict[str, str]) -> str:
    """Fill `{{name}}` slots in one pass, so inserted text is never scanned for slots."""
    return SLOT.sub(lambda match: values.get(match.group(1), match.group(0)), template)


def issue_values(repo: str, number: int, images: Path | None = None) -> dict[str, str]:
    issue = api(f"repos/{repo}/issues/{number}", *RENDERED)
    comments = api_pages(f"repos/{repo}/issues/{number}/comments?per_page=100", *RENDERED)
    sources = image_sources(issue.get("body_html"))
    for comment in trusted(repo, comments):
        sources += image_sources(comment.get("body_html"))
    return {
        "repo": repo,
        "number": str(number),
        "title": issue["title"],
        "body": (issue.get("body") or "(empty)")[:BODY_LIMIT],
        "comments": trusted_comments(repo, comments),
        "attachments": attachments(number, sources, images) if images else NONE,
    }


def pull_values(repo: str, number: int) -> dict[str, str]:
    """A pull request as data for the reviewer. The diff is text; nothing from the fork is checked out."""
    pull = api(f"repos/{repo}/pulls/{number}")
    files = api(f"repos/{repo}/pulls/{number}/files?per_page={FILES_KEPT}")  # one page is enough
    try:
        # GitHub answers 406 instead of rendering a diff past its size limit, which bounds this read.
        diff = gh("api", f"repos/{repo}/pulls/{number}", "-H", "Accept: application/vnd.github.diff")
    except subprocess.CalledProcessError as error:
        print(f"::warning::no diff for {repo}#{number}: {error.stderr.strip()[:200]}")
        diff = NO_DIFF
    return {
        "repo": repo,
        "number": str(number),
        "author": pull["user"]["login"],
        "base": pull["base"]["ref"],
        "title": pull["title"],
        "body": (pull.get("body") or "(empty)")[:BODY_LIMIT],
        "files": "\n".join(entry["filename"] for entry in files) or "(none)",
        "diff": diff if len(diff) <= DIFF_LIMIT else diff[:DIFF_LIMIT] + CUT,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("triage", "code", "fix", "hooks", "review"))
    parser.add_argument("--repo", default="")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--pull", type=int)
    parser.add_argument("--findings", type=Path)
    parser.add_argument("--hooks-log", type=Path)
    parser.add_argument("--attachments", type=Path, help="directory to save the issue's images in")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    values = {"repo": args.repo}
    if args.issue:
        values |= issue_values(args.repo, args.issue, args.attachments)
    if args.pull:
        values |= pull_values(args.repo, args.pull)
    if args.findings:
        values["findings"] = args.findings.read_text(encoding="utf-8")
    if args.hooks_log:
        values["hooks_log"] = args.hooks_log.read_text(encoding="utf-8", errors="replace")[-LOG_LIMIT:]
    template = (PROMPTS / f"{args.kind}.md").read_text(encoding="utf-8")
    args.out.write_text(render(template, values), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
