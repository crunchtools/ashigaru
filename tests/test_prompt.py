"""Prompt assembly: no slot injection, no comments from strangers."""

import subprocess

import prompt
from prompt import render, trusted_comments


def test_inserted_text_is_not_scanned_for_slots():
    out = render("T: {{title}}\nF: {{findings}}", {"title": "{{findings}}", "findings": "secret"})
    assert out == "T: {{findings}}\nF: secret"


def test_unknown_slots_are_left_alone():
    assert render("{{nope}}", {}) == "{{nope}}"


def test_only_comments_from_accounts_with_write_access_reach_the_agent(monkeypatch):
    monkeypatch.setattr(prompt, "can_write", lambda repo, login: login == "maintainer")
    comments = [
        {"user": {"login": "maintainer", "type": "User"}, "body": "keep"},
        {"user": {"login": "stranger", "type": "User"}, "body": "drop"},
        {"user": {"login": "some[bot]", "type": "Bot"}, "body": "bot"},
    ]
    assert trusted_comments("o/r", comments) == "[maintainer] keep"
    assert trusted_comments("o/r", []) == "(none)"


def test_a_long_thread_is_cut_to_the_latest_comments(monkeypatch):
    monkeypatch.setattr(prompt, "can_write", lambda repo, login: True)
    comments = [{"user": {"login": "m", "type": "User"}, "body": f"c{n}"} for n in range(50)]
    kept = trusted_comments("o/r", comments).split("\n\n")
    assert len(kept) == prompt.COMMENTS_KEPT and kept[-1] == "[m] c49"


def pull_request(monkeypatch, body, files, diff):
    pull = {"user": {"login": "visitor"}, "base": {"ref": "release-1"}, "title": "T", "body": body}
    monkeypatch.setattr(prompt, "api", lambda path: pull)
    monkeypatch.setattr(prompt, "api_pages", lambda path: [{"filename": name} for name in files])
    monkeypatch.setattr(prompt, "gh", lambda *args: diff)
    return prompt.pull_values("o/r", 9)


def test_a_pull_request_reaches_the_reviewer_as_text(monkeypatch):
    values = pull_request(monkeypatch, "why", ["a.py", "b.py"], "diff --git a/a.py b/a.py")
    assert values == {
        "repo": "o/r",
        "number": "9",
        "author": "visitor",
        "base": "release-1",
        "title": "T",
        "body": "why",
        "files": "a.py\nb.py",
        "diff": "diff --git a/a.py b/a.py",
    }


def test_an_empty_pull_request_says_so(monkeypatch):
    values = pull_request(monkeypatch, None, [], "")
    assert (values["body"], values["files"]) == ("(empty)", "(none)")


def test_a_huge_pull_request_is_cut_and_marked(monkeypatch):
    names = [f"f{n}.py" for n in range(prompt.FILES_KEPT + 5)]
    values = pull_request(monkeypatch, "b" * (prompt.BODY_LIMIT + 9), names, "d" * (prompt.DIFF_LIMIT + 1))
    assert len(values["body"]) == prompt.BODY_LIMIT
    assert len(values["files"].splitlines()) == prompt.FILES_KEPT
    assert values["diff"] == "d" * prompt.DIFF_LIMIT + prompt.CUT


def test_a_diff_github_will_not_render_does_not_stop_the_review(monkeypatch):
    def refuse(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 406: diff exceeded the maximum")

    monkeypatch.setattr(
        prompt, "api", lambda path: {"user": {"login": "v"}, "base": {"ref": "main"}, "title": "T"}
    )
    monkeypatch.setattr(prompt, "api_pages", lambda path: [{"filename": "big.bin"}])
    monkeypatch.setattr(prompt, "gh", refuse)
    assert prompt.pull_values("o/r", 9)["diff"] == prompt.NO_DIFF
