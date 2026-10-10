"""The sweep's limits are the budget: triage per run, fixes per hour (profile IV)."""

import sweep
from sweep import MAX_LIMIT, candidates, conflicted, remaining, select, waiting


def issue(number, created, author="maintainer", labels=(), pr=False):
    item = {"number": number, "created_at": created, "user": {"login": author},
            "labels": [{"name": name} for name in labels]}  # fmt: skip
    if pr:
        item["pull_request"] = {}
    return item


def writes(repo, login):
    return login == "maintainer"


def test_candidates_skip_touched_issues_and_pull_requests():
    issues = [
        issue(1, "2026-01-01"),
        issue(2, "2026-01-02", labels=("triaged",)),
        issue(4, "2026-01-04", pr=True),
        issue(5, "2026-01-05", labels=("enhancement",)),
    ]
    assert [item["number"] for item in candidates("o/r", issues)] == [1, 5]


def test_select_takes_the_oldest_across_repos_up_to_the_limit():
    found = candidates("o/a", [issue(1, "2026-03-01"), issue(2, "2026-01-01")])
    found += candidates("o/b", [issue(9, "2026-02-01")])
    chosen = select(found, 2, writes)
    assert [(item["repo"], item["number"]) for item in chosen] == [("o/a", 2), ("o/b", 9)]


def test_authors_without_write_access_are_skipped_without_using_the_budget():
    found = candidates("o/a", [issue(1, "2026-01-01", author="stranger"), issue(2, "2026-02-01")])
    assert [item["number"] for item in select(found, 1, writes)] == [2]


def test_limit_zero_queues_nothing():
    assert select(candidates("o/a", [issue(1, "2026-03-01")]), 0, writes) == []


def test_no_run_queues_more_than_the_hard_maximum():
    found = candidates("o/a", [issue(n, f"2026-01-{n:02d}") for n in range(1, 29)])
    assert len(select(found, 500, writes)) == MAX_LIMIT


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


def test_a_ready_pull_request_is_not_an_issue_to_start():
    labeled_pull = {**ready(9), "pull_request": {}}
    assert waiting("o/r", [labeled_pull], set(), HOUR_AGO) == []


def test_a_negative_budget_starts_nothing():
    assert remaining(-3, 0) == 0


def test_the_past_hour_is_counted_from_the_apps_pull_requests(monkeypatch):
    calls = []
    opened_this_hour = 3

    def fake_gh(*args):
        calls.append(args)
        return f"{opened_this_hour}\n"

    monkeypatch.setattr(sweep, "gh", fake_gh)
    assert sweep.opened_since("o", "app/bot", HOUR_AGO) == opened_this_hour
    assert f"q=org:o is:pr author:app/bot created:>={HOUR_AGO}" in calls[0]
    assert "search/issues" in calls[0]
