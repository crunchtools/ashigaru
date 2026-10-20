"""What may become a pull request, and what may be pushed in a fix round."""

from unittest.mock import patch

from common import touches_protected
from fix_publish import next_step, replies
from publish import commit_message, credit, landed, refusal, stale

PATCH = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-a\n+b\n"
WORKFLOW_PATCH = PATCH.replace("src/app.py", ".github/workflows/ci.yml")
RENAME_OUT = "diff --git a/.github/workflows/ci.yml b/docs/ci.yml\nrename from x\nrename to y\n"


def test_a_fix_with_a_patch_is_published():
    assert refusal({"status": "fixed", "summary": "s"}, PATCH) is None


def test_no_fix_is_handed_to_a_human_with_the_reason():
    assert "already fixed" in refusal({"status": "no_change", "reason": "already fixed"}, "")
    assert refusal({}, PATCH) is not None


def test_a_claimed_fix_without_changes_is_refused():
    assert "changed no files" in refusal({"status": "fixed"}, "  \n")


ACTION_PATCH = PATCH.replace("src/app.py", ".github/actions/setup/action.yml")


def test_nothing_under_dot_github_is_ever_published():
    assert touches_protected(WORKFLOW_PATCH) and touches_protected(RENAME_OUT)
    assert touches_protected(ACTION_PATCH)
    assert not touches_protected(PATCH)
    assert ".github/" in refusal({"status": "fixed"}, WORKFLOW_PATCH)


def test_commit_message_closes_the_issue_and_carries_the_trailer():
    message = commit_message("Digest shows the wrong date", 12, "Off by one.")
    assert message.startswith("fix: Digest shows the wrong date (#12)\n")
    assert "Closes #12" in message and "Co-Authored-By:" in message
    assert "Reported by" not in message


def test_commit_message_thanks_the_reporter_before_closing():
    message = commit_message("Digest shows the wrong date", 12, "Off by one.", "Reported by @someone.")
    assert "Off by one.\n\nReported by @someone.\n\nCloses #12" in message


def test_an_outside_reporter_is_credited():
    with patch("publish.can_write", return_value=False):
        assert credit("o/r", {"login": "someone", "type": "User"}) == "Reported by @someone."


def test_maintainers_and_bots_are_not_credited():
    with patch("publish.can_write", return_value=True):
        assert credit("o/r", {"login": "fatherlinux", "type": "User"}) == ""
    with patch("publish.can_write", return_value=False):
        assert credit("o/r", {"login": "dependabot[bot]", "type": "Bot"}) == ""
        assert credit("o/r", {}) == ""


FINDINGS = [{"id": 1}, {"id": 2}, {"id": 3}]


def test_replies_use_the_two_forms_gatehouse_triage_accepts():
    dispositions = [
        {"id": 1, "disposition": "fixed", "reason": "x"},
        {"id": 2, "disposition": "not_a_bug", "reason": "the value is validated in parse()"},
    ]
    assert replies(FINDINGS, dispositions, "abc123") == {
        1: "fixed in abc123",
        2: "not a bug: the value is validated in parse()",
    }


def test_fixed_without_a_pushed_commit_is_not_answered():
    assert replies(FINDINGS, [{"id": 1, "disposition": "fixed", "reason": "x"}], None) == {}


def test_not_a_bug_without_a_reason_is_not_answered():
    assert replies(FINDINGS, [{"id": 2, "disposition": "not_a_bug", "reason": " "}], "abc") == {}


def test_dispositions_for_unknown_findings_are_ignored():
    assert replies(FINDINGS, [{"id": 99, "disposition": "not_a_bug", "reason": "r"}], "abc") == {}


def test_a_round_that_pushed_turns_auto_merge_back_on():
    assert next_step("abc123", 0, False) == "rearm"
    assert next_step("abc123", 0, True) == "rearm"
    assert next_step("abc123", 2, False) is None


def test_a_round_that_changed_nothing_with_work_left_goes_to_a_human():
    assert next_step(None, 1, False) == "hand_off"
    assert next_step(None, 0, True) == "hand_off"
    assert next_step(None, 0, False) is None


def test_a_branch_with_no_open_pull_request_may_be_built():
    assert stale([])


def test_an_open_pull_request_that_still_merges_is_never_overwritten():
    assert not stale([{"number": 5, "mergeable": "MERGEABLE"}])
    assert not stale([{"number": 5, "mergeable": "UNKNOWN"}])


def test_a_conflicting_pull_request_is_rebuilt():
    assert stale([{"number": 5, "mergeable": "CONFLICTING"}])


def test_an_issue_closed_by_our_merged_pull_request_is_not_handed_to_a_human():
    assert landed({"state": "closed"}, [{"number": 46}])


def test_an_open_issue_or_one_without_a_merged_pull_request_is_still_worked():
    assert not landed({"state": "open"}, [{"number": 46}])
    assert not landed({"state": "closed"}, [])
