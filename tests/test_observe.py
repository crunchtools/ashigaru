"""Watch-only mode: a repository with the `ashigaru-observe` topic records and writes nothing else."""

import json
import subprocess

import pytest

import fix_publish
import inbox
import publish
import release
import review_publish
import sweep
import triage_apply
from common import OBSERVE


def forbid(*_args, **_kwargs):
    raise AssertionError("a watched repository was written to")


@pytest.fixture
def recorded(monkeypatch):
    """Watch every repository and collect what is recorded; any other write fails the test."""
    calls = []
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr("common.observing", lambda repo: True)
    for module in (triage_apply, publish, fix_publish, review_publish):
        monkeypatch.setattr(module, "observing", lambda repo: True)
    monkeypatch.setattr(inbox, "observe", lambda repo, source, lines: calls.append((repo, source, lines)))
    for module in (triage_apply, publish, fix_publish, review_publish, release, sweep):
        for name in ("gh", "git", "set_state", "ensure_labels"):
            if name in vars(module):
                monkeypatch.setattr(module, name, forbid)
    return calls


def test_the_topic_is_read_from_the_repository(monkeypatch):
    import common

    monkeypatch.setattr(common, "api", lambda path: {"names": ["python", OBSERVE]})
    assert common.observing("o/r")
    monkeypatch.setattr(common, "api", lambda path: {"names": []})
    assert not common.observing("o/r")


def test_a_record_replaces_its_own_source_and_keeps_the_others():
    body = inbox.record_body("", "triage #1", ["Would label it `triaged`."])
    body = inbox.record_body(body, "code #2", ["Would open a pull request."])
    body = inbox.record_body(body, "triage #1", ["Would label it `ready-to-code`."])
    lines = [line for line in body.splitlines() if line.startswith("- **")]
    assert lines == [
        "- **code #2**: Would open a pull request.",
        "- **triage #1**: Would label it `ready-to-code`.",
    ]


def test_an_empty_record_is_not_opened_for_a_quiet_repository(monkeypatch):
    monkeypatch.setattr(inbox, "api", lambda path: [])
    monkeypatch.setattr(inbox, "publish", forbid)
    assert inbox.observe("o/r", "sweep", []) == "nothing to record"


def test_triage_records_and_labels_nothing(recorded, monkeypatch, capsys):
    monkeypatch.setattr(triage_apply, "api", lambda path: {"labels": []})
    monkeypatch.setattr(
        "sys.argv",
        ["triage_apply", "--repo", "o/r", "--issue", "7", "--result",
         json.dumps({"category": "bug", "confidence": 0.9, "summary": "s"})],
    )  # fmt: skip
    assert triage_apply.main() == 0
    assert recorded == [("o/r", "triage #7", ["Would label it `ready-to-code`, `bug` and comment: bug."])]
    assert "state=ready-to-code" in capsys.readouterr().out


def test_code_opens_no_branch_and_no_pull_request(recorded, monkeypatch, tmp_path):
    patch = tmp_path / "fix.patch"
    patch.write_text("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"status": "fixed", "summary": "s"}))
    monkeypatch.setattr(publish, "gh", lambda *args: "[]" if args[:2] == ("pr", "list") else forbid())
    monkeypatch.setattr(publish, "api", lambda path: {"state": "open", "title": "Broken"})
    monkeypatch.setattr(
        "sys.argv",
        ["publish", "--repo", "o/r", "--issue", "7", "--patch", str(patch), "--result", str(result),
         "--base", "main"],
    )  # fmt: skip
    assert publish.main() == 0
    assert recorded == [("o/r", "code #7", ["Would push `ashigaru/issue-7` and open a pull request: Broken"])]


def test_code_records_the_hand_off_it_would_have_made(recorded, monkeypatch, tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"status": "failed", "reason": "no idea"}))
    monkeypatch.setattr(publish, "gh", lambda *args: "[]" if args[:2] == ("pr", "list") else forbid())
    monkeypatch.setattr(publish, "api", lambda path: {"state": "open", "title": "Broken"})
    monkeypatch.setattr(
        "sys.argv",
        ["publish", "--repo", "o/r", "--issue", "7", "--patch", str(tmp_path / "none"),
         "--result", str(result), "--base", "main"],
    )  # fmt: skip
    assert publish.main() == 0
    assert recorded[0][2] == ["Would stop and hand it to a human: No fix was produced: no idea"]


def test_a_fix_round_pushes_and_answers_nothing(recorded, monkeypatch, tmp_path):
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"pr": 9, "round": 2, "findings": [], "failed_checks": []}))
    monkeypatch.setattr(
        "sys.argv", ["fix_publish", "--repo", "o/r", "--mode", "fix", "--findings", str(findings)]
    )
    assert fix_publish.main() == 0
    assert recorded == [("o/r", "fix #9", ["Would push fix round 2 and answer its findings."])]


