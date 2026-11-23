"""A fix round pushes with a lease on the head it started from, so a moved branch is refused."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import fix_publish


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          check=True, capture_output=True, text=True)  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def round_(tmp_path, monkeypatch):
    """A clone of a bare remote on branch `b`, a one-file patch, and the round's inputs."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "b", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-b", "b")
    (work / "a.txt").write_text("one\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    git(work, "push", "-q", "origin", "HEAD:b")
    head = git(work, "rev-parse", "HEAD")
    (work / "b.txt").write_text("fix\n")
    git(work, "add", "-A")
    (tmp_path / "change.patch").write_text(git(work, "diff", "--cached", "--binary", "HEAD") + "\n")
    git(work, "reset", "-q", "--hard", "HEAD")
    (tmp_path / "findings.json").write_text(json.dumps({
        "pr": 5, "round": 1, "head": head, "failed_checks": [],
        "findings": [{"id": 7, "kind": "inline", "body": "x"}],
    }))  # fmt: skip
    (tmp_path / "result.json").write_text(
        json.dumps({"summary": "s", "dispositions": [{"id": 7, "disposition": "fixed"}]})
    )
    calls = []
    monkeypatch.setattr(fix_publish, "observing", lambda repo: False)
    monkeypatch.setattr(fix_publish, "gh", lambda *args: calls.append(args))
    monkeypatch.setattr(fix_publish, "set_state", lambda *args: calls.append(("state", *args)))
    monkeypatch.setattr(sys, "argv", ["fix_publish", "--repo", "o/r", "--mode", "fix", "--branch", "b",
                                      "--findings", str(tmp_path / "findings.json"),
                                      "--patch", str(tmp_path / "change.patch"),
                                      "--result", str(tmp_path / "result.json")])  # fmt: skip
    monkeypatch.chdir(work)
    return remote, head, calls


def test_an_unmoved_branch_takes_the_fix(round_):
    remote, head, calls = round_
    assert fix_publish.main() == 0
    pushed = git(remote, "rev-parse", "b")
    assert pushed != head and git(remote, "rev-parse", "b~1") == head
    assert any(c[-1] == f"body=fixed in {pushed}" for c in calls)


def test_a_moved_branch_is_refused_and_nothing_is_answered(round_, tmp_path):
    remote, head, calls = round_
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "b", str(remote), str(other)], check=True,
                   capture_output=True)  # fmt: skip
    (other / "a.txt").write_text("someone else\n")
    git(other, "commit", "-qam", "moved")
    git(other, "push", "-q", "origin", "HEAD:b")
    moved = git(remote, "rev-parse", "b")
    git(Path.cwd(), "fetch", "-q", "origin")  # a tracking ref that saw the move must not satisfy the lease
    assert fix_publish.main() == 0
    assert git(remote, "rev-parse", "b") == moved != head
    assert calls == []


def test_a_push_that_fails_for_another_reason_is_not_taken_for_a_moved_branch(round_):
    remote, _, _ = round_
    git(Path.cwd(), "remote", "set-url", "origin", str(remote.parent / "missing.git"))
    with pytest.raises(RuntimeError, match="push to b failed"):
        fix_publish.main()


@pytest.mark.parametrize(
    ("spoil", "why"),
    [
        ("conflict", "The fix no longer applies to the pull request branch."),
        ("stale_lock", "uv.lock is stale."),
    ],
)
def test_a_fix_that_cannot_be_committed_is_handed_off_and_pushes_nothing(round_, tmp_path, monkeypatch,
                                                                         spoil, why):  # fmt: skip
    remote, head, calls = round_
    if spoil == "conflict":
        (tmp_path / "change.patch").write_text(
            "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-not there\n+x\n"
        )
    else:
        monkeypatch.setattr(fix_publish, "relock", lambda patch, root: why)
    assert fix_publish.main() == 0
    assert git(remote, "rev-parse", "b") == head
    assert [c[-1] for c in calls if c[:2] == ("pr", "comment")] == [f"**Ashigaru stopped.** {why}"]
    assert not any(c[0] == "api" for c in calls)


def move(remote: Path, tmp_path: Path, files: dict) -> str:
    """Someone else's push to `b`; returns the new head."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "b", str(remote), str(other)], check=True,
                   capture_output=True)  # fmt: skip
    for name, text in files.items():
        (other / name).write_text(text)
    git(other, "add", "-A")
    git(other, "commit", "-qm", "moved")
    git(other, "push", "-q", "origin", "HEAD:b")
    return git(remote, "rev-parse", "b")


def test_a_branch_that_moved_before_checkout_yields_rather_than_handing_off(round_, tmp_path):
    remote, head, calls = round_
    moved = move(remote, tmp_path, {"b.txt": "theirs\n"})  # the patch creates b.txt: it would conflict
    git(Path.cwd(), "pull", "-q", "--ff-only", "origin", "b")  # the publish job checks out by name
    assert fix_publish.main() == 0
    assert git(remote, "rev-parse", "b") == moved
    assert calls == []


def dismiss_only(tmp_path: Path) -> None:
    (tmp_path / "change.patch").write_text("")
    (tmp_path / "result.json").write_text(json.dumps(
        {"summary": "s", "dispositions": [{"id": 7, "disposition": "not_a_bug", "reason": "r"}]}
    ))  # fmt: skip


def test_a_round_with_no_patch_answers_on_an_unmoved_branch(round_, tmp_path):
    _, _, calls = round_
    dismiss_only(tmp_path)
    assert fix_publish.main() == 0
    assert any(c[-1] == "body=not a bug: r" for c in calls)


def test_a_round_with_no_patch_yields_on_a_moved_branch(round_, tmp_path):
    remote, _, calls = round_
    dismiss_only(tmp_path)
    move(remote, tmp_path, {"a.txt": "someone else\n"})
    assert fix_publish.main() == 0
    assert calls == []


def test_a_round_with_no_patch_yields_on_a_deleted_branch(round_, tmp_path):
    remote, _, calls = round_
    dismiss_only(tmp_path)
    git(remote, "update-ref", "-d", "refs/heads/b")
    assert fix_publish.main() == 0
    assert calls == []
