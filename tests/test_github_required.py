"""GitHub's required status checks and what a commit has reported, against a fake API."""

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from ashigaru.desk.forges import github
from ashigaru.desk.model import Verdict

REPO = "crunchtools/ashigaru"
SHA = "a" * 40
TIMEOUT = 5
TOTAL = 101
PAGES = 2


class Api:
    """GitHub's API in urlopen's place: URL path (without query) to a body, or to an exception."""

    def __init__(self, **routes: Any) -> None:
        self.routes: dict[str, Any] = {"/" + k.replace("__", "/").strip("/"): v for k, v in routes.items()}
        self.asked: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        assert request.get_header("Authorization") == "Bearer token" and timeout == TIMEOUT
        self.asked.append(request.full_url)
        path = request.full_url.removeprefix(f"{github.API}/repos/{REPO}").partition("?")[0] or "/"
        body = self.routes.get(path, {})
        if callable(body):
            body = body(request.full_url)
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(json.dumps(body).encode())


def install(monkeypatch: pytest.MonkeyPatch, **routes: Any) -> Api:
    fake = Api(**routes)
    monkeypatch.setattr(github.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_rules", {})


def ruleset(*contexts: str) -> list[dict[str, Any]]:
    return [
        {"type": "deletion", "ruleset_source_type": "Organization"},
        {
            "type": "required_status_checks",
            "ruleset_source_type": "Organization",
            "ruleset_source": "crunchtools",
            "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
        },
    ]


def protected(contexts: list[str] | None = None, checks: list[str] | None = None) -> dict[str, Any]:
    classic: dict[str, Any] = {}
    if contexts is not None:
        classic["contexts"] = contexts
    if checks is not None:
        classic["checks"] = [{"context": c, "app_id": 1} for c in checks]
    return {"protected": True, "protection": {"required_status_checks": classic}}


def test_org_ruleset_check_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    api = install(monkeypatch, rules__branches__main=ruleset("sdlc ci"), branches__main={"protected": False})
    assert github.required_checks("CrunchTools/Ashigaru", "main", "token") == ("main", ("sdlc ci",))
    assert api.asked[0] == f"{github.API}/repos/{REPO}/rules/branches/main?per_page=100&page=1"


def test_classic_contexts_and_checks_are_found(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, branches__main=protected(["b"], ["c"]))
    assert github.required_checks(REPO, "main", "token") == ("main", ("b", "c"))


def test_union_is_sorted_and_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        rules__branches__main=ruleset("sdlc ci", "z"),
        branches__main=protected(["sdlc ci", "a"], ["a"]),
    )
    assert github.required_checks(REPO, "main", "token")[1] == ("a", "sdlc ci", "z")


def test_branch_is_quoted_and_unprotected_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    api = install(
        monkeypatch, **{"rules__branches__feat%2Fx": [], "branches__feat%2Fx": {"protected": False}}
    )
    assert github.required_checks(REPO, "feat/x", "token") == ("feat/x", ())
    assert "/rules/branches/feat%2Fx?" in api.asked[0]


def test_default_branch_is_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, **{"/": {"default_branch": "trunk"}, "rules__branches__trunk": ruleset("ci")})
    assert github.required_checks(REPO, None, "token") == ("trunk", ("ci",))


def test_bad_repo_or_token_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="token"):
        github.required_checks("no-slash", "main", "token")
    with pytest.raises(ValueError, match="token"):
        github.required_checks(REPO, "main", "")
    with pytest.raises(ValueError, match="token"):
        github.reported(REPO, SHA, "")


def test_rulesets_paginate(monkeypatch: pytest.MonkeyPatch) -> None:
    full = [{"type": "deletion"}] * 100

    def page(url: str) -> list[dict[str, Any]]:
        return full if url.endswith("page=1") else ruleset("late")

    install(monkeypatch, rules__branches__main=page, branches__main={})
    assert github.required_checks(REPO, "main", "token")[1] == ("late",)


