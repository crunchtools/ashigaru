"""What a fix round must answer, and when the rounds are spent (profile IV)."""

from findings import (
    ANSWER_MARKER,
    ROUND_MARKER,
    failed,
    issue_context,
    maintainer_check,
    maintainer_findings,
    model_for,
    opened_by,
    pending,
    plan,
    rounds_used,
    unanswered,
)

REVIEWER = "github-actions[bot]"


def inline(comment_id, login=REVIEWER, reply_to=None):
    return {"id": comment_id, "user": {"login": login}, "in_reply_to_id": reply_to,
            "path": "a.py", "line": 3, "body": "**HIGH** finding"}  # fmt: skip


def check(name, status="completed", conclusion="success", run_id="100"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/7"}  # fmt: skip


def test_findings_without_a_reply_from_someone_else_are_unanswered():
    comments = [
        inline(1),
        inline(2),
        inline(3, login="ashigaru-crunchtools[bot]", reply_to=2),
        inline(4, login=REVIEWER, reply_to=1),  # the reviewer replying to itself is not an answer
    ]
    assert [finding["id"] for finding in unanswered(comments, REVIEWER)] == [1]


def human(item, login, user_type="User"):
    return {**item, "user": {"login": login, "type": user_type}}


def test_only_write_access_humans_are_maintainer_findings():
    writers = {"maint"}
    threads = [
        human(inline_comment, login)
        for inline_comment, login in [
            (inline(1), "maint"),
            (inline(2), "outsider"),
            (inline(3), "ashigaru-crunchtools"),
        ]  # fmt: skip
    ]
    reviews = [human({"id": 10, "body": "please redo", "state": "CHANGES_REQUESTED"}, "maint"),
               human({"id": 11, "body": "hi", "state": "COMMENTED"}, "outsider")]  # fmt: skip
    comments = [human({"id": 20, "body": "note"}, "maint"),
                human({"id": 21, "body": "x"}, "app", "Bot")]  # fmt: skip
    ok = maintainer_check(REVIEWER, "ashigaru-crunchtools", lambda login: login in writers)
    found = maintainer_findings(threads, reviews, comments, ok, "ashigaru-crunchtools")
    assert [(f["kind"], f["id"]) for f in found] == [("inline", 1), ("review", 10), ("comment", 20)]
    assert {f["origin"] for f in found} == {"maintainer"}


def test_answered_maintainer_comments_are_not_collected_again():
    threads = [human(inline(1), "maint"), human(inline(2, login="bot", reply_to=1), "bot", "Bot")]
    marker = ANSWER_MARKER.format(kind="comment", id=20)
    comments = [human({"id": 20, "body": "note"}, "maint"), human({"id": 30, "body": marker}, "bot", "Bot")]
    ok = maintainer_check(REVIEWER, "bot", lambda _: True)
    assert maintainer_findings(threads, [], comments, ok, "bot") == []


def test_an_outsider_cannot_answer_for_a_maintainer_finding():
    marker = ANSWER_MARKER.format(kind="comment", id=20)
    threads = [human(inline(1), "maint"), human(inline(2, login="outsider", reply_to=1), "outsider")]
    comments = [human({"id": 20, "body": "note"}, "maint"), human({"id": 30, "body": marker}, "outsider")]
    ok = maintainer_check(REVIEWER, "bot", lambda login: login == "maint")
    found = maintainer_findings(threads, [], comments, ok, "bot")
    assert [(f["kind"], f["id"]) for f in found] == [("inline", 1), ("comment", 20)]


def test_issue_comments_since_the_last_round_are_context():
    prs = [{"body": ROUND_MARKER, "created_at": "2026-01-02"}]
    issue = [human({"body": "old", "created_at": "2026-01-01"}, "maint"),
             human({"body": "new", "created_at": "2026-01-03"}, "maint"),
             human({"body": "spam", "created_at": "2026-01-03"}, "outsider")]  # fmt: skip
    got = issue_context(issue, prs, maintainer_check(REVIEWER, "b", lambda login: login == "maint"))
    assert [c["body"] for c in got] == ["new"]


def test_rounds_are_counted_from_marker_comments():
    rounds = [{"body": f"{ROUND_MARKER}\nround"}, {"body": ROUND_MARKER}]
    assert rounds_used([*rounds, {"body": "chatter"}]) == len(rounds)


def test_round_cap_hands_off_instead_of_fixing():
    assert plan([{"id": 1}], [], used=4, max_rounds=5) == "fix"
    assert plan([{"id": 1}], [], used=5, max_rounds=5) == "cap"
    assert plan([], [check("CI", conclusion="failure")], used=5, max_rounds=5) == "cap"


def test_nothing_to_answer_means_no_round():
    assert plan([], [], used=0, max_rounds=5) == "none"


def test_failed_checks_exclude_triage_and_this_run():
    runs = [
        check("CI / test", conclusion="failure"),
        check("Gatehouse triage / Gatehouse triage", conclusion="failure"),
        check("Fix / Fix agent", conclusion="failure", run_id="555"),
        check("Lint", conclusion="success"),
    ]
    assert [run["name"] for run in failed(runs, own_run_id="555")] == ["CI / test"]


def test_pending_ignores_this_run():
    runs = [check("CI", status="in_progress", conclusion=None),
            check("Fix / Fix agent", status="in_progress", conclusion=None, run_id="555")]  # fmt: skip
    assert pending(runs, own_run_id="555") == ["CI"]


def test_model_escalates_at_the_configured_round():
    assert [model_for(n, "sonnet", "opus", 3) for n in (1, 2, 3, 5)] == ["sonnet", "sonnet", "opus", "opus"]


def test_only_pull_requests_the_app_opened_are_touched():
    assert opened_by({"author": {"login": "app/ashigaru-crunchtools"}}, "ashigaru-crunchtools")
    assert not opened_by({"author": {"login": "somebody"}}, "ashigaru-crunchtools")
