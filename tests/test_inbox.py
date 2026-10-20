"""What waits on a maintainer: which outside work is listed, and which pull requests are reviewed."""

import inbox
from inbox import (
    MAX_LISTED,
    MAX_REVIEWS,
    due,
    failed_run,
    fixed,
    handed_over,
    held_releases,
    last_review,
    outside_issues,
    outside_pulls,
    past_week,
    recently_triaged,
    red,
    red_branches,
    render,
    review_marker,
    triaged_at,
    unanswered,
)

BOT = "ashigaru-crunchtools[bot]"


def writes(repo, login):
    return login == "maintainer"


def pull(number, author="visitor", **overrides):
    return {
        "number": number,
        "title": f"Change {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "author": {"login": author, "is_bot": overrides.pop("is_bot", False)},
        "createdAt": overrides.pop("created", "2026-03-30T10:00:00Z"),
        "headRefOid": overrides.pop("sha", "abc1234"),
        "baseRefName": "main",
        "isDraft": overrides.pop("draft", False),
        "labels": [{"name": name} for name in overrides.pop("labels", ())],
    }


def issue(number, author="visitor", labels=(), kind="User", pr=False):
    item = {
        "number": number,
        "title": f"Report {number}",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "user": {"login": author, "type": kind},
        "created_at": "2026-04-18T09:00:00Z",
        "labels": [{"name": name} for name in labels],
    }
    if pr:
        item["pull_request"] = {}
    return item


def comment(body, login=BOT, ident=1):
    return {"id": ident, "body": body, "user": {"login": login}}


def test_only_people_without_write_access_count_as_outside():
    pulls = [pull(1), pull(2, author="maintainer"), pull(3, author="app/dependabot", is_bot=True)]
    assert [item["number"] for item in outside_pulls("o/r", pulls, writes)] == [1]


def test_outside_issues_carry_what_triage_made_of_them():
    issues = [
        issue(1, labels=("bug", "triaged", "good first issue")),
        issue(2, author="maintainer"),
        issue(3, kind="Bot"),
        issue(4, pr=True),
    ]
    found = outside_issues("o/r", issues, writes)
    assert [(item["number"], item["labels"]) for item in found] == [(1, ["bug", "triaged"])]


def test_the_review_marker_round_trips():
    body = f"{review_marker('abc1234', 'close', True)}\n**Ashigaru first-pass review**"
    assert last_review([comment(body, ident=7)], BOT) == {
        "sha": "abc1234",
        "verdict": "close",
        "fixed": True,
        "id": 7,
    }


def test_a_marker_someone_else_wrote_is_not_a_review():
    forged = comment(review_marker("abc1234", "merge", False), login="visitor")
    assert last_review([forged], BOT) is None


def test_the_newest_review_wins():
    old = comment(review_marker("aaa", "changes", False), ident=1)
    new = comment(review_marker("bbb", "merge", False), ident=2)
    assert last_review([old, comment("thanks!", login="visitor"), new], BOT)["sha"] == "bbb"


def test_a_pull_request_is_due_until_its_head_commit_has_been_reviewed():
    pulls = outside_pulls("o/r", [pull(1, sha="new"), pull(2, sha="same"), pull(3, draft=True)], writes)
    reviews = {
        pulls[0]["url"]: {"sha": "old", "verdict": "merge", "fixed": False},
        pulls[1]["url"]: {"sha": "same", "verdict": "merge", "fixed": False},
        pulls[2]["url"]: None,
    }
    assert due(pulls, reviews) == [{"repo": "o/r", "number": 1, "sha": "new", "base": "main"}]


def test_no_sweep_reviews_more_than_its_share_and_the_oldest_go_first():
    listed = [pull(n, created=f"2026-03-{30 - n:02d}T00:00:00Z") for n in range(1, MAX_REVIEWS + 4)]
    chosen = due(outside_pulls("o/r", listed, writes), {})
    assert len(chosen) == MAX_REVIEWS
    assert chosen[0]["number"] == MAX_REVIEWS + 3


