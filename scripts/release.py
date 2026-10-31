#!/usr/bin/env python3
"""Cut releases from merged work: the release pull request, then the tag and the GitHub Release.

Ashigaru's fixes merge on their own, and a merged fix that is never released
helps nobody. Each hour this looks at the enrolled repositories that asked for
releases (a repository topic), opens a release pull request where the changelog
has unreleased entries, and publishes the release once that pull request has
merged. It keeps one issue listing what was released and what is waiting.

Three steps, so the token that can write to a repository is minted only for the
repositories that need it this hour: `plan` reads, `act` writes, `record`
rewrites the issue. Nothing here calls a model, and no repository code runs:
the edits are text replacements in a fresh clone.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import inbox
from common import BRANCH_PREFIX, RELEASES, api, api_pages, gh, git, set_output
from publish import BOT_EMAIL, BOT_NAME, CONFLICTING, announce
from sweep import SETTLE, enrolled, open_pulls, stamp

OFF, PULL, AUTO = "off", "pr", "auto"
MODES = {f"ashigaru-release-{mode}": mode for mode in (PULL, AUTO)}
"""The repository topic that turns releases on. Only a repository admin can set one."""
LEVELS = ("patch", "minor", "major")
LIMITS = {f"ashigaru-release-max-{level}": level for level in LEVELS}
"""The topic that sets the largest bump merged without a person."""
DEFAULT_LIMIT = "minor"
WINDOW = timedelta(days=7)
"""How long a published release stays on the record."""
MAX_BYTES = 1_000_000
"""Files larger than this are not searched for the version."""
WORKERS = 8
"""Repositories read at a time."""
MARKER = "<!-- ashigaru:releases -->"
TITLE = "Releases"
INTRO = (
    "What was released across the organization in the last seven days, and what is waiting to be. "
    "The hourly sweep rewrites this when something changes."
)
FOOTER = "Opened by Ashigaru. Merging this pull request publishes the release."

UNRELEASED = re.compile(r"^## \[Unreleased\][^\n]*\n?", re.MULTILINE | re.IGNORECASE)
RELEASED = re.compile(r"^## \[v?(\d+\.\d+\.\d+)\]", re.MULTILINE)
BOUNDARY = re.compile(r"^(?:## |\[[^\]\n]+\]: \S)", re.MULTILINE)
"""Where a changelog section ends: the next version heading, or the link references at the foot."""
ENTRY = re.compile(r"^\s*[-*] ", re.MULTILINE)
BREAKING = re.compile(r"\bBREAKING\b|^### Removed\b", re.MULTILINE)
FEATURE = re.compile(r"^### (?:Added|Changed|Deprecated)\b", re.MULTILINE)
COMPARE = re.compile(
    r"^\[Unreleased\]: (\S+?)/compare/v?\d+\.\d+\.\d+\.\.\.HEAD$", re.MULTILINE | re.IGNORECASE
)
STATES = re.compile(r"(?<![\w-])(?:__version__|version)(?![\w-])", re.IGNORECASE)
"""The word `version` standing alone: a key, a label or a heading, not part of a longer name."""
QUOTED = re.compile(r'"([^"]+)"')
LOCK_FILES = ("package-lock.json",)
MANIFESTS = (
    ("pyproject.toml", ("project", "name")),
    ("pyproject.toml", ("tool", "poetry", "name")),
    ("Cargo.toml", ("package", "name")),
    ("package.json", ("name",)),
)
"""Where a project states its own name, which is how its entry in a lock file is found."""


class Refusal(Exception):
    """Why a repository that is due a release did not get one."""


def configured(topics: list[str]) -> tuple[str, str]:
    """A repository's release mode and bump limit, read from its topics.

    Conflicting topics resolve to the more careful one: `pr` over `auto`, the lower limit.
    """
    modes = {MODES[topic] for topic in topics if topic in MODES}
    limits = [LIMITS[topic] for topic in topics if topic in LIMITS]
    mode = PULL if PULL in modes else AUTO if modes else OFF
    return mode, min(limits, key=LEVELS.index) if limits else DEFAULT_LIMIT


def span(changelog: str, start: int) -> int:
    """Where the section that starts at `start` ends."""
    found = BOUNDARY.search(changelog, start)
    return found.start() if found else len(changelog)


def unreleased(changelog: str) -> str:
    """The body under `## [Unreleased]`, empty when there is no such heading."""
    found = UNRELEASED.search(changelog)
    return changelog[found.end() : span(changelog, found.end())].strip() if found else ""


def current(changelog: str) -> str | None:
    """The newest released version: the first `## [X.Y.Z]` heading."""
    found = RELEASED.search(changelog)
    return found[1] if found else None


def notes(changelog: str, version: str) -> str:
    """The changelog entry of one released version, without its heading."""
    found = re.search(rf"^## \[v?{re.escape(version)}\][^\n]*\n", changelog, re.MULTILINE)
    return changelog[found.end() : span(changelog, found.end())].strip() if found else ""


def level(body: str) -> str:
    """How large a bump the unreleased entries call for.

    The word BREAKING or a `### Removed` heading is major; `### Added`, `### Changed`
    or `### Deprecated` is minor; anything else (fixes, security) is patch.
    """
    if BREAKING.search(body):
        return "major"
    return "minor" if FEATURE.search(body) else "patch"


def bump(version: str, step: str) -> str:
    """The next version. Before 1.0.0 a breaking change moves the minor, as SemVer allows."""
    major, minor, patch = (int(part) for part in version.split("."))
    if step == "major" and major:
        return f"{major + 1}.0.0"
    if step in ("major", "minor"):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def within(step: str, limit: str) -> bool:
    """True when a bump of this size may merge without a person."""
    return LEVELS.index(step) <= LEVELS.index(limit)


def roll(changelog: str, version: str, day: str) -> str:
    """Move the unreleased entries under a heading for `version` and leave `[Unreleased]` empty."""
    found = UNRELEASED.search(changelog)
    if not found:
        raise Refusal("CHANGELOG.md has no [Unreleased] heading")
    end = span(changelog, found.end())
    body = changelog[found.end() : end].strip()
    head = changelog[: found.end()].rstrip("\n")
    rolled = f"{head}\n\n## [{version}] - {day}\n\n{body}\n\n{changelog[end:]}".rstrip() + "\n"
    return COMPARE.sub(
        lambda link: (
            f"[Unreleased]: {link[1]}/compare/v{version}...HEAD\n"
            f"[{version}]: {link[1]}/releases/tag/v{version}"
        ),
        rolled,
    )


def normal(name: str) -> str:
    """A package name as lock files spell it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def rewrite(name: str, text: str, old: str, new: str, projects: set[str]) -> str:
    """One file's text with the project's version moved from `old` to `new`.

    Only where the file says it is the version: a file that holds nothing else, or a
    line that carries the word on its own (`version`, `__version__`), which leaves
    `minimum_version` and `python-version` alone. In a lock file, only the version line
    that follows the project's own name, so a dependency at the same version stays.
    """
    if text.strip() == old:
        return text.replace(old, new)
    exact = re.compile(rf"(?<![\d.]){re.escape(old)}(?!\.?\d)(?![-+]\w)")
    locked = name.endswith(".lock") or name in LOCK_FILES
    lines = text.splitlines(keepends=True)
    previous = ""
    for index, line in enumerate(lines):
        named = {normal(quoted) for quoted in QUOTED.findall(previous)} & projects
        if STATES.search(line) and (named or not locked):
            lines[index] = exact.sub(new, line)
        if line.strip():
            previous = line
    return "".join(lines)