def test_a_review_posts_no_comment(recorded, monkeypatch):
    def gh(*args):
        if args[:2] == ("pr", "view"):
            return json.dumps({"state": "OPEN"})
        forbid()

    monkeypatch.setattr(review_publish, "gh", gh)
    monkeypatch.setattr(review_publish, "api_pages", forbid)
    monkeypatch.setattr(
        "sys.argv",
        ["review_publish", "--repo", "o/r", "--pull", "4", "--sha", "abc1234",
         "--result", json.dumps({"recommendation": "merge", "summary": "ok"})],
    )  # fmt: skip
    assert review_publish.main() == 0
    assert recorded == [("o/r", "review #4", ["Would post a first-pass review: merge."])]


def test_release_cuts_nothing_in_a_watched_repository(recorded):
    items = [
        {"repo": "o/a", "observe": True, "action": "open", "current": "1.0.0", "next": "1.1.0"},
        {"repo": "o/b", "observe": True, "action": "publish", "current": "2.0.0"},
        {"repo": "o/c", "observe": True, "action": None, "current": "3.0.0"},
    ]
    release.carry_out(items, "2026-11-06")
    assert recorded == [
        ("o/a", "release", ["Would open a pull request releasing 1.1.0."]),
        ("o/b", "release", ["Would publish the release v2.0.0."]),
        ("o/c", "release", []),
    ]


def test_release_survey_marks_a_watched_repository(monkeypatch):
    monkeypatch.setattr(release, "fetch", lambda repo, path: None)
    repo = {"full_name": "o/r", "default_branch": "main", "topics": [OBSERVE]}
    assert release.survey(repo, "app/x", "")["observe"] is True
    assert release.survey({**repo, "topics": []}, "app/x", "")["observe"] is False


def test_the_sweep_does_not_queue_the_observe_issue():
    item = {"number": 1, "created_at": "2026-10-31T00:00:00Z", "user": {"type": "User"},
            "labels": [{"name": OBSERVE}]}  # fmt: skip
    assert sweep.candidates("o/r", [item]) == []


def test_the_sweep_lists_what_it_would_have_done():
    lines = sweep.would_do([{"number": 1}], [{"number": 2, "pull": 8}], [{"number": 3}])
    assert lines == [
        "Would queue #1 for triage.",
        "Would rebuild #2, whose pull request #8 conflicts.",
        "Would start the fix for #3.",
    ]


def test_the_sweep_closes_the_observe_issue_when_the_topic_is_gone(monkeypatch):
    held = [
        {"repo": "o/a", "number": 1, "url": "u", "body": ""},
        {"repo": "o/b", "number": 2, "url": "v", "body": ""},
    ]
    calls, written = [], []
    monkeypatch.setattr(sweep, "labeled_issues", lambda org, label: held)
    monkeypatch.setattr(sweep.inbox, "observe", lambda repo, source, lines: written.append((repo, lines)))
    monkeypatch.setattr(sweep, "gh", lambda *args: calls.append(args))
    kept = sweep.keep_watch("o", {"o/a": ["Would start the fix for #3."]}, dry_run=False)
    assert kept == [held[0]]
    assert written == [("o/a", ["Would start the fix for #3."])]
    assert [call[:4] for call in calls] == [("issue", "close", "2", "--repo")]


def test_a_dry_run_sweep_writes_nothing_for_watched_repositories(monkeypatch):
    monkeypatch.setattr(sweep, "labeled_issues", lambda org, label: [{"repo": "o/b", "number": 2}])
    monkeypatch.setattr(sweep.inbox, "observe", forbid)
    monkeypatch.setattr(sweep, "gh", forbid)
    assert sweep.keep_watch("o", {"o/a": ["x"]}, dry_run=True) == []


def test_an_unreadable_observe_search_changes_nothing(monkeypatch):
    def refuse(org, label):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")

    monkeypatch.setattr(sweep, "labeled_issues", refuse)
    monkeypatch.setattr(sweep, "gh", forbid)
    assert sweep.keep_watch("o", {"o/a": []}, dry_run=False) == []


def test_the_observe_issues_are_one_search_of_the_organization(monkeypatch):
    paths = []
    found = {"items": [{"repository_url": "https://api.github.com/repos/o/r", "number": 5,
                        "html_url": "https://github.com/o/r/issues/5", "body": None}]}  # fmt: skip
    monkeypatch.setattr(sweep, "api", lambda path: paths.append(path) or found)
    assert sweep.labeled_issues("o", OBSERVE) == [
        {"repo": "o/r", "number": 5, "url": "https://github.com/o/r/issues/5", "body": ""}
    ]
    assert paths == [
        "search/issues?q=org%3Ao%20is%3Aissue%20is%3Aopen%20label%3Aashigaru-observe&per_page=100"
    ]


def test_the_watching_part_lists_each_recorded_line_with_its_issue():
    body = inbox.record_body("", "triage #7", ["Would label it `triaged`."])
    part = inbox.watching([{"repo": "o/r", "number": 5, "url": "https://x/5", "body": body}])
    assert part.startswith("## Watching (1)\n")
    assert "- r `triage #7`: `Would label it 'triaged'.` ([record](https://x/5))" in part
    assert "## Watching (0)" in inbox.watching([])