def test_handed_over_and_unanswered_are_read_from_labels():
    assert [item["number"] for item in handed_over("o/r", [pull(1, labels=("needs-human",)), pull(2)])] == [1]
    assert (
        unanswered([issue(1, labels=("needs-info",)), issue(2), issue(3, labels=("needs-info",), pr=True)])
        == 1
    )


def release_pull(number, version="1.2.3", **overrides):
    head = {
        "headRefName": f"ashigaru/release-{version}",
        "autoMergeRequest": None,
        "isCrossRepository": False,
    }
    return (
        pull(number, author="app/ashigaru-crunchtools", labels=overrides.pop("labels", ())) | head | overrides
    )


def test_a_release_pull_request_that_will_not_merge_on_its_own_waits_on_a_maintainer():
    pulls = [
        release_pull(1),
        release_pull(2, autoMergeRequest={"mergeMethod": "SQUASH"}),
        release_pull(3, labels=("needs-human",)),
        release_pull(4, isCrossRepository=True),
        release_pull(5, headRefName="ashigaru/issue-5"),
    ]
    held = held_releases("o/r", pulls)
    assert [item["number"] for item in held] == [1]
    body = render([], {}, [], held, {})
    assert "merging it publishes the release" in body and "Labeled needs-human" not in body


def test_the_list_says_what_ashigaru_made_of_each_item_and_notifies_nobody():
    pulls = outside_pulls("o/mcp-trove", [pull(9)], writes)
    reviews = {pulls[0]["url"]: {"sha": "abc1234", "verdict": "close", "fixed": True}}
    issues = outside_issues("o/mcp-workboard", [issue(25, labels=("enhancement", "triaged"))], writes)
    body = render(pulls, reviews, issues, [], {"o/acquacotta": 9, "o/quiet": 0})
    assert "## Pull requests from outside contributors (1)" in body
    assert (
        "- [mcp-trove#9](https://github.com/o/r/pull/9) `Change 9`, by visitor, opened 2026-03-30. " in body
    )
    assert "Ashigaru: close, already fixed on the base branch." in body
    assert "- [mcp-workboard#25](https://github.com/o/r/issues/25) `Report 25`, by visitor" in body
    assert "enhancement, triaged." in body
    assert "- acquacotta: 9" in body and "quiet" not in body
    assert "## Pull requests Ashigaru handed over (0)\n\nNothing waiting." in body
    assert "@" not in body


def test_an_unchanged_list_renders_the_same_and_a_long_one_is_cut():
    pulls = outside_pulls("o/r", [pull(n) for n in range(MAX_LISTED + 2)], writes)
    body = render(pulls, {}, [], [], {})
    assert body == render(pulls, {}, [], [], {})
    assert "- and 2 more" in body
    assert body.count("Not reviewed yet.") == MAX_LISTED


def publishing(monkeypatch, found):
    calls = []
    monkeypatch.setattr(inbox, "ensure_labels", lambda repo, names: set(names))
    monkeypatch.setattr(inbox, "api", lambda path: found)
    monkeypatch.setattr(inbox, "gh", lambda *args: calls.append(args))
    return calls


def test_the_inbox_issue_is_created_once(monkeypatch):
    calls = publishing(monkeypatch, [])
    assert inbox.publish("o/ashigaru", "body\n") == "created"
    assert calls[0][:2] == ("issue", "create") and "ashigaru-inbox" in calls[0]


def test_an_unchanged_inbox_is_not_edited(monkeypatch):
    calls = publishing(monkeypatch, [{"number": 90, "body": "body\r\n"}])
    assert inbox.publish("o/ashigaru", "body\n") == "unchanged"
    assert calls == []


