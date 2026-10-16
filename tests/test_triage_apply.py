"""Verdict to label: only confident bug-class verdicts reach the code agent."""

import json

import pytest

import triage_apply
from common import NEEDS_INFO, READY_TO_CODE, TRIAGED
from triage_apply import (
    DECIDE,
    NEXT_STEP,
    QUEUED,
    TYPE_LABEL,
    VOUCH,
    comment,
    decide,
    settle,
    starts_now,
    type_labels,
)

AUTO = {"bug", "documentation", "performance"}


def verdict(category, confidence=0.9, needs_info=False, needs_decision=False):
    return {
        "category": category,
        "confidence": confidence,
        "needs_info": needs_info,
        "needs_decision": needs_decision,
        "summary": "s",
    }


def labeled(name, actor_type):
    return {"event": "labeled", "label": {"name": name}, "actor": {"type": actor_type}}


@pytest.mark.parametrize("category", sorted(AUTO))
def test_bug_class_work_goes_to_code(category):
    assert decide(verdict(category), AUTO, 0.7) == READY_TO_CODE


def test_feature_waits_for_a_maintainer():
    assert decide(verdict("feature"), AUTO, 0.7) == TRIAGED


def test_low_confidence_bug_waits_for_a_maintainer():
    assert decide(verdict("bug", confidence=0.5), AUTO, 0.7) == TRIAGED


@pytest.mark.parametrize("category", ["question", "unclear"])
def test_questions_need_info(category):
    assert decide(verdict(category), AUTO, 0.7) == NEEDS_INFO


def test_a_feature_with_an_open_question_still_waits_as_triaged():
    """Most needs-info issues were features: the label hid them from the work waiting on a decision."""
    assert decide(verdict("feature", needs_info=True), AUTO, 0.7) == TRIAGED


def test_bug_that_needs_info_is_not_coded():
    assert decide(verdict("bug", needs_info=True), AUTO, 0.7) == NEEDS_INFO


def test_a_bug_that_needs_a_decision_stays_a_bug_and_waits():
    """rotv#770: a stale flag with three possible fixes was relabeled a feature to keep it from coding."""
    result = verdict("bug", needs_decision=True)
    assert decide(result, AUTO, 0.7) == TRIAGED
    assert "bug (90%), needs a maintainer's decision" in comment(result, TRIAGED, DECIDE)


def test_the_decision_next_step_points_at_ready_to_code_not_another_triage():
    assert "ready-to-code" in DECIDE and "ready-for-triage" not in DECIDE


def test_a_missing_fact_outranks_a_decision():
    assert decide(verdict("bug", needs_info=True, needs_decision=True), AUTO, 0.7) == NEEDS_INFO


def test_a_verdict_without_needs_decision_still_codes():
    result = verdict("bug")
    del result["needs_decision"]
    assert decide(result, AUTO, 0.7) == READY_TO_CODE


def test_a_type_label_a_person_set_wins():
    assert type_labels("feature", [labeled("bug", "User")], {"bug"}) == ((), ())


def test_a_person_label_drops_a_bot_label_that_disagrees():
    history = [labeled("bug", "User"), labeled("enhancement", "Bot")]
    assert type_labels("feature", history, {"bug", "enhancement"}) == ((), ("enhancement",))


def test_without_a_person_label_ashigaru_replaces_its_own_earlier_reading():
    assert type_labels("bug", [labeled("enhancement", "Bot")], {"enhancement"}) == (
        ("bug",),
        ("enhancement",),
    )


def test_a_label_with_no_history_counts_as_a_persons():
    assert type_labels("feature", [], {"bug"}) == ((), ())


def test_an_unlabeled_issue_gets_the_verdict_type():
    assert type_labels("feature", [], set()) == (("enhancement",), ())
    assert type_labels("unclear", [], set()) == ((), ())


@pytest.mark.parametrize("bad", [{}, {"category": "bug"}, {"category": "rm -rf", "confidence": 1}])
def test_malformed_verdict_goes_to_a_human(bad):
    assert decide(bad, AUTO, 0.7) == TRIAGED


def test_category_can_be_taken_out_of_the_automatic_set():
    assert decide(verdict("performance"), {"bug"}, 0.7) == TRIAGED