def skipped(name: str) -> bool:
    """Files the release never edits: the changelog (rolled separately) and anything under a dot directory.

    `.github/` is the maintainers', and other dot directories hold tool state whose
    version numbers are not the project's.
    """
    return name == "CHANGELOG.md" or any(part.startswith(".") for part in Path(name).parts[:-1])


def declared(path: Path, keys: tuple[str, ...]) -> str | None:
    """The value at `keys` in a TOML or JSON manifest, or None when it is not there."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        found = json.loads(raw) if path.suffix == ".json" else tomllib.loads(raw)
    except (OSError, ValueError) as error:
        # Without the project's name its lock file entry stays put, and the lock check in CI says so.
        print(f"::warning::could not read {path.name} for the project's name: {error}")
        return None
    for key in keys:
        found = found.get(key) if isinstance(found, dict) else None
    return found if isinstance(found, str) else None


def projects(root: Path) -> set[str]:
    """The names this repository's own package goes by."""
    names = (declared(root / name, keys) for name, keys in MANIFESTS)
    return {normal(name) for name in names if name}


def edit(root: Path, old: str, new: str) -> list[str]:
    """Move the version in every tracked file that states it. Returns the files changed."""
    names = projects(root)
    changed = []
    for name in git("-C", str(root), "ls-files", "-z").split("\0"):
        path = root / name
        if not name or skipped(name) or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > MAX_BYTES:
            continue
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                text = handle.read()
        except UnicodeDecodeError:
            continue
        updated = rewrite(path.name, text, old, new, names)
        if updated != text:
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
            changed.append(name)
    return changed