def test_rules_are_cached_for_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    api = install(monkeypatch, rules__branches__main=ruleset("ci"), branches__main={})
    now = [1000.0]
    monkeypatch.setattr(github, "_clock", lambda: now[0])
    github.required_checks(REPO, "main", "token")
    first = len(api.asked)
    now[0] += github.RULES_TTL - 1
    github.required_checks(REPO, "main", "token")
    assert len(api.asked) == first
    now[0] += 2
    github.required_checks(REPO, "main", "token")
    assert len(api.asked) == 2 * first


def run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def runs(*items: dict[str, Any], total: int | None = None) -> dict[str, Any]:
    return {"total_count": len(items) if total is None else total, "check_runs": list(items)}


def test_runs_and_statuses_are_both_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = {"statuses": [{"context": "legacy", "state": "success"}, {"context": "old", "state": "error"}]}
    install(
        monkeypatch,
        **{
            f"commits__{SHA}__check-runs": runs(run("sdlc ci"), run("slow", "in_progress", None)),
            f"commits__{SHA}__status": statuses,
        },
    )
    assert github.reported(REPO, SHA, "token") == {
        "sdlc ci": Verdict.SUCCESS,
        "slow": Verdict.PENDING,
        "legacy": Verdict.SUCCESS,
        "old": Verdict.FAILURE,
    }


def test_duplicates_combine(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        run("a"), run("a", conclusion="failure"),
        run("b"), run("b", "queued", None),
        run("c", conclusion="skipped"), run("c"),
        run("d", conclusion="cancelled"), run("d", conclusion="neutral"),
    ]  # fmt: skip
    install(monkeypatch, **{f"commits__{SHA}__check-runs": runs(*items)})
    assert github.reported(REPO, SHA, "token") == {
        "a": Verdict.FAILURE,
        "b": Verdict.PENDING,
        "c": Verdict.SUCCESS,
        "d": Verdict.FAILURE,
    }


def test_check_runs_paginate(monkeypatch: pytest.MonkeyPatch) -> None:
    def page(url: str) -> dict[str, Any]:
        n = 100 if url.endswith("page=1") else 1
        return runs(*[run(f"{url[-1]}-{i}") for i in range(n)], total=TOTAL)

    api = install(monkeypatch, **{f"commits__{SHA}__check-runs": page})
    assert len(github.reported(REPO, SHA, "token")) == TOTAL
    assert sum("check-runs" in url for url in api.asked) == PAGES


def test_commit_statuses_paginate(monkeypatch: pytest.MonkeyPatch) -> None:
    def page(url: str) -> dict[str, Any]:
        n = 100 if url.endswith("page=1") else 1
        entries = [{"context": f"{url[-1]}-{i}", "state": "success"} for i in range(n)]
        return {"total_count": TOTAL, "statuses": entries}

    api = install(monkeypatch, **{f"commits__{SHA}__status": page})
    assert len(github.reported(REPO, SHA, "token")) == TOTAL
    assert sum("/status?" in url for url in api.asked) == PAGES


def test_required_without_a_report_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        rules__branches__main=ruleset("sdlc ci", "other"),
        branches__main={},
        **{f"commits__{SHA}__check-runs": runs(run("sdlc ci"), run("unrelated"))},
    )
    assert github.GitHub().required(REPO, SHA, "main", "token") == (
        "main",
        {"other": Verdict.PENDING, "sdlc ci": Verdict.SUCCESS},
    )


def test_http_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    boom = urllib.error.HTTPError("u", 403, "Forbidden", Message(), None)
    install(monkeypatch, rules__branches__main=boom, **{f"commits__{SHA}__check-runs": boom})
    with pytest.raises(urllib.error.HTTPError):
        github.required_checks(REPO, "main", "token")
    with pytest.raises(urllib.error.HTTPError):
        github.reported(REPO, SHA, "token")