def test_a_changed_inbox_is_edited_in_place(monkeypatch):
    calls = publishing(monkeypatch, [{"number": 90, "body": "old"}])
    assert inbox.publish("o/ashigaru", "body\n") == "updated"
    assert calls == [("issue", "edit", "90", "--repo", "o/ashigaru", "--body", "body\n")]


def test_a_strangers_title_cannot_add_links_images_or_mentions_to_the_list():
    hostile = pull(1)
    hostile["title"] = "fix `x` ![img](http://evil) [click](http://evil)\n@maintainer " + "a" * 300
    line = render(outside_pulls("o/r", [hostile], writes), {}, [], [], {}).splitlines()[5]
    before, title, after = line.split("`")
    assert before.endswith(") ") and after.startswith(", by visitor") and len(title) == inbox.TITLE_LIMIT
    assert title.startswith("fix 'x' ![img](http://evil) [click](http://evil) @maintainer")


def test_a_pull_request_whose_author_is_gone_is_not_listed():
    orphan = pull(1)
    orphan["author"] = None
    assert outside_pulls("o/r", [orphan], writes) == []


def hit(number, repo="mcp-trove", labels=(), closed=None, events=()):
    return {
        "repository": {"nameWithOwner": f"o/{repo}"},
        "number": number,
        "title": f"Bug {number}",
        "url": f"https://github.com/o/{repo}/issues/{number}",
        "createdAt": "2026-10-01T09:00:00Z",
        "closedAt": closed,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "timelineItems": {"nodes": [{"createdAt": at, "label": {"name": name}} for name, at in events]},
    }


def test_fixes_are_listed_newest_first_with_the_day_they_closed():
    done = fixed([hit(3, closed="2026-10-12T08:00:00Z"), hit(7, repo="rotv", closed="2026-10-15T08:00:00Z")])
    assert [item["number"] for item in done] == [7, 3] and done[0]["repo"] == "o/rotv"
    body = past_week(done, [])
    assert "## Fixed by Ashigaru, past 7 days (2)" in body
    assert (
        "- [rotv#7](https://github.com/o/rotv/issues/7) `Bug 7`, opened 2026-10-01. Closed 2026-10-15."
        in (body)
    )


def test_the_last_triaged_label_says_when_an_issue_was_triaged():
    events = [("triaged", "2026-10-02T00:00:00Z"), ("bug", "2026-10-14T00:00:00Z"),
              ("triaged", "2026-10-11T00:00:00Z")]  # fmt: skip
    assert triaged_at(hit(1, events=events)) == "2026-10-11T00:00:00Z"
    assert triaged_at(hit(1, events=[("bug", "2026-10-14T00:00:00Z")])) is None


def test_only_issues_triaged_inside_the_window_are_listed_oldest_verdict_first():
    nodes = [
        hit(1, labels=("enhancement", "triaged"), events=[("triaged", "2026-10-14T00:00:00Z")]),
        hit(2, events=[("triaged", "2026-10-10T00:00:00Z")]),
        hit(3, events=[("triaged", "2026-10-01T00:00:00Z")]),  # updated lately, triaged before the window
        hit(4),
    ]
    listed = recently_triaged(nodes, "2026-10-09T00:00:00Z")
    assert [item["number"] for item in listed] == [2, 1]
    body = past_week([], listed)
    assert "## Triaged, past 7 days, still open (2)" in body
    assert "`Bug 1`, opened 2026-10-01. Triaged 2026-10-14: enhancement." in body
    assert "`Bug 2`, opened 2026-10-01. Triaged 2026-10-10." in body


def test_an_empty_week_says_so():
    body = past_week([], [])
    assert "## Fixed by Ashigaru, past 7 days (0)\n\nNothing fixed." in body
    assert "## Triaged, past 7 days, still open (0)\n\nNothing triaged." in body