def test_a_recorded_title_cannot_link_or_embed_in_the_inbox():
    title = "`[x](https://e.vil)` ![t](https://e.vil/p.png)"
    body = inbox.record_body("", "code #7", [f"Would open a pull request: {title}"])
    part = inbox.watching([{"repo": "o/r", "number": 5, "url": "https://x/5", "body": body}])
    row = next(line for line in part.splitlines() if line.startswith("- r "))
    shown = "'[x](https://e.vil)' ![t](https://e.vil/p.png)"
    assert row == f"- r `code #7`: `Would open a pull request: {shown}` ([record](https://x/5))"


def test_the_topic_lookup_fails_closed(monkeypatch):
    import common

    def refuse(path):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")

    monkeypatch.setattr(common, "api", refuse)
    with pytest.raises(subprocess.CalledProcessError):
        common.observing("o/r")


def test_an_unreadable_topic_stops_triage_before_any_write(monkeypatch):
    def refuse(repo):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")

    monkeypatch.setattr(triage_apply, "observing", refuse)
    monkeypatch.setattr(triage_apply, "api", lambda path: {"labels": []})
    monkeypatch.setattr(inbox, "observe", forbid)
    for name in ("gh", "set_state", "ensure_labels"):
        if name in vars(triage_apply):
            monkeypatch.setattr(triage_apply, name, forbid)
    monkeypatch.setattr(
        "sys.argv",
        ["triage_apply", "--repo", "o/r", "--issue", "7", "--result",
         json.dumps({"category": "bug", "confidence": 0.9, "summary": "s"})],
    )  # fmt: skip
    with pytest.raises(subprocess.CalledProcessError):
        triage_apply.main()


def test_a_first_record_opens_the_observe_issue(monkeypatch):
    paths, published = [], []
    monkeypatch.setattr(inbox, "api", lambda path: paths.append(path) or [])
    monkeypatch.setattr(inbox, "publish", lambda *args: published.append(args) or "opened")
    assert inbox.observe("o/r", "sweep", ["Would queue #1 for triage."]) == "opened"
    assert paths == [f"repos/o/r/issues?state=open&labels={OBSERVE}&per_page=100&sort=created&direction=asc"]
    expected = inbox.record_body("", "sweep", ["Would queue #1 for triage."])
    assert published == [("o/r", expected, OBSERVE, inbox.OBSERVE_TITLE)]


def test_a_record_updates_the_oldest_observe_issue_and_skips_pull_requests(monkeypatch):
    older = inbox.record_body("", "triage #2", ["Would label it `triaged`."])
    found = [
        {"number": 9, "body": inbox.record_body("", "triage #3", ["newer"])},
        {"number": 1, "body": "pull request", "pull_request": {}},
        {"number": 4, "body": older},
    ]
    published = []
    monkeypatch.setattr(inbox, "api", lambda path: found)
    monkeypatch.setattr(inbox, "publish", lambda *args: published.append(args) or "updated")
    assert inbox.observe("o/r", "sweep", ["Would start the fix for #5."]) == "updated"
    expected = inbox.record_body(older, "sweep", ["Would start the fix for #5."])
    assert published == [("o/r", expected, OBSERVE, inbox.OBSERVE_TITLE)]
    assert "**triage #2**" in expected and "newer" not in expected


def test_the_sweep_records_a_watched_repository_and_dispatches_only_the_others(monkeypatch):
    listed = [
        {"full_name": "o/watched", "archived": False, "has_issues": True, "topics": [OBSERVE]},
        {"full_name": "o/plain", "archived": False, "has_issues": True, "topics": []},
    ]

    def work(number):
        def found(repo, *rest):
            return [{"repo": repo, "number": number, "pull": 8, "created_at": "2026-10-01T00:00:00Z"}]

        return found

    dispatched, watched = [], []
    monkeypatch.setattr(sweep, "api_pages", lambda path: listed)
    monkeypatch.setattr(sweep, "enrolled", lambda repo: True)
    monkeypatch.setattr(sweep, "api", lambda path: [])
    monkeypatch.setattr(sweep, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sweep, "candidates", work(1))
    monkeypatch.setattr(sweep, "conflicted", work(2))
    monkeypatch.setattr(sweep, "waiting", work(3))
    monkeypatch.setattr(sweep, "dispatch", lambda args, now, *queues: dispatched.append(queues))
    monkeypatch.setattr(sweep, "plan_reviews", lambda *args: {})
    monkeypatch.setattr(sweep, "keep_watch", lambda org, lines, dry_run: watched.append(lines) or [])
    monkeypatch.setattr("sys.argv", ["sweep.py", "--org", "o"])
    assert sweep.main() == 0
    [(stuck, ready, untriaged)] = dispatched
    assert {item["repo"] for item in [*stuck, *ready, *untriaged]} == {"o/plain"}
    assert [len(stuck), len(ready), len(untriaged)] == [1, 1, 1]
    lines = sweep.would_do([{"number": 1}], [{"number": 2, "pull": 8}], [{"number": 3}])
    assert watched == [{"o/watched": lines}]
