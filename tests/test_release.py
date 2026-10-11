"""Releases come from the changelog, only where a repository asked, and only as text edits."""

import json
import subprocess
from pathlib import Path

import pytest

import release
from release import (
    Refusal,
    bump,
    configured,
    current,
    level,
    next_step,
    notes,
    prepare,
    render,
    rewrite,
    roll,
    unreleased,
    within,
)

CHANGELOG = """# Changelog

## [Unreleased]

### Fixed

- `read_text_file` returns the file.
- No bearer header to the content host.

## [2.6.0] - 2026-09-01

### Added

- Search.

## [2.5.0] - 2026-08-01

- First.
"""
BREAKING = CHANGELOG.replace("### Fixed", "### Changed\n\n- **BREAKING:** `path` is required.\n\n### Fixed")
EMPTY = CHANGELOG.replace(
    "### Fixed\n\n- `read_text_file` returns the file.\n- No bearer header to the content host.\n\n", ""
)


def test_releases_are_off_unless_a_topic_turns_them_on():
    assert configured([]) == ("off", "minor")
    assert configured(["mcp", "ashigaru-release-auto"]) == ("auto", "minor")
    assert configured(["ashigaru-release-pr", "ashigaru-release-max-major"]) == ("pr", "major")


def test_conflicting_topics_resolve_to_the_careful_one():
    topics = ["ashigaru-release-auto", "ashigaru-release-pr", "ashigaru-release-max-major",
              "ashigaru-release-max-patch"]  # fmt: skip
    assert configured(topics) == ("pr", "patch")


def test_the_changelog_gives_the_current_version_and_the_unreleased_entries():
    assert current(CHANGELOG) == "2.6.0"
    assert unreleased(CHANGELOG).startswith("### Fixed") and "Search" not in unreleased(CHANGELOG)
    assert unreleased(EMPTY) == "" and unreleased("# Changelog\n") == ""
    assert current("# Changelog\n\n## [Unreleased]\n") is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("### Fixed\n\n- a", "patch"),
        ("### Security\n\n- a", "patch"),
        ("### Added\n\n- a", "minor"),
        ("### Changed\n\n- a", "minor"),
        ("### Removed\n\n- a", "major"),
        ("### Fixed\n\n- **BREAKING:** a", "major"),
        ("### Fixed\n\n- Stops breaking on empty input.", "patch"),
    ],
)
def test_the_bump_level_is_read_from_the_unreleased_entries(body, expected):
    assert level(body) == expected


def test_the_next_version_follows_semver():
    assert bump("2.6.0", "patch") == "2.6.1"
    assert bump("2.6.3", "minor") == "2.7.0"
    assert bump("2.6.3", "major") == "3.0.0"
    assert bump("0.4.1", "major") == "0.5.0"


def test_a_bump_above_the_limit_is_not_within_it():
    assert within("patch", "minor") and within("minor", "minor") and within("major", "major")
    assert not within("major", "minor") and not within("minor", "patch")


def test_rolling_moves_the_entries_and_leaves_unreleased_empty():
    rolled = roll(CHANGELOG, "2.6.1", "2026-10-11")
    assert "## [Unreleased]\n\n## [2.6.1] - 2026-10-11\n\n### Fixed\n\n- `read_text_file`" in rolled
    assert unreleased(rolled) == "" and current(rolled) == "2.6.1"
    assert notes(rolled, "2.6.1") == unreleased(CHANGELOG)
    assert rolled.endswith("## [2.5.0] - 2026-08-01\n\n- First.\n")


def test_rolling_keeps_the_link_references_at_the_foot():
    foot = "[Unreleased]: https://github.com/o/r/compare/v2.6.0...HEAD\n[2.6.0]: https://github.com/o/r/releases/tag/v2.6.0\n"
    rolled = roll(CHANGELOG + "\n" + foot, "2.6.1", "2026-10-11")
    assert "[Unreleased]: https://github.com/o/r/compare/v2.6.1...HEAD\n" in rolled
    assert "[2.6.1]: https://github.com/o/r/releases/tag/v2.6.1\n[2.6.0]: " in rolled
    assert "compare" not in notes(rolled, "2.5.0")


def test_a_changelog_without_an_unreleased_heading_is_not_rolled():
    with pytest.raises(Refusal):
        roll("# Changelog\n\n## [1.0.0]\n", "1.0.1", "2026-10-11")