def prepare(root: Path, old: str, new: str, day: str) -> list[str]:
    """Make the release edits in a checkout. Raises Refusal when there is nothing sound to release."""
    log = root / "CHANGELOG.md"
    text = log.read_text(encoding="utf-8") if log.is_file() else ""
    if current(text) != old or not ENTRY.search(unreleased(text)):
        raise Refusal("the changelog changed while the release was being prepared")
    changed = edit(root, old, new)
    if not changed:
        raise Refusal(f"version {old} is stated in no tracked file")
    log.write_text(roll(text, new, day), encoding="utf-8")
    return [*changed, "CHANGELOG.md"]


def next_step(status: dict, text: str, facts: dict) -> dict:
    """What to do for a repository that asked for releases, given what the API said about it.

    `facts`: `tagged` (the current version has its tag), `merged` (the commit a merged
    release pull request for it landed as, if any), `pulls` (Ashigaru's open pull
    requests), `settled` (the default branch has been quiet for SETTLE).
    """
    version = status["current"]
    if not facts["tagged"]:
        if facts["merged"]:
            return {"action": "publish", "sha": facts["merged"], "notes": notes(text, version)}
        return {"refused": f"CHANGELOG.md is at {version} but tag v{version} does not exist"}
    releases = [pull for pull in facts["pulls"] if pull["headRefName"].startswith(inbox.RELEASE_BRANCH)]
    listed = {"pulls": [listing(pull) for pull in releases]}
    conflicts = [pull for pull in releases if pull["mergeable"] == CONFLICTING]
    if conflicts:
        return listed | {"action": "retire", "url": conflicts[0]["url"]}
    if not status["entries"] or facts["pulls"] or not facts["settled"]:
        return listed
    step = level(unreleased(text))
    auto = status["mode"] == AUTO and within(step, status["limit"])
    return {"action": "open", "level": step, "next": bump(version, step), "auto": auto}


def listing(pull: dict) -> dict:
    """An open release pull request as the record lists it."""
    return {
        "number": pull["number"],
        "url": pull["url"],
        "version": pull["headRefName"].removeprefix(inbox.RELEASE_BRANCH),
        "auto": bool(pull.get("autoMergeRequest")),
        "failing": inbox.failing(pull.get("statusCheckRollup") or []),
    }


def fetch(repo: str, path: str) -> str | None:
    """A file from the default branch, or None when the repository has none."""
    try:
        return gh("api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw")
    except subprocess.CalledProcessError as error:
        if "404" not in error.stderr:
            raise
        return None


def tagged(repo: str, version: str) -> bool:
    try:
        gh("api", f"repos/{repo}/git/ref/tags/v{version}", "--silent")
    except subprocess.CalledProcessError as error:
        if "404" not in error.stderr:
            raise
        return False
    return True


def merged(status: dict, login: str) -> str | None:
    """The commit Ashigaru's release pull request for the current version merged as, if it did."""
    repo, branch = status["repo"], f"{inbox.RELEASE_BRANCH}{status['current']}"
    owner = repo.split("/", 1)[0]
    for pull in api(f"repos/{repo}/pulls?state=closed&head={owner}:{branch}&per_page=10"):
        ours = pull["user"]["login"] == login and pull["base"]["ref"] == status["base"]
        if ours and pull["merged_at"]:
            return pull["merge_commit_sha"]
    return None


def facts_of(status: dict, bot: str, before: str) -> dict:
    """Ask the API what `next_step` needs to know, and no more than the answer so far calls for."""
    repo = status["repo"]
    if not tagged(repo, status["current"]):
        login = f"{bot.removeprefix('app/')}[bot]"
        return {"tagged": False, "merged": merged(status, login), "pulls": [], "settled": False}
    pulls = [
        pull
        for pull in open_pulls(repo)
        if pull["headRefName"].startswith(BRANCH_PREFIX) and not pull["isCrossRepository"]
    ]
    newest = api(f"repos/{repo}/commits?per_page=1")[0]["commit"]["committer"]["date"]
    return {"tagged": True, "merged": None, "pulls": pulls, "settled": newest < before}


