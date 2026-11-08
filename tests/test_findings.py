"""What a fix round must answer, and when the rounds are spent (profile IV)."""

import json
import subprocess

import findings
from findings import (
    ANSWER_MARKER,
    LOG_EXCERPT_LINES,
    LOG_TAIL_LINES,
    MAX_ROUNDS,
    REBUILD_MARKER,
    ROUND_MARKER,
    excerpt,
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
BOT = "ashigaru-crunchtools"


def inline(comment_id, login=REVIEWER, reply_to=None):
    return {"id": comment_id, "user": {"login": login}, "in_reply_to_id": reply_to,
            "path": "a.py", "line": 3, "body": "**HIGH** finding"}  # fmt: skip


def check(name, status="completed", conclusion="success", run_id="100", suite=None):
    """One workflow run is one check suite. `suite` is the suite's own conclusion, which
    the per-commit listing leaves out."""
    return {"name": name, "status": status, "conclusion": conclusion,
            "details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/7",
            "check_suite": {"id": int(run_id)} | ({} if suite is None else {"conclusion": suite}),
            }  # fmt: skip


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
    got = issue_context(issue, prs, maintainer_check(REVIEWER, "b", lambda login: login == "maint"), BOT)
    assert [c["body"] for c in got] == ["new"]


def test_rounds_are_counted_from_marker_comments():
    rounds = [{"body": f"{ROUND_MARKER}\nround"}, {"body": ROUND_MARKER}]
    assert rounds_used([*rounds, {"body": "chatter"}], BOT) == len(rounds)


def test_a_fresh_rebuild_starts_the_round_count_again():
    ours = {"login": f"{BOT}[bot]"}
    rebuild = {"body": f"{REBUILD_MARKER}\nrebuilt", "user": ours}
    spent = [{"body": ROUND_MARKER}] * MAX_ROUNDS
    comments = [*spent, rebuild, {"body": ROUND_MARKER}]
    assert rounds_used(comments, BOT) == 1
    forged = [*spent, {**rebuild, "user": {"login": "outsider"}}]
    assert rounds_used(forged, BOT) == MAX_ROUNDS
    prs = [
        {"body": ROUND_MARKER, "created_at": "2026-01-01"},
        {"body": REBUILD_MARKER, "created_at": "2026-01-02", "user": ours},
    ]
    issue = [human({"body": "before", "created_at": "2026-01-01T12"}, "m"),
             human({"body": "after", "created_at": "2026-01-03"}, "m")]  # fmt: skip
    got = issue_context(issue, prs, maintainer_check(REVIEWER, "b", lambda login: True), BOT)
    assert [c["body"] for c in got] == ["after"]


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


def test_a_best_effort_job_does_not_make_the_branch_red():
    """`continue-on-error: true` fails the job's check run and still concludes the suite a success."""
    runs = [check("mirror push", conclusion="failure", suite="success"),
            check("CI", conclusion="failure", suite="failure", run_id="200")]  # fmt: skip
    assert [run["name"] for run in failed(runs, own_run_id="555")] == ["CI"]


def test_a_suite_that_could_not_be_read_still_counts_as_a_failure():
    """Unknown is not best effort."""
    assert [run["name"] for run in failed([check("CI", conclusion="failure")], "555")] == ["CI"]


def test_the_suites_are_read_across_pages(monkeypatch):
    pages = [{"check_suites": [{"id": 1, "conclusion": "success"}]},
             {"check_suites": [{"id": 2, "conclusion": None}]}]  # fmt: skip
    monkeypatch.setattr(findings, "gh", lambda *args: json.dumps(pages))
    assert findings.suite_conclusions("o/r", "abc") == {1: "success", 2: None}


def test_an_unreadable_listing_leaves_every_failure_counted(monkeypatch):
    def refuse(*args):
        raise subprocess.CalledProcessError(1, args, stderr="gone")

    monkeypatch.setattr(findings, "gh", refuse)
    assert findings.suite_conclusions("o/r", "abc") == {}


def test_the_listing_is_not_read_when_nothing_failed(monkeypatch):
    monkeypatch.setattr(findings, "suite_conclusions", forbid_suites)
    monkeypatch.setattr(findings, "gh", lambda *args: json.dumps([{"check_runs": [check("CI")]}]))
    assert [run["name"] for run in findings.check_runs("o/r", "abc")] == ["CI"]


def forbid_suites(repo, sha):
    raise AssertionError("the check suites were read with nothing failing")


def test_the_conclusions_reach_every_run_on_their_suite():
    runs = [check("a", conclusion="failure"), check("b", conclusion="failure"), check("c")]
    filled = findings.with_suites(runs, {100: "success"})
    assert [run["check_suite"]["conclusion"] for run in filled] == ["success"] * 3
    assert failed(filled, own_run_id="555") == []


def test_pending_ignores_this_run():
    runs = [check("CI", status="in_progress", conclusion=None),
            check("Fix / Fix agent", status="in_progress", conclusion=None, run_id="555")]  # fmt: skip
    assert pending(runs, own_run_id="555") == ["CI"]


def test_model_escalates_at_the_configured_round():
    assert [model_for(n, "sonnet", "opus", 3) for n in (1, 2, 3, 5)] == ["sonnet", "sonnet", "opus", "opus"]


def test_excerpt_finds_a_failure_far_above_a_passing_tail():
    log = ["sdlc: lane main: mypy"] + [f"build step {i}" for i in range(200)]
    log += ['tests/test_x.py:86: error: Module has no attribute "PROC"  [attr-defined]']
    log += ["sdlc: mypy FAILED after 2s"]
    log += [f"image step {i}" for i in range(500)] + ["== image-tests passed in 1s"]
    text = excerpt("\n".join(log))
    assert 'Module has no attribute "PROC"' in text
    assert "sdlc: mypy FAILED" in text
    assert "lines omitted" in text


def test_excerpt_without_a_marker_is_the_tail():
    log = [f"line {i}" for i in range(400)]
    assert excerpt("\n".join(log)).splitlines() == log[-LOG_TAIL_LINES:]


def test_excerpt_is_capped_and_says_so():
    log = [f"error: problem {i}" for i in range(LOG_EXCERPT_LINES * 2)]
    lines = excerpt("\n".join(log)).splitlines()
    assert len(lines) == LOG_EXCERPT_LINES + 1
    assert lines[-1] == f"[… {LOG_EXCERPT_LINES} lines omitted …]"


def test_excerpt_keeps_distant_failures_separate_and_counts_the_gaps():
    log = [f"ok {i}" for i in range(300)]
    log[100] = "error: first"
    log[250] = "error: second"
    lines = excerpt("\n".join(log)).splitlines()
    assert "error: first" in lines and "error: second" in lines
    assert lines[0] == "[… 60 lines omitted …]"
    # The first window ends after line 110 (100 + 10), the second starts at 210 (250 - 40).
    assert "[… 99 lines omitted …]" in lines
    assert lines[-1] == "[… 39 lines omitted …]"


def test_excerpt_cap_keeps_every_window_when_there_are_many():
    log = []
    for i in range(10):
        log += [f"ok {i}-{j}" for j in range(200)] + [f"error: failure {i}"] * 40
    text = excerpt("\n".join(log))
    assert all(f"error: failure {i}" in text for i in range(10))
    assert len(text.splitlines()) <= LOG_EXCERPT_LINES + 11


def test_excerpt_with_more_windows_than_the_cap_keeps_the_first_ones_and_counts_the_rest():
    count = LOG_EXCERPT_LINES + 5
    log = []
    for i in range(count):
        log += [f"ok {i}-{j}" for j in range(200)] + [f"error: failure {i}"]
    text = excerpt("\n".join(log))
    lines = text.splitlines()
    assert "error: failure 0" in lines
    assert f"error: failure {LOG_EXCERPT_LINES - 1}" in lines
    assert f"error: failure {LOG_EXCERPT_LINES}" not in lines
    # One line per window (share 1) is kept, so everything after the last kept hit is
    # counted: the 5 dropped windows of 201 lines each.
    dropped = (count - LOG_EXCERPT_LINES) * 201
    assert lines[-1] == f"[… {dropped} lines omitted …]"


def test_only_pull_requests_the_app_opened_are_touched():
    assert opened_by({"author": {"login": "app/ashigaru-crunchtools"}}, "ashigaru-crunchtools")
    assert not opened_by({"author": {"login": "somebody"}}, "ashigaru-crunchtools")


def test_a_pull_request_merged_during_the_wait_is_nothing_to_do(monkeypatch, tmp_path):
    outputs = {}

    def fake_gh(*args, **_):
        if args[:2] == ("pr", "list"):
            return json.dumps([{"number": 7, "headRefOid": "abc", "author": {"login": BOT}}])
        assert args[:2] == ("pr", "view")
        return json.dumps({"state": "MERGED", "mergeCommit": {"oid": "deadbeef"}})

    out = tmp_path / "f.json"
    monkeypatch.setattr(findings, "gh", fake_gh)
    monkeypatch.setattr(findings, "check_runs", lambda *_: [])
    monkeypatch.setattr(findings, "set_output", outputs.__setitem__)
    monkeypatch.setattr(
        "sys.argv", ["findings", "--repo", "o/r", "--branch", "ashigaru/issue-1", "--out", str(out)]
    )
    assert findings.main() == 0
    assert outputs == {"action": "none"}
    assert not out.exists()
