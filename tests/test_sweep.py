"""The sweep's limits are the budget: triage per run, fixes per hour (profile IV)."""

import pytest

import sweep
from sweep import MAX_LIMIT, candidates, conflicted, cutoff, remaining, select, setters, stale, waiting


def issue(number, created, author="maintainer", labels=(), **overrides):
    pr, kind = overrides.get("pr", False), overrides.get("kind", "User")
    item = {"number": number, "created_at": created, "user": {"login": author, "type": kind},
            "labels": [{"name": name} for name in labels]}  # fmt: skip
    if pr:
        item["pull_request"] = {}
    return item


def test_candidates_skip_touched_issues_and_pull_requests():
    issues = [
        issue(1, "2026-01-01"),
        issue(2, "2026-01-02", labels=("triaged",)),
        issue(4, "2026-01-04", pr=True),
        issue(5, "2026-01-05", labels=("enhancement",)),
        issue(6, "2026-01-06", author="ashigaru-crunchtools[bot]", kind="Bot", labels=("ashigaru-inbox",)),
        issue(7, "2026-01-07", author="spam[bot]", kind="Bot"),
        issue(8, "2026-01-08", labels=("ashigaru-releases",)),
    ]
    assert [item["number"] for item in candidates("o/r", issues)] == [1, 5]


def test_select_takes_the_oldest_across_repos_up_to_the_limit():
    found = candidates("o/a", [issue(1, "2026-03-01"), issue(2, "2026-01-01")])
    found += candidates("o/b", [issue(9, "2026-02-01")])
    chosen = select(found, 2)
    assert [(item["repo"], item["number"]) for item in chosen] == [("o/a", 2), ("o/b", 9)]


def test_issues_from_outside_are_queued_for_triage_like_any_other():
    found = candidates("o/a", [issue(1, "2026-01-01", author="stranger"), issue(2, "2026-02-01")])
    assert [item["number"] for item in select(found, 1)] == [1]


def test_limit_zero_queues_nothing():
    assert select(candidates("o/a", [issue(1, "2026-03-01")]), 0) == []


def test_no_run_queues_more_than_the_hard_maximum():
    found = candidates("o/a", [issue(n, f"2026-01-{n:02d}") for n in range(1, 29)])
    assert len(select(found, 500)) == MAX_LIMIT


BOT = "app/ashigaru-crunchtools"


def pull(number, branch, **overrides):
    labels = overrides.pop("labels", ())
    return {
        "number": number,
        "headRefName": branch,
        "mergeable": "CONFLICTING",
        "author": {"login": overrides.pop("author", BOT)},
        "isCrossRepository": False,
        "labels": [{"name": name} for name in labels],
        **overrides,
    }


def test_a_conflicted_ashigaru_pull_request_is_rebuilt_from_its_issue():
    assert conflicted("o/r", [pull(51, "ashigaru/issue-47")], BOT) == [
        {"repo": "o/r", "number": 47, "pull": 51}
    ]


def test_pull_requests_that_merge_or_belong_to_someone_else_are_left_alone():
    pulls = [
        pull(1, "ashigaru/issue-2", mergeable="MERGEABLE"),
        pull(3, "ashigaru/issue-4", author="mallory"),
        pull(5, "ashigaru/issue-6", isCrossRepository=True),
        pull(7, "feature/thing"),
        pull(9, "ashigaru/issue-x"),
    ]
    assert conflicted("o/r", pulls, BOT) == []


def test_a_pull_request_a_human_took_over_is_not_rebuilt():
    assert conflicted("o/r", [pull(51, "ashigaru/issue-47", labels=("needs-human",))], BOT) == []


HOUR_AGO = "2026-10-10T12:00:00Z"


def ready(number, updated="2026-10-10T09:00:00Z"):
    return {**issue(number, "2026-10-01T00:00:00Z", labels=("ready-to-code",)), "updated_at": updated}


def test_a_ready_issue_with_no_pull_request_is_waiting():
    assert waiting("o/r", [ready(7)], set(), HOUR_AGO) == [
        {"repo": "o/r", "number": 7, "created_at": "2026-10-01T00:00:00Z"}
    ]


def test_an_issue_that_already_has_its_pull_request_is_not_started_again():
    assert waiting("o/r", [ready(7)], {"ashigaru/issue-7"}, HOUR_AGO) == []