PROJECTS = {"mcp-pcloud-crunchtools"}


@pytest.mark.parametrize(
    "line",
    [
        'version = "2.6.0"\n',
        '  "version": "2.6.0",\n',
        '__version__ = "2.6.0"\n',
        '      version="2.6.0" \\\n',
        'mcp = FastMCP("mcp-pcloud-crunchtools", version="2.6.0")\n',
        "> **Version:** 2.6.0\n",
    ],
)
def test_a_line_that_states_the_version_is_moved(line):
    assert rewrite("any.txt", line, "2.6.0", "2.6.1", PROJECTS) == line.replace("2.6.0", "2.6.1")


@pytest.mark.parametrize(
    "line",
    [
        'dependencies = ["httpx>=2.6.0"]\n',
        "| 2.6.0 | 2026-09-01 | Manifest updated |\n",
        "podman pull quay.io/example/other:2.6.0\n",
        'version = "12.6.0"\n',
        'version = "2.6.0.1"\n',
        'version = "2.6.0-rc1"\n',
        'version = "2.6.01"\n',
    ],
)
def test_the_same_digits_elsewhere_are_left_alone(line):
    assert rewrite("any.txt", line, "2.6.0", "2.6.1", PROJECTS) == line


def test_a_file_that_holds_only_the_version_is_moved():
    assert rewrite("VERSION", "2.6.0\n", "2.6.0", "2.6.1", set()) == "2.6.1\n"


LOCK = """[[package]]
name = "httpx"
version = "2.6.0"

[[package]]
name = "mcp-pcloud-crunchtools"
version = "2.6.0"
source = { editable = "." }
"""
NPM_LOCK = """{
  "name": "mcp_pcloud.crunchtools",
  "version": "2.6.0",
  "packages": {
    "node_modules/left-pad": {
      "version": "2.6.0"
    }
  }
}
"""


@pytest.mark.parametrize(("name", "text"), [("uv.lock", LOCK), ("package-lock.json", NPM_LOCK)])
def test_in_a_lock_file_only_the_projects_own_entry_moves(name, text):
    moved = rewrite(name, text, "2.6.0", "2.6.1", PROJECTS)
    assert moved.count("2.6.1") == 1 and moved.count("2.6.0") == 1
    assert rewrite(name, text, "2.6.0", "2.6.1", set()) == text