def repo_node(name, state, contexts=(), **overrides):
    rollup = {"state": state, "contexts": {"nodes": list(contexts)}} if state else None
    return {
        "nameWithOwner": f"o/{name}",
        "isArchived": overrides.get("archived", False),
        "defaultBranchRef": {
            "name": "main",
            "target": {"oid": "abc1234def", "url": f"https://github.com/o/{name}/commit/abc1234def",
                       "statusCheckRollup": rollup},
        },
    }  # fmt: skip


def check(name, conclusion, url="https://github.com/o/r/actions/runs/1/job/2"):
    return {"name": name, "conclusion": conclusion, "detailsUrl": url}


def test_only_failed_or_errored_rollups_are_red():
    repos = [
        repo_node("green", "SUCCESS"),
        repo_node("waiting", "PENDING"),
        repo_node("expected", "EXPECTED"),
        repo_node("unchecked", None),
        repo_node("old", "FAILURE", archived=True),
        repo_node("errored", "ERROR"),
        repo_node("broken", "FAILURE"),
        {"nameWithOwner": "o/empty", "isArchived": False, "defaultBranchRef": None},
    ]
    assert [item["repo"] for item in red_branches(repos)] == ["o/broken", "o/errored"]


def test_a_red_branch_links_its_failed_checks_and_statuses_but_not_the_cancelled():
    contexts = [
        check("test (3.11)", "CANCELLED"),
        check("test (3.12)", "FAILURE"),
        check("lint", "SUCCESS"),
        check("build", None),
        {"context": "ci/legacy", "state": "ERROR", "targetUrl": "https://ci.example.com/9"},
        {"context": "ci/ok", "state": "SUCCESS", "targetUrl": "https://ci.example.com/8"},
    ]
    body = red(red_branches([repo_node("mcp-x", "FAILURE", contexts)]), [], [])
    assert body.startswith("## Red on the default branch (1)\n\n")
    assert (
        "- [mcp-x](https://github.com/o/mcp-x/commit/abc1234def) `main` at `abc1234`: failure. "
        "Failing: [`test (3.12)`](https://github.com/o/r/actions/runs/1/job/2), "
        "[`ci/legacy`](https://ci.example.com/9)."
    ) in body


def test_a_red_branch_with_no_failed_check_listed_still_says_where_to_look():
    body = red(red_branches([repo_node("mcp-x", "FAILURE", [check("t", "CANCELLED")])]), [], [])
    assert "failure. No check failed outright; see the commit." in body


def test_a_strangers_check_name_and_link_stay_inert():
    hostile = check("x` [click](http://evil) @maintainer\n![i](http://evil)", "FAILURE",
                    url="https://evil.example/a) [more](http://evil")  # fmt: skip
    line = red(red_branches([repo_node("r", "FAILURE", [hostile])]), [], []).splitlines()[2]
    assert line.endswith("Failing: `x' [click](http://evil) @maintainer ![i](http://evil)`.")
    assert "evil.example" not in line


def test_a_watched_run_is_red_only_when_its_last_completed_run_failed():
    run = {"conclusion": "failure", "html_url": "https://github.com/o/constitution/actions/runs/3",
           "created_at": "2026-10-18T05:01:01Z"}  # fmt: skip
    assert failed_run("o/constitution", "fleet-drift.yml", None) is None
    assert failed_run("o/constitution", "fleet-drift.yml", {**run, "conclusion": "success"}) is None
    assert failed_run("o/constitution", "fleet-drift.yml", {**run, "conclusion": "cancelled"}) is None
    body = red([], [failed_run("o/constitution", "fleet-drift.yml", run)], [])
    assert (
        "- [constitution](https://github.com/o/constitution/actions/runs/3) `fleet-drift.yml`, "
        "last run 2026-10-18: failure."
    ) in body


def test_nothing_red_says_so_and_what_could_not_be_read_is_named():
    assert red([], [], []) == "## Red on the default branch (0)\n\nNothing red.\n"
    body = red([], [], ["the default branches"])
    assert "- Could not read the default branches; see the sweep log." in body