def survey(repo: dict, bot: str, before: str) -> dict:
    """Where one enrolled repository stands: its version, its unreleased entries, and what is due."""
    mode, limit = configured(repo.get("topics") or [])
    status = {
        "repo": repo["full_name"], "base": repo["default_branch"], "mode": mode, "limit": limit,
        "current": None, "entries": 0, "action": None, "refused": "", "pulls": [],
    }  # fmt: skip
    text = fetch(status["repo"], "CHANGELOG.md")
    if text is None:
        return status | {"refused": "no CHANGELOG.md"}
    status |= {"current": current(text), "entries": len(ENTRY.findall(unreleased(text)))}
    if mode == OFF:
        return status
    if not status["current"]:
        return status | {"refused": "CHANGELOG.md names no released version"}
    return status | next_step(status, text, facts_of(status, bot, before))


def open_release(item: dict, day: str) -> dict:
    """Branch, edit, push and open the release pull request. Returns it as the record lists it."""
    repo, old, new = item["repo"], item["current"], item["next"]
    branch = f"{inbox.RELEASE_BRANCH}{new}"
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "repo"
        git("clone", "--quiet", "--depth", "1", "--branch", item["base"],
            f"https://github.com/{repo}.git", str(root))  # fmt: skip
        changed = prepare(root, old, new, day)
        entry = notes((root / "CHANGELOG.md").read_text(encoding="utf-8"), new)
        print(f"    edited {', '.join(changed)}")
        git("-C", str(root), "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
            "commit", "--quiet", "--all", "--message", f"chore: release {new}")  # fmt: skip
        git("-C", str(root), "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}")
    title = f"chore: release {new}"
    if item["auto"]:
        url = announce(repo, branch, item["base"], title, f"{entry}\n\n{FOOTER}\n")
    else:
        above = f"a {item['level']} bump is above this repository's limit ({item['limit']})"
        why = above if item["mode"] == AUTO else "this repository asks for a release pull request only"
        held = f"Not set to merge on its own: {why}."
        url = gh("pr", "create", "--repo", repo, "--head", branch, "--base", item["base"],
                 "--title", title, "--body", f"{entry}\n\n{FOOTER} {held}\n").strip()  # fmt: skip
    return {"number": int(url.rsplit("/", 1)[-1]), "url": url, "version": new, "auto": item["auto"]}


def act(item: dict, day: str) -> None:
    """Carry out one repository's planned step and note the outcome on the item."""
    repo, version = item["repo"], item["current"]
    if item["action"] == "publish":
        gh("release", "create", f"v{version}", "--repo", repo, "--target", item["sha"],
           "--title", f"v{version}", "--notes", item["notes"] or f"Release {version}.")  # fmt: skip
        print(f"  published {repo} v{version}")
    elif item["action"] == "retire":
        gh("pr", "close", item["url"], "--delete-branch", "--comment",
           "**Ashigaru closed this release pull request**: it no longer merges. "
           "A fresh one follows from the current default branch.")  # fmt: skip
        item["pulls"] = [pull for pull in item["pulls"] if pull["url"] != item["url"]]
        print(f"  retired {item['url']}")
    elif item["action"] == "open":
        item["pulls"] = [open_release(item, day)]
        print(f"  opened {item['pulls'][0]['url']}")


def carry_out(items: list[dict], day: str) -> None:
    """Act on every planned step. One repository failing does not stop the rest."""
    for item in items:
        if not item["action"]:
            continue
        try:
            act(item, day)
        except Refusal as why:
            item["refused"] = str(why)
            print(f"  refused {item['repo']}: {why}")
        except subprocess.CalledProcessError as error:
            item["refused"] = "the release step failed; the sweep log has the error"
            print(f"::warning::release step failed for {item['repo']}: {error.stderr.strip()}")


def published(repo: str, since: str) -> list[dict]:
    """Releases one repository published since `since`, whoever cut them."""
    return [
        {
            "repo": repo,
            "tag": release["tag_name"],
            "url": release["html_url"],
            "day": release["published_at"][:10],
        }  # fmt: skip
        for release in api(f"repos/{repo}/releases?per_page=30")
        if not release["draft"] and (release["published_at"] or "") >= since
    ]


def short(repo: str) -> str:
    return repo.split("/", 1)[-1]


def pull_row(item: dict, pull: dict) -> str:
    """One open release pull request. Auto-merge only waits on checks, so a failed one is named."""
    if pull.get("failing"):
        fate = inbox.blocked(pull["failing"])
    else:
        fate = "Merges when its checks pass." if pull["auto"] else "Waiting on a maintainer."
    return f"- [{short(item['repo'])}#{pull['number']}]({pull['url']}) release {pull['version']}. {fate}"


