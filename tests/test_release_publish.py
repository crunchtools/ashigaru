"""A release pull request Ashigaru opened is published the moment it merges, and nothing else is."""

import subprocess

import pytest

import release
import release_publish

CHANGELOG = """# Changelog

## [Unreleased]

## [2.6.0] - 2026-09-01

### Added

- Search.

## [2.5.0] - 2026-08-01

- First.
"""


def merged_pull(login="bot[bot]", ref="ashigaru/release-2.6.0", head_repo="o/r", base="main", merged_at="t"):
    return {
        "number": 136,
        "user": {"login": login},
        "merged_at": merged_at,
        "merge_commit_sha": "5bffb79aa",
        "head": {"ref": ref, "repo": {"full_name": head_repo} if head_repo else None},
        "base": {"ref": base, "repo": {"default_branch": "main"}},
    }


def test_only_ashigarus_merged_release_pull_request_names_a_version_to_publish():
    assert release_publish.merged_release(merged_pull(), "o/r", "bot[bot]") == "2.6.0"
    for other in (
        merged_pull(login="mallory"),
        merged_pull(ref="ashigaru/issue-7"),
        merged_pull(head_repo="fork/r"),
        merged_pull(head_repo=None),
        merged_pull(base="side"),
        merged_pull(merged_at=None),
    ):
        assert release_publish.merged_release(other, "o/r", "bot[bot]") is None


def publishing(monkeypatch, pull, topics=("ashigaru-release-auto",), changelog=CHANGELOG, tag=False):
    """Stub the API for `release.py publish`; return the `gh` calls that write."""
    answers = {"repos/o/r/pulls/136": pull, "repos/o/r/topics": {"names": list(topics)}}
    written, fetched = [], []
    monkeypatch.setattr(release_publish, "api", lambda path: answers[path])
    monkeypatch.setattr(release_publish, "fetch", lambda repo, path, ref="": fetched.append(ref) or changelog)
    monkeypatch.setattr(release_publish, "tagged", lambda repo, version: tag)
    monkeypatch.setattr(release, "gh", lambda *args: written.append(args) or "")
    argv = ["release_publish.py", "--repo", "o/r", "--pull", "136", "--bot", "app/bot"]
    monkeypatch.setattr("sys.argv", argv)
    return written, fetched


def test_a_merged_release_pull_request_is_tagged_at_its_merge_commit_at_once(monkeypatch):
    """#166: gatehouse 0.26.0 merged at 09:07 and stayed untagged until a person cut it."""
    written, fetched = publishing(monkeypatch, merged_pull(login="bot[bot]"))
    assert release_publish.main() == 0
    assert fetched == ["5bffb79aa"]
    assert written == [("release", "create", "v2.6.0", "--repo", "o/r", "--target", "5bffb79aa",
                        "--title", "v2.6.0", "--notes", "### Added\n\n- Search.")]  # fmt: skip


@pytest.mark.parametrize(
    "case",
    [
        {"pull": merged_pull(login="mallory")},
        {"pull": merged_pull(login="bot[bot]"), "topics": ()},
        {"pull": merged_pull(login="bot[bot]"), "topics": ("ashigaru-release-auto", "ashigaru-observe")},
        {"pull": merged_pull(login="bot[bot]"), "tag": True},
    ],
)
def test_publish_on_merge_leaves_what_is_not_ashigarus_to_release(monkeypatch, case):
    written, _ = publishing(monkeypatch, **case)
    assert release_publish.main() == 0 and written == []


def test_a_release_whose_changelog_names_another_version_fails_instead_of_tagging(monkeypatch):
    pull = merged_pull(login="bot[bot]", ref="ashigaru/release-2.7.0")
    written, _ = publishing(monkeypatch, pull)
    assert release_publish.main() == 1 and written == []


def test_a_release_the_sweep_published_in_the_same_minute_is_not_a_failure(monkeypatch):
    publishing(monkeypatch, merged_pull(login="bot[bot]"))
    answers = iter((False, True))
    monkeypatch.setattr(release_publish, "tagged", lambda repo, version: next(answers))

    def taken(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 422: tag_name already exists")

    monkeypatch.setattr(release, "gh", taken)
    assert release_publish.main() == 0


def test_a_publish_that_fails_with_no_tag_made_fails_the_run(monkeypatch):
    publishing(monkeypatch, merged_pull(login="bot[bot]"))

    def refused(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403")

    monkeypatch.setattr(release, "gh", refused)
    with pytest.raises(subprocess.CalledProcessError):
        release_publish.main()
