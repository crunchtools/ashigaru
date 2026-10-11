"""What waits on a maintainer: which outside work is listed, and which pull requests are reviewed."""

import inbox
from inbox import (
    MAX_LISTED,
    MAX_REVIEWS,
    due,
    handed_over,
    held_releases,
    last_review,
    outside_issues,
    outside_pulls,
    render,
    review_marker,
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