def backlog_row(item: dict) -> str:
    since = f" since {item['current']}" if item["current"] else ""
    count = f"{item['entries']} {'entry' if item['entries'] == 1 else 'entries'}"
    return f"- {short(item['repo'])}: {count}{since}. Release mode {item['mode']}."


def render(items: list[dict], shipped: list[dict]) -> str:
    """The body of the releases issue. No timestamps: an unchanged record renders the same."""
    items = sorted(items, key=lambda item: item["repo"])
    lines = [MARKER, INTRO, ""]
    lines += inbox.section(
        "Published in the last seven days",
        [
            f"- [{short(release['repo'])} {release['tag']}]({release['url']}), published {release['day']}."
            for release in sorted(
                shipped, key=lambda release: (release["day"], release["repo"]), reverse=True
            )
        ],
        "Nothing published.",
    )
    lines += inbox.section(
        "Release pull requests open",
        [pull_row(item, pull) for item in items for pull in item["pulls"]],
        "None open.",
    )
    lines += inbox.section(
        "Merged and not released",
        [backlog_row(item) for item in items if item["entries"] and not item["pulls"]],
        "Nothing unreleased.",
    )
    lines += inbox.section(
        "Could not release",
        [
            f"- {short(item['repo'])}: {item['refused']}."
            for item in items
            if item["refused"] and item["mode"] != OFF
        ],
        "No refusals.",
    )
    return "\n".join(lines).rstrip() + "\n"


def surveyed(repo: dict, bot: str, before: str) -> dict:
    """One repository's status. If the API would not answer, the record says so; the next sweep retries."""
    try:
        return survey(repo, bot, before)
    except subprocess.CalledProcessError as error:
        print(f"::warning::could not survey {repo['full_name']}: {error.stderr.strip()}")
        mode, limit = configured(repo.get("topics") or [])
        return {
            "repo": repo["full_name"], "base": repo["default_branch"], "mode": mode, "limit": limit,
            "current": None, "entries": 0, "action": None, "pulls": [],
            "refused": "GitHub did not answer when its state was read",
        }  # fmt: skip


def plan(args: argparse.Namespace) -> int:
    before = stamp(datetime.now(UTC) - SETTLE)
    repos = [
        repo for repo in api_pages(f"orgs/{args.org}/repos?type=public&per_page=100") if not repo["archived"]
    ]
    # Each repository costs a few requests and none depends on another's.
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        answers = pool.map(lambda repo: enrolled(repo["full_name"]), repos)
        kept = [repo for repo, yes in zip(repos, answers, strict=True) if yes]
        items = list(pool.map(lambda repo: surveyed(repo, args.bot, before), kept))
    due = [item for item in items if item["action"]]
    active = sum(item["mode"] != OFF for item in items)
    print(f"release: {len(items)} enrolled repo(s), {active} with releases on, {len(due)} step(s) due")
    for item in due:
        print(f"  {item['action']} {item['repo']} {item.get('next') or item['current']}")
    args.plan.write_text(json.dumps(items), encoding="utf-8")
    # The names go to the step that mints the write token, so it covers these repositories only.
    set_output("repos", "" if args.dry_run else ",".join(short(item["repo"]) for item in due))
    return 0


def carry(args: argparse.Namespace) -> int:
    items = json.loads(args.plan.read_text(encoding="utf-8"))
    gh("auth", "setup-git")
    carry_out(items, datetime.now(UTC).strftime("%Y-%m-%d"))
    args.plan.write_text(json.dumps(items), encoding="utf-8")
    return 0


def record(args: argparse.Namespace) -> int:
    items = json.loads(args.plan.read_text(encoding="utf-8"))
    since = stamp(datetime.now(UTC) - WINDOW)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        found = pool.map(lambda item: published(item["repo"], since), items)
    shipped = [release for releases in found for release in releases]
    body = render(items, shipped)
    if args.dry_run:
        print(body)
        return 0
    print(f"release: record {inbox.publish(args.repo, body, RELEASES, TITLE)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("plan", "act", "record"))
    parser.add_argument("--plan", required=True, type=Path, help="where the plan is kept between steps")
    parser.add_argument("--org", default="")
    parser.add_argument("--bot", default="app/ashigaru-crunchtools")
    parser.add_argument("--repo", default="", help="repo whose releases issue is the record")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return {"plan": plan, "act": carry, "record": record}[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
