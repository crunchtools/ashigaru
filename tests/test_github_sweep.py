"""The sweep's two read-only GitHub calls, against a fake API."""

import io
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from ashigaru.desk.forges import github
from ashigaru.desk.model import Kind

REPO = "crunchtools/ashigaru"
TIMEOUT = 10
PAGE = 100
SINCE = "2026-11-19T10:00:00Z"
LAST = 3
SHORT = 5
SHA = "ABC" + "d" * 37


def install(monkeypatch: pytest.MonkeyPatch, route: Callable[[str], Any]) -> list[str]:
    asked: list[str] = []

    def fake(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        assert request.get_header("Authorization") == "Bearer token" and timeout == TIMEOUT
        assert request.get_method() == "GET"
        asked.append(request.full_url)
        return io.BytesIO(json.dumps(route(request.full_url)).encode())

    monkeypatch.setattr(github.urllib.request, "urlopen", fake)
    return asked


def issue(number: int = 7, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Do a thing",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "lane:code"}],
        "user": {"login": "alice"},
        "updated_at": "2026-11-19T11:00:00Z",
        **extra,
    }


def test_subject_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = install(monkeypatch, lambda url: issue())
    item = github.FORGE.subject(REPO, 7, "token")
    assert asked == [f"{github.API}/repos/{REPO}/issues/7"]
    assert (item.source, item.kind, item.repo) == ("github", Kind.OUTCOME, REPO)
    assert item.subject_key == f"{REPO}#7"
    assert item.title == "Do a thing" and item.ref.endswith("/issues/7")
    assert item.labels == ("bug", "lane:code") and item.lane_hint == "code"
    assert item.author == "alice" and item.updated_at == "2026-11-19T11:00:00Z"
    assert not (item.closed or item.merged) and item.draft is None and item.head is None
    assert item.sender is None and item.approves_lane is None
    assert item.revokes_lane is None and item.head_before is None


def pull(**extra: Any) -> dict[str, Any]:
    return {
        **issue(),
        "html_url": f"https://github.com/{REPO}/pull/7",
        "state": "closed",
        "merged": True,
        "merged_at": "2026-11-19T11:00:00Z",
        "draft": False,
        "head": {"sha": SHA},
        **extra,
    }


def test_subject_merged_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str) -> Any:
        return issue(pull_request={}) if url.endswith("/issues/7") else pull()

    asked = install(monkeypatch, route)
    item = github.FORGE.subject(REPO, 7, "token")
    assert asked == [f"{github.API}/repos/{REPO}/issues/7", f"{github.API}/repos/{REPO}/pulls/7"]
    assert item.kind is Kind.STORY and item.ref.endswith("/pull/7")
    assert item.closed and item.merged and item.draft is False and item.head == SHA.lower()
    assert item.lane_hint == "code"


def test_subject_draft_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str) -> Any:
        if "/issues/" in url:
            return issue(pull_request={})
        return pull(state="open", merged=False, merged_at=None, draft=True)

    install(monkeypatch, route)
    item = github.FORGE.subject(REPO, 7, "token")
    assert item.draft is True and not item.merged and not item.closed


@pytest.mark.parametrize("repo", ["nope", "Crunchtools/ashigaru", "a/b/c", ""])
def test_subject_bad_repo(monkeypatch: pytest.MonkeyPatch, repo: str) -> None:
    asked = install(monkeypatch, lambda url: issue())
    with pytest.raises(ValueError):
        github.FORGE.subject(repo, 1, "token")
    assert asked == []


def hit(number: int, **extra: Any) -> dict[str, Any]:
    return {**issue(number), "repository_url": f"{github.API}/repos/CrunchTools/Ashigaru", **extra}


