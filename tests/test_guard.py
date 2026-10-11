"""The guard decides how far an issue may go: to code, to triage only, or nowhere (profile III)."""

import pytest

import guard
from guard import MAINTAINER, NONE, OUTSIDE, STAGES, decide

WRITERS = {"maintainer"}


def event(action, author, sender, **overrides):
    payload = {
        "action": action,
        "sender": {"login": sender, "type": overrides.pop("sender_type", "User")},
        "issue": {
            "state": overrides.pop("state", "open"),
            "user": {"login": author, "type": overrides.pop("author_type", "User")},
        },
    }
    if "label" in overrides:
        payload["label"] = {"name": overrides.pop("label")}
    payload["issue"] |= overrides
    return payload


def trust(*args, vouched=False, **kwargs):
    return decide(event(*args, **kwargs), WRITERS.__contains__, lambda: vouched)[0]


def test_an_issue_opened_by_an_account_with_write_access_is_a_maintainers():
    assert trust("opened", "maintainer", "maintainer") == MAINTAINER


def test_an_issue_opened_by_a_stranger_is_outside():
    assert trust("opened", "stranger", "stranger") == OUTSIDE


def test_outside_work_reaches_triage_and_never_the_code_agent():
    assert OUTSIDE in STAGES["triage"]
    assert STAGES["code"] == {MAINTAINER}


def test_a_maintainers_label_vouches_for_a_strangers_issue():
    assert trust("labeled", "stranger", "maintainer", label="ready-to-code") == MAINTAINER


def test_the_sweep_may_queue_a_strangers_issue_for_triage_only():
    assert (
        trust("labeled", "stranger", "sweeper[bot]", sender_type="Bot", label="ready-for-triage") == OUTSIDE
    )


def test_a_strangers_ready_to_code_label_opens_nothing():
    assert trust("labeled", "stranger", "stranger", label="ready-to-code") == NONE


def test_a_bot_is_never_the_maintainer_whose_label_vouches_for_an_issue():
    assert trust("labeled", "stranger", "maintainer", sender_type="Bot", label="ready-to-code") == NONE


def test_automation_may_requeue_what_a_maintainer_vouched_for():
    relabel = ("labeled", "stranger", "sweeper[bot]")
    assert trust(*relabel, sender_type="Bot", label="ready-to-code", vouched=True) == MAINTAINER


def test_the_label_history_is_read_only_for_a_requeue():
    def unexpected():
        raise AssertionError("label history must not be consulted")

    payloads = (
        event("opened", "stranger", "stranger"),
        event("labeled", "stranger", "sweeper[bot]", sender_type="Bot", label="ready-for-triage"),
        event("labeled", "maintainer", "sweeper[bot]", sender_type="Bot", label="ready-to-code"),
    )
    levels = [decide(payload, WRITERS.__contains__, unexpected)[0] for payload in payloads]
    assert levels == [OUTSIDE, OUTSIDE, MAINTAINER]


def test_a_maintainers_other_labels_do_not_vouch_for_a_strangers_issue():
    assert trust("labeled", "stranger", "maintainer", label="ready-for-triage") == OUTSIDE
    assert trust("labeled", "stranger", "maintainer", label="good first issue") == NONE


def test_a_label_from_automation_admits_a_maintainers_issue():
    assert (
        trust("labeled", "maintainer", "sweeper[bot]", sender_type="Bot", label="ready-to-code") == MAINTAINER
    )


def test_an_issue_written_by_a_bot_without_write_access_goes_nowhere():
    assert trust("opened", "spam[bot]", "spam[bot]", sender_type="Bot", author_type="Bot") == NONE


@pytest.mark.parametrize("overrides", [{"state": "closed"}, {"pull_request": {}}])
def test_closed_issues_and_pull_requests_are_refused(overrides):
    assert trust("opened", "maintainer", "maintainer", **overrides) == NONE


def test_other_actions_are_refused():
    assert trust("edited", "maintainer", "maintainer") == NONE


def labeled(label, login, kind="User"):
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": login, "type": kind}}


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([labeled("ready-to-code", "maintainer")], True),
        ([labeled("ready-to-code", "stranger")], False),
        ([labeled("ready-to-code", "maintainer", kind="Bot")], False),
        ([labeled("ready-for-triage", "maintainer")], False),
        ([{"event": "labeled", "label": {"name": "ready-to-code"}, "actor": None}], False),
        ([{"event": "closed", "actor": {"login": "maintainer", "type": "User"}}], False),
    ],
)
def test_only_a_person_with_write_access_adding_the_label_vouches(monkeypatch, history, expected):
    monkeypatch.setattr(guard, "api_pages", lambda path: history)
    monkeypatch.setattr(guard, "can_write", lambda repo, login: login in WRITERS)
    assert guard.maintainer_added("o/r", 7, "ready-to-code") is expected