def test_an_issue_touched_moments_ago_may_have_a_fix_in_flight():
    assert waiting("o/r", [ready(7, updated="2026-10-10T12:05:00Z")], set(), HOUR_AGO) == []


def test_issues_without_the_label_are_not_waiting():
    untouched = {**issue(8, "2026-10-01T00:00:00Z"), "updated_at": "2026-10-10T09:00:00Z"}
    assert waiting("o/r", [untouched], set(), HOUR_AGO) == []


def test_the_hourly_budget_is_what_the_past_hour_left():
    per_hour = 5
    assert remaining(per_hour, 0) == per_hour
    assert remaining(per_hour, 2) == per_hour - 2
    assert remaining(per_hour, 9) == 0
    assert remaining(MAX_LIMIT + 50, 0) == MAX_LIMIT


FIX = "2026-10-16"


ASHIGARU = setters(BOT)


def labeled(name, login, at):
    return {"event": "labeled", "label": {"name": name}, "actor": {"login": login}, "created_at": at}


def test_a_verdict_ashigaru_gave_before_the_fix_is_triaged_again():
    """mcp-pcloud#40: a bug labeled a feature before 2.7.0, never revisited."""
    history = {
        40: [
            labeled("enhancement", "github-actions[bot]", "2026-10-11T09:00:00Z"),
            labeled("triaged", "github-actions[bot]", "2026-10-11T09:00:00Z"),
        ]
    }
    issues = [issue(40, "2026-10-11", labels=("enhancement", "triaged"))]
    assert [item["number"] for item in stale("o/r", issues, FIX, lambda repo, n: history[n], ASHIGARU)] == [
        40
    ]


def test_newer_verdicts_and_a_persons_label_are_left_alone():
    history = {
        1: [labeled("needs-info", "github-actions[bot]", "2026-10-17T09:00:00Z")],
        2: [
            labeled("triaged", "github-actions[bot]", "2026-10-01T09:00:00Z"),
            labeled("triaged", "fatherlinux", "2026-10-02T09:00:00Z"),
        ],
        3: [labeled("triaged", "other-automation[bot]", "2026-10-01T09:00:00Z")],
    }
    issues = [issue(n, "2026-10-01", labels=(history[n][-1]["label"]["name"],)) for n in history]
    assert stale("o/r", issues, FIX, lambda repo, n: history[n], ASHIGARU) == []


def test_issues_already_queued_or_unlabeled_cost_no_lookup():
    issues = [
        issue(1, "2026-10-01"),
        issue(2, "2026-10-01", labels=("triaged", "ready-for-triage")),
        issue(3, "2026-10-01", labels=("ready-to-code",)),
        issue(4, "2026-10-01", labels=("triaged",), pr=True),
    ]

    def events(repo, number):
        raise AssertionError(f"looked up #{number}")

    assert stale("o/r", issues, FIX, events, ASHIGARU) == []


@pytest.mark.parametrize(
    ("at", "queued"),
    [("2026-10-15T23:59:59Z", True), ("2026-10-16T00:00:00Z", False), ("2026-10-16T18:00:00Z", False)],
)
def test_the_cutoff_date_itself_is_not_before_it(at, queued):
    history = [labeled("triaged", "ashigaru-crunchtools[bot]", at)]
    issues = [issue(1, "2026-10-01", labels=("triaged",))]
    assert bool(stale("o/r", issues, FIX, lambda repo, n: history, ASHIGARU)) is queued


@pytest.mark.parametrize("value", ["2026-99-99", "yesterday", "2026-10-16T00:00"])
def test_a_malformed_cutoff_is_refused(value):
    with pytest.raises(ValueError):
        cutoff(value)


class Parsed(Exception):
    """Raised by the first API call, so reaching it means the arguments parsed."""


@pytest.mark.parametrize("extra", [[], ["--retriage-before", "2026-10-16"]])
def test_the_sweep_starts_with_or_without_a_cutoff(monkeypatch, extra):
    def first_call(path):
        raise Parsed

    monkeypatch.setattr(sweep, "api_pages", first_call)
    monkeypatch.setattr("sys.argv", ["sweep.py", "--org", "o", *extra])
    with pytest.raises(Parsed):
        sweep.main()


def test_events_reads_the_issue_event_history(monkeypatch):
    asked = []
    monkeypatch.setattr(sweep, "api_pages", lambda path: asked.append(path) or [])
    assert sweep.events("o/r", 40) == []
    assert asked == ["repos/o/r/issues/40/events?per_page=100"]
