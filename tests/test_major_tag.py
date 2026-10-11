"""The major tag moves forward only, and only for a release."""

import pytest

import major_tag

TAGS = ["v1.1.0", "v2", "v2.4.0", "v2.5.0", "v2.10.0", "v3.0.0-rc1", "vnext"]


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v2.10.0", "v2"),  # the newest of its major, compared as numbers
        ("v2.11.0", "v2"),  # newer than every tag listed
        ("v2.5.1", None),  # a late patch to an older minor
        ("v1.1.0", "v1"),  # each major has its own tag
        ("v3.0.0", "v3"),  # the first release of a major
        ("v3.0.0-rc1", None),
        ("v2", None),
        ("main", None),
    ],
)
def test_only_the_newest_release_of_a_major_moves_its_tag(tag, expected):
    assert major_tag.major_tag(tag, TAGS) == expected


def delivered(monkeypatch, tag):
    calls = []
    refs = [{"ref": f"refs/tags/{name}"} for name in TAGS]
    monkeypatch.setattr(major_tag, "api_pages", lambda path: refs)
    monkeypatch.setattr(major_tag, "api", lambda path: {"sha": "abcdef0123"})
    monkeypatch.setattr(major_tag, "gh", lambda *args: calls.append(args) or "")
    return major_tag.deliver("o/r", tag), calls


def test_an_existing_major_tag_is_moved_to_the_release(monkeypatch):
    said, calls = delivered(monkeypatch, "v2.11.0")
    assert calls == [
        ("api", "-X", "PATCH", "repos/o/r/git/refs/tags/v2", "-f", "sha=abcdef0123", "-F", "force=true")
    ]
    assert said == "v2 now points at v2.11.0 (abcdef0)"


def test_the_first_release_of_a_major_creates_its_tag(monkeypatch):
    _, calls = delivered(monkeypatch, "v3.0.0")
    assert calls == [
        ("api", "-X", "POST", "repos/o/r/git/refs", "-f", "ref=refs/tags/v3", "-f", "sha=abcdef0123")
    ]


def test_only_tags_of_the_same_major_are_listed(monkeypatch):
    asked = []
    monkeypatch.setattr(major_tag, "api_pages", lambda path: asked.append(path) or [])
    monkeypatch.setattr(major_tag, "api", lambda path: {"sha": "abcdef0123"})
    monkeypatch.setattr(major_tag, "gh", lambda *args: "")
    major_tag.deliver("o/r", "v2.6.0")
    assert asked == ["repos/o/r/git/matching-refs/tags/v2?per_page=100"]
    assert "not a release tag" in major_tag.deliver("o/r", "nightly") and len(asked) == 1


def test_an_older_release_moves_nothing(monkeypatch):
    said, calls = delivered(monkeypatch, "v2.5.1")
    assert calls == [] and "stays where it is" in said