def query(url: str) -> dict[str, list[str]]:
    assert url.startswith(f"{github.API}/search/issues?")
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def test_updated_fields_and_query(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        hit(1, author_association="MEMBER", body="text"),
        hit(2, author_association="NONE", body=None),
        hit(3, pull_request={"merged_at": "2026-11-19T12:00:00Z"}, draft=False, state="closed"),
        hit(4, pull_request={"merged_at": None}, draft=True),
    ]
    asked = install(monkeypatch, lambda url: {"items": items})
    found = github.FORGE.updated("crunchtools", SINCE, "token").hits[: len(items)]
    assert len(asked) == len(github.SEARCHED)
    assert [query(url)["q"][0] for url in asked] == [
        f"org:crunchtools is:issue updated:>={SINCE}",
        f"org:crunchtools is:pr updated:>={SINCE}",
    ]
    sent = query(asked[0])
    assert sent["sort"] == ["updated"] and sent["order"] == ["asc"]
    assert sent["per_page"] == [str(PAGE)] and sent["page"] == ["1"]
    assert "%3E%3D" in asked[0] and " " not in asked[0]
    first, second, merged, draft = found
    assert (first.association, first.body) == ("MEMBER", "text")
    assert (second.association, second.body) == ("NONE", "")
    assert first.item.kind is Kind.OUTCOME and first.item.repo == REPO and first.item.head is None
    assert first.item.subject_key == f"{REPO}#1" and first.item.lane_hint == "code"
    assert merged.item.kind is Kind.STORY and merged.item.merged and merged.item.closed
    assert draft.item.kind is Kind.STORY and not draft.item.merged and draft.item.draft is True
    assert draft.item.updated_at == "2026-11-19T11:00:00Z" and draft.item.author == "alice"


def test_updated_paginates_until_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str) -> Any:
        page = int(query(url)["page"][0])
        count = PAGE if page < LAST else SHORT
        return {"items": [hit(page * 1000 + n) for n in range(count)]}

    asked = install(monkeypatch, route)
    search = github.FORGE.updated("crunchtools", SINCE, "token")
    pages = [str(n) for n in range(1, LAST + 1)]
    assert [query(url)["page"][0] for url in asked] == pages * len(github.SEARCHED)
    assert len(search.hits) == (PAGE * (LAST - 1) + SHORT) * len(github.SEARCHED) and search.complete
    assert search.reached is None


def test_updated_stops_at_ten_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = install(monkeypatch, lambda url: {"items": [hit(n) for n in range(PAGE)]})
    search = github.FORGE.updated("crunchtools", SINCE, "token")
    assert len(asked) == github.SEARCH_PAGES * len(github.SEARCHED) and not search.complete


def test_updated_merges_both_searches_and_reaches_the_lower_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str) -> Any:
        sent = query(url)
        if "is:issue" in sent["q"][0]:
            return {"items": [hit(n, updated_at="2026-11-19T12:00:00Z") for n in range(PAGE)]}
        if sent["page"] == ["1"]:
            return {"items": [hit(5000 + n, updated_at="2026-11-19T11:00:00Z") for n in range(PAGE)]}
        return {"items": [hit(6000, updated_at="2026-11-19T11:30:00Z")]}

    install(monkeypatch, route)
    search = github.FORGE.updated("crunchtools", SINCE, "token")
    assert not search.complete and search.reached == "2026-11-19T12:00:00Z"
    stamps = [found.item.updated_at or "" for found in search.hits]
    assert stamps == sorted(stamps) and len(stamps) == PAGE * github.SEARCH_PAGES + PAGE + 1


def test_updated_reaches_the_lower_of_two_cuts(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str) -> Any:
        stamp = "2026-11-19T12:00:00Z" if "is:issue" in query(url)["q"][0] else "2026-11-19T11:00:00Z"
        return {"items": [hit(n, updated_at=stamp) for n in range(PAGE)]}

    install(monkeypatch, route)
    assert github.FORGE.updated("crunchtools", SINCE, "token").reached == "2026-11-19T11:00:00Z"


def test_updated_skips_bad_repository_url(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        hit(1, repository_url="https://example.com/elsewhere"),
        hit(2, repository_url=None),
        hit(3, repository_url=f"{github.API}/repos/a/b/c"),
        hit(4),
    ]
    install(monkeypatch, lambda url: {"items": items})
    found = github.FORGE.updated("crunchtools", SINCE, "token").hits
    assert [f.item.subject_key for f in found] == [f"{REPO}#4"] * len(github.SEARCHED)


@pytest.mark.parametrize("org", ["", "-x", "a b", "a/b", "bot[bot]"])
def test_updated_bad_org(monkeypatch: pytest.MonkeyPatch, org: str) -> None:
    asked = install(monkeypatch, lambda url: {"items": []})
    with pytest.raises(ValueError):
        github.FORGE.updated(org, SINCE, "token")
    assert asked == []
