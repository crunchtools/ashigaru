"""The review comment: the agent's opinion, the API's facts, and a marker that stops repeats."""

import json

import pytest

import review_publish
from inbox import REVIEWED
from review_publish import FAILED, MAX_POINTS, checks, comment, parse

GOOD = {
    "summary": "Replaces the fixed vector width with one read from the model.",
    "recommendation": "close",
    "already_fixed": True,
    "concerns": ["src/store.py: the migration is not covered by a test."],
    "blockers": [],
}
FACTS = {
    "mergeable": "CONFLICTING",
    "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}],
}


def test_a_well_formed_verdict_is_accepted():
    assert parse(json.dumps(GOOD)) == GOOD


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        json.dumps({**GOOD, "recommendation": "ship it"}),
        json.dumps({**GOOD, "summary": " "}),
    ],
)
def test_anything_else_is_not_a_verdict(raw):
    assert parse(raw) is None


def test_the_comment_carries_the_verdict_the_facts_and_the_reviewed_commit():
    body = comment(GOOD, "abc1234def", FACTS)
    assert REVIEWED.search(body).groups() == ("abc1234def", "close", "true")
    assert "Recommendation: **close**" in body
    assert "The base branch already contains this change" in body
    assert "Worth a look:\n- src/store.py: the migration is not covered by a test." in body
    assert "Blocking:" not in body
    assert "Merges cleanly: no, it conflicts with the base branch. Checks: 1 passed, 1 failed." in body
    assert "Reviewed at `abc1234`." in body


def test_a_failed_review_still_leaves_a_marker_so_the_sweep_does_not_retry_hourly():
    body = comment(None, "abc1234", {})
    assert REVIEWED.search(body).groups() == ("abc1234", "unknown", "false")
    assert FAILED in body
    assert "Merges cleanly: not known yet. Checks: none finished." in body


def test_the_agent_cannot_flood_the_comment():
    noisy = {**GOOD, "recommendation": "changes", "blockers": [f"file{n}.py: x" for n in range(40)]}
    assert comment(noisy, "abc1234", FACTS).count("\n- file") == MAX_POINTS


def test_running_checks_are_counted_neither_way():
    assert (
        checks([{"state": "PENDING"}, {"conclusion": "SKIPPED"}, {"conclusion": ""}]) == "1 passed, 0 failed"
    )


def posting(monkeypatch, comments):
    calls = []
    monkeypatch.setattr(review_publish, "api_pages", lambda path: comments)
    monkeypatch.setattr(review_publish, "api", lambda *args: calls.append(("api", *args)))
    monkeypatch.setattr(review_publish, "gh", lambda *args: calls.append(("gh", *args)))
    return calls


def test_the_first_review_is_a_new_comment(monkeypatch):
    calls = posting(monkeypatch, [])
    assert review_publish.post("o/r", 9, "body", "ashigaru-crunchtools[bot]") == "posted"
    assert calls == [("gh", "pr", "comment", "9", "--repo", "o/r", "--body", "body")]


def test_a_later_review_edits_the_earlier_comment(monkeypatch):
    earlier = {"id": 55, "body": comment(GOOD, "old", FACTS), "user": {"login": "ashigaru-crunchtools[bot]"}}
    calls = posting(monkeypatch, [earlier])
    assert review_publish.post("o/r", 9, "body", "ashigaru-crunchtools[bot]") == "updated"
    assert calls == [("api", "repos/o/r/issues/comments/55", "-X", "PATCH", "-f", "body=body")]


def test_token_shaped_text_never_reaches_the_comment():
    leaky = {
        **GOOD,
        "summary": "see sk-ant-oat01-AbCdEfGhIjKlMnOpQrStUv",
        "concerns": ["x.py: ghs_" + "a" * 36],
    }
    body = comment(leaky, "abc1234", FACTS)
    assert "sk-ant-" not in body and "ghs_" not in body
    assert "see [removed]" in body and "- x.py: [removed]" in body


@pytest.mark.parametrize(("state", "posted"), [("OPEN", ["body"]), ("MERGED", []), ("CLOSED", [])])
def test_only_an_open_pull_request_gets_a_comment(monkeypatch, state, posted):
    seen = []
    argv = [
        "review_publish.py",
        "--repo",
        "o/r",
        "--pull",
        "9",
        "--sha",
        "abc1234",
        "--result",
        json.dumps(GOOD),
    ]
    monkeypatch.setattr(review_publish.sys, "argv", argv)
    monkeypatch.setattr(review_publish, "gh", lambda *args: json.dumps({"state": state, **FACTS}))
    monkeypatch.setattr(review_publish, "post", lambda repo, pull, body, bot: seen.append("body") or "posted")
    assert review_publish.main() == 0
    assert seen == posted