def checkout(tmp_path: Path, changelog: str = CHANGELOG) -> Path:
    """A repository shaped like the fleet's MCP servers, at 2.6.0."""
    files = {
        "CHANGELOG.md": changelog,
        "pyproject.toml": '[project]\nname = "mcp-pcloud-crunchtools"\nversion = "2.6.0"\n'
        'dependencies = ["other==2.6.0"]\n',
        "uv.lock": LOCK,
        "src/pkg/__init__.py": '__version__ = "2.6.0"\n',
        "README.md": "Runs anywhere.\n",
        ".github/workflows/ci.yml": "env:\n  VERSION: 2.6.0\n",
        ".specify/memory/constitution.md": "> **Version:** 2.6.0\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for command in (["init", "--quiet"], ["add", "--all"]):
        subprocess.run(["git", "-C", str(tmp_path), *command], check=True)
    return tmp_path


def test_the_release_edits_every_file_that_states_the_version_and_nothing_else(tmp_path):
    root = checkout(tmp_path)
    changed = prepare(root, "2.6.0", "2.6.1", "2026-10-11")
    assert sorted(changed) == ["CHANGELOG.md", "pyproject.toml", "src/pkg/__init__.py", "uv.lock"]
    assert (root / "pyproject.toml").read_text().count("2.6.1") == 1
    assert "other==2.6.0" in (root / "pyproject.toml").read_text()
    assert "## [2.6.1] - 2026-10-11" in (root / "CHANGELOG.md").read_text()
    for untouched in (".github/workflows/ci.yml", ".specify/memory/constitution.md"):
        assert "2.6.0" in (root / untouched).read_text()


def test_a_manifest_that_does_not_parse_is_reported_and_its_lock_entry_left(tmp_path, capsys):
    root = checkout(tmp_path)
    (root / "pyproject.toml").write_text('[project\nversion = "2.6.0"\n')
    assert "uv.lock" not in prepare(root, "2.6.0", "2.6.1", "2026-10-11")
    assert "could not read pyproject.toml" in capsys.readouterr().out


def test_a_version_stated_in_no_file_is_refused_and_nothing_is_written(tmp_path):
    root = checkout(tmp_path)
    for name in ("pyproject.toml", "uv.lock", "src/pkg/__init__.py"):
        (root / name).write_text("nothing here\n")
    with pytest.raises(Refusal, match="stated in no tracked file"):
        prepare(root, "2.6.0", "2.6.1", "2026-10-11")
    assert (root / "CHANGELOG.md").read_text() == CHANGELOG


def test_a_changelog_that_moved_since_the_plan_is_refused(tmp_path):
    root = checkout(tmp_path, EMPTY)
    with pytest.raises(Refusal, match="changed while"):
        prepare(root, "2.6.0", "2.6.1", "2026-10-11")
    with pytest.raises(Refusal):
        prepare(root, "2.5.0", "2.5.1", "2026-10-11")


def status(mode="auto", limit="minor", entries=2):
    return {"repo": "o/r", "base": "main", "mode": mode, "limit": limit, "current": "2.6.0",
            "entries": entries, "action": None, "refused": "", "pulls": []}  # fmt: skip


def facts(**overrides):
    return {"tagged": True, "merged": None, "pulls": [], "settled": True} | overrides


def pull(number, branch, **overrides):
    return {"number": number, "headRefName": branch, "url": f"https://github.com/o/r/pull/{number}",
            "mergeable": "MERGEABLE", "autoMergeRequest": None} | overrides  # fmt: skip


def test_a_settled_repository_with_entries_is_due_a_release():
    step = next_step(status(), CHANGELOG, facts())
    assert step == {"action": "open", "level": "patch", "next": "2.6.1", "auto": True}


def test_pr_mode_opens_the_pull_request_without_auto_merge():
    assert next_step(status(mode="pr"), CHANGELOG, facts())["auto"] is False


def test_a_bump_above_the_limit_is_opened_but_never_merged_automatically():
    step = next_step(status(), BREAKING, facts())
    assert (step["next"], step["level"], step["auto"]) == ("3.0.0", "major", False)
    assert next_step(status(limit="major"), BREAKING, facts())["auto"] is True


@pytest.mark.parametrize(
    ("item", "found"),
    [
        (status(entries=0), facts()),
        (status(), facts(settled=False)),
        (status(), facts(pulls=[pull(7, "ashigaru/issue-31")])),
        (status(), facts(pulls=[pull(8, "ashigaru/release-2.6.1")])),
    ],
)
def test_nothing_is_due_while_empty_unsettled_or_with_ashigaru_work_open(item, found):
    assert not next_step(item, CHANGELOG, found).get("action")


def test_an_open_release_pull_request_is_listed_with_how_it_merges():
    waiting = pull(8, "ashigaru/release-2.6.1", autoMergeRequest={"mergeMethod": "SQUASH"})
    assert next_step(status(), CHANGELOG, facts(pulls=[waiting]))["pulls"] == [
        {"number": 8, "url": "https://github.com/o/r/pull/8", "version": "2.6.1", "auto": True}
    ]


def test_a_release_pull_request_that_no_longer_merges_is_retired():
    stuck = pull(8, "ashigaru/release-2.6.1", mergeable="CONFLICTING")
    step = next_step(status(), CHANGELOG, facts(pulls=[stuck]))
    assert (step["action"], step["url"]) == ("retire", stuck["url"])


def test_a_merged_release_without_its_tag_is_published_with_its_entry_as_notes():
    step = next_step(status(entries=0), CHANGELOG, facts(tagged=False, merged="abc123"))
    assert step == {"action": "publish", "sha": "abc123", "notes": "### Added\n\n- Search."}


def test_an_untagged_version_nobody_released_through_ashigaru_is_refused():
    step = next_step(status(), CHANGELOG, facts(tagged=False))
    assert "tag v2.6.0 does not exist" in step["refused"] and not step.get("action")


def surveying(monkeypatch, changelog):
    """Stub the API: one changelog, a tagged current version, a quiet default branch."""
    monkeypatch.setattr(release, "fetch", lambda repo, path: changelog)
    monkeypatch.setattr(release, "facts_of", lambda status, bot, before: facts())


def repo(*topics):
    return {"full_name": "o/r", "default_branch": "main", "topics": list(topics)}


def test_with_releases_off_a_repository_is_only_counted(monkeypatch):
    surveying(monkeypatch, CHANGELOG)
    monkeypatch.setattr(release, "facts_of", lambda *args: pytest.fail("asked the API about an off repo"))
    found = release.survey(repo(), "app/bot", "2026-10-11T00:00:00Z")
    assert (found["mode"], found["entries"], found["current"], found["action"]) == ("off", 2, "2.6.0", None)


@pytest.mark.parametrize(
    ("changelog", "reason"),
    [(None, "no CHANGELOG.md"), ("# Changelog\n\n## [Unreleased]\n\n- a\n", "names no released version")],
)
def test_a_repository_that_cannot_be_released_says_why(monkeypatch, changelog, reason):
    surveying(monkeypatch, changelog)
    found = release.survey(repo("ashigaru-release-auto"), "app/bot", "2026-10-11T00:00:00Z")
    assert reason in found["refused"] and found["action"] is None


def test_a_dry_run_names_no_repository_for_the_write_token_and_writes_no_issue(monkeypatch, tmp_path, capsys):
    surveying(monkeypatch, CHANGELOG)
    monkeypatch.setattr(
        release, "api_pages", lambda path: [repo("ashigaru-release-auto") | {"archived": False}]
    )
    monkeypatch.setattr(release, "enrolled", lambda name: True)
    monkeypatch.setattr(release, "published", lambda name, since: [])
    monkeypatch.setattr(release.inbox, "publish", lambda *args: pytest.fail("a dry run edited the record"))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    plan = tmp_path / "plan.json"
    monkeypatch.setattr("sys.argv", ["release.py", "plan", "--plan", str(plan), "--org", "o", "--dry-run"])
    assert release.main() == 0
    assert "repos=\n" in capsys.readouterr().out
    assert json.loads(plan.read_text())[0]["next"] == "2.6.1"
    monkeypatch.setattr(
        "sys.argv", ["release.py", "record", "--plan", str(plan), "--repo", "o/a", "--dry-run"]
    )
    assert release.main() == 0


def test_the_plan_names_the_repositories_that_need_the_write_token(monkeypatch, tmp_path, capsys):
    surveying(monkeypatch, CHANGELOG)
    quiet = {"full_name": "o/quiet", "default_branch": "main", "topics": [], "archived": False}
    second = repo("ashigaru-release-pr") | {"full_name": "o/s", "archived": False}
    listed = [repo("ashigaru-release-auto") | {"archived": False}, quiet, second]
    monkeypatch.setattr(release, "api_pages", lambda path: listed)
    monkeypatch.setattr(release, "enrolled", lambda name: True)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr("sys.argv", ["release.py", "plan", "--plan", str(tmp_path / "p.json"), "--org", "o"])
    assert release.main() == 0
    assert "repos=r,s\n" in capsys.readouterr().out


def test_a_repository_github_would_not_answer_for_is_recorded_not_dropped(monkeypatch):
    def failing(repo, path):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")

    monkeypatch.setattr(release, "fetch", failing)
    found = release.surveyed(repo("ashigaru-release-auto"), "app/bot", "2026-10-11T00:00:00Z")
    assert found["mode"] == "auto" and found["action"] is None
    assert "- r: GitHub did not answer" in render([found], [])


def closed(login="bot[bot]", base="main", merged_at="2026-10-11T00:00:00Z"):
    return {
        "user": {"login": login},
        "base": {"ref": base},
        "merged_at": merged_at,
        "merge_commit_sha": "abc123",
    }


def untagged(monkeypatch, pulls):
    """Stub the API for a repository whose current version has no tag."""

    def missing(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="gh: Not Found (HTTP 404)")

    asked = []
    monkeypatch.setattr(release, "gh", missing)
    monkeypatch.setattr(release, "api", lambda path: asked.append(path) or pulls)
    return asked


def test_only_a_release_pull_request_ashigaru_merged_into_the_default_branch_is_published(monkeypatch):
    asked = untagged(monkeypatch, [closed()])
    found = release.facts_of(status(), "app/bot", "2026-10-11T00:00:00Z")
    assert (found["tagged"], found["merged"]) == (False, "abc123")
    assert asked == ["repos/o/r/pulls?state=closed&head=o:ashigaru/release-2.6.0&per_page=10"]
    for other in (closed(login="mallory"), closed(base="side"), closed(merged_at=None)):
        untagged(monkeypatch, [other])
        assert release.facts_of(status(), "app/bot", "2026-10-11T00:00:00Z")["merged"] is None


def test_an_api_failure_is_not_read_as_a_missing_tag_or_changelog(monkeypatch):
    def failing(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")

    monkeypatch.setattr(release, "gh", failing)
    with pytest.raises(subprocess.CalledProcessError):
        release.tagged("o/r", "2.6.0")
    with pytest.raises(subprocess.CalledProcessError):
        release.fetch("o/r", "CHANGELOG.md")


def test_a_tagged_repository_reports_ashigarus_own_pull_requests_and_whether_it_is_quiet(monkeypatch):
    ours, fork = pull(7, "ashigaru/issue-7"), pull(8, "ashigaru/issue-8", isCrossRepository=True)
    ours["isCrossRepository"] = False
    commit = [{"commit": {"committer": {"date": "2026-10-10T23:50:00Z"}}}]
    monkeypatch.setattr(release, "gh", lambda *args: "")
    monkeypatch.setattr(
        release, "open_pulls", lambda name: [ours, fork, pull(9, "feature") | {"isCrossRepository": False}]
    )
    monkeypatch.setattr(release, "api", lambda path: commit)
    found = release.facts_of(status(), "app/bot", "2026-10-11T00:00:00Z")
    assert found == {"tagged": True, "merged": None, "pulls": [ours], "settled": True}
    assert release.facts_of(status(), "app/bot", "2026-10-10T23:40:00Z")["settled"] is False


def test_the_record_is_kept_under_its_own_label_and_title(monkeypatch, tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([status(mode="off", entries=1)]))
    kept = []
    monkeypatch.setattr(release, "published", lambda name, since: [])
    monkeypatch.setattr(release.inbox, "publish", lambda *args: kept.append(args) or "unchanged")
    monkeypatch.setattr("sys.argv", ["release.py", "record", "--plan", str(plan), "--repo", "o/ashigaru"])
    assert release.main() == 0
    assert kept[0][0] == "o/ashigaru" and kept[0][2:] == ("ashigaru-releases", "Releases")
    assert "- r: 1 entry since 2.6.0. Release mode off." in kept[0][1]


def test_one_repository_failing_does_not_stop_the_rest(monkeypatch):
    def failing(item, day):
        if item["repo"] == "o/a":
            raise subprocess.CalledProcessError(1, "gh", stderr="boom")
        if item["repo"] == "o/b":
            raise Refusal("version 1.0.0 is stated in no tracked file")
        item["done"] = True

    monkeypatch.setattr(release, "act", failing)
    items = [status() | {"repo": name, "action": "open"} for name in ("o/a", "o/b", "o/c")]
    release.carry_out([*items, status()], "2026-10-11")
    assert "failed" in items[0]["refused"] and "no tracked file" in items[1]["refused"]
    assert items[2].get("done") and not items[2]["refused"]


def test_the_record_lists_what_shipped_what_waits_and_what_was_refused():
    waiting = {"number": 8, "url": "u8", "version": "2.6.1", "auto": True}
    held = {"number": 9, "url": "u9", "version": "3.0.0", "auto": False}
    items = [
        status() | {"repo": "o/open", "pulls": [waiting]},
        status(mode="pr") | {"repo": "o/held", "pulls": [held]},
        status(mode="off", entries=1) | {"repo": "o/forgotten"},
        status(mode="off", entries=0) | {"repo": "o/quiet", "refused": "no CHANGELOG.md"},
        status(entries=0) | {"repo": "o/broken", "refused": "no CHANGELOG.md"},
    ]
    shipped = [{"repo": "o/open", "tag": "v2.6.0", "url": "r1", "day": "2026-10-09"}]
    body = render(items, shipped)
    assert body.startswith("<!-- ashigaru:releases -->\n")
    assert "- [open v2.6.0](r1), published 2026-10-09." in body
    assert "- [open#8](u8) release 2.6.1. Merges when its checks pass." in body
    assert "- [held#9](u9) release 3.0.0. Waiting on a maintainer." in body
    assert "- forgotten: 1 entry since 2.6.0. Release mode off." in body
    assert "## Could not release (1)\n\n- broken: no CHANGELOG.md." in body
    assert "quiet" not in body
    assert render(list(reversed(items)), shipped) == body