def test_comment_names_the_next_step():
    text = comment(verdict("feature"), TRIAGED, NEXT_STEP[TRIAGED])
    assert "feature (90%)" in text and "ready-to-code" in text


def test_a_newly_opened_bug_goes_straight_to_the_code_agent():
    assert starts_now(READY_TO_CODE, "fresh")


def test_a_bug_triaged_from_the_backlog_waits_for_the_hourly_budget():
    assert not starts_now(READY_TO_CODE, "backlog")
    assert "Queued" in comment(verdict("bug"), READY_TO_CODE, QUEUED)


def test_nothing_but_ready_to_code_ever_starts_the_code_agent():
    assert not starts_now(TRIAGED, "fresh")
    assert not starts_now(NEEDS_INFO, "fresh")


def test_a_bug_reported_from_outside_waits_for_a_maintainer_to_vouch():
    assert settle(READY_TO_CODE, "outside") == (TRIAGED, VOUCH)
    assert not starts_now(settle(READY_TO_CODE, "outside")[0], "fresh")


@pytest.mark.parametrize("state", [TRIAGED, NEEDS_INFO])
def test_other_verdicts_on_outside_issues_are_left_as_they_are(state):
    assert settle(state, "outside") == (state, None)


def test_a_maintainers_issue_is_not_held_back():
    assert settle(READY_TO_CODE, "maintainer") == (READY_TO_CODE, None)


def test_every_category_with_a_meaning_gets_a_type_label():
    assert TYPE_LABEL == {
        "bug": "bug",
        "documentation": "documentation",
        "performance": "performance",
        "feature": "enhancement",
        "question": "question",
    }


def run_main(monkeypatch, result, labels, history):
    calls = {"pages": [], "state": None, "comment": None, "outputs": {}, "ensured": []}
    monkeypatch.setattr(triage_apply, "ensure_labels", lambda repo, names: calls["ensured"].extend(names))
    monkeypatch.setattr(triage_apply, "api", lambda path: {"labels": [{"name": n} for n in labels]})

    def pages(path):
        calls["pages"].append(path)
        return history

    def state(repo, number, label, *, extra=(), remove=()):
        calls["state"] = (label, extra, remove)

    monkeypatch.setattr(triage_apply, "api_pages", pages)
    monkeypatch.setattr(triage_apply, "set_state", state)
    monkeypatch.setattr(triage_apply, "gh", lambda *args: calls.update(comment=args[-1]))
    monkeypatch.setattr(triage_apply, "set_output", lambda key, value: calls["outputs"].update({key: value}))
    argv = ["triage_apply.py", "--repo", "o/r", "--issue", "770", "--result", json.dumps(result)]
    monkeypatch.setattr("sys.argv", argv)
    assert triage_apply.main() == 0
    return calls


def test_main_keeps_a_reporters_bug_label_and_asks_for_the_decision(monkeypatch):
    history = [labeled("bug", "User"), labeled("enhancement", "Bot")]
    calls = run_main(monkeypatch, verdict("bug", needs_decision=True), {"bug", "enhancement"}, history)
    assert calls["state"] == (TRIAGED, (), ("enhancement",))
    assert calls["comment"].endswith(DECIDE)
    assert calls["outputs"] == {"state": TRIAGED, "auto_code": "false"}


def test_main_skips_the_event_history_when_no_type_label_is_present(monkeypatch):
    calls = run_main(monkeypatch, verdict("bug"), set(), [])
    assert calls["pages"] == []
    assert calls["state"] == (READY_TO_CODE, ("bug",), ())
    assert calls["outputs"] == {"state": READY_TO_CODE, "auto_code": "true"}


def test_main_creates_the_label_a_needs_info_comment_asks_for(monkeypatch):
    calls = run_main(monkeypatch, verdict("question"), set(), [])
    assert calls["state"][0] == NEEDS_INFO
    assert calls["comment"].endswith(NEXT_STEP[NEEDS_INFO])
    assert calls["ensured"] == ["ready-for-triage"]


def test_main_creates_no_extra_label_for_other_verdicts(monkeypatch):
    assert run_main(monkeypatch, verdict("bug"), set(), [])["ensured"] == []
