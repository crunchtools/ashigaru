"""Prompt assembly: no slot injection, no comments from strangers."""

import subprocess

import prompt
from prompt import comment_text, render, trusted


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
    assert comment_text(trusted("o/r", comments)) == "[maintainer] keep"
    assert comment_text(trusted("o/r", [])) == "(none)"


def test_a_long_thread_is_cut_to_the_latest_comments(monkeypatch):
    monkeypatch.setattr(prompt, "can_write", lambda repo, login: True)
    comments = [{"user": {"login": "m", "type": "User"}, "body": f"c{n}"} for n in range(50)]
    kept = comment_text(trusted("o/r", comments)).split("\n\n")
    assert len(kept) == prompt.COMMENTS_KEPT and kept[-1] == "[m] c49"


def pull_request(monkeypatch, body, files, diff):
    pull = {"user": {"login": "visitor"}, "base": {"ref": "release-1"}, "title": "T", "body": body}
    listing = [{"filename": name} for name in files]
    monkeypatch.setattr(
        prompt, "api", lambda path: listing if path.endswith(f"per_page={prompt.FILES_KEPT}") else pull
    )
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
    values = pull_request(monkeypatch, "b" * (prompt.BODY_LIMIT + 9), ["a.py"], "d" * (prompt.DIFF_LIMIT + 1))
    assert len(values["body"]) == prompt.BODY_LIMIT
    assert values["diff"] == "d" * prompt.DIFF_LIMIT + prompt.CUT


def test_a_diff_github_will_not_render_does_not_stop_the_review(monkeypatch):
    def refuse(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 406: diff exceeded the maximum")

    pull = {"user": {"login": "v"}, "base": {"ref": "main"}, "title": "T"}
    monkeypatch.setattr(prompt, "api", lambda path: [] if "/files" in path else pull)
    monkeypatch.setattr(prompt, "gh", refuse)
    assert prompt.pull_values("o/r", 9)["diff"] == prompt.NO_DIFF


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
UPLOAD = "https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000001"
SIGNED = "https://private-user-images.githubusercontent.com/1/2-3.png?jwt=a&amp;b=c"


def test_only_images_github_serves_are_taken_from_the_rendered_issue():
    rendered = (
        f'<p><img width="10" alt="Image" src="{UPLOAD}" style="x"></p>'
        f'<img src="{SIGNED}">'
        '<img src="https://example.com/a.png">'
        '<img src="https://githubusercontent.com.example.com/a.png">'
        '<a href="https://github.com/user-attachments/assets/not-an-image">link</a>'
    )
    assert prompt.image_sources({"body_html": rendered}, {"body_html": None}, {}) == [
        UPLOAD,
        "https://private-user-images.githubusercontent.com/1/2-3.png?jwt=a&b=c",
    ]


def test_an_image_is_recognised_by_its_bytes():
    assert prompt.image_kind(PNG) == "png"
    assert prompt.image_kind(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert prompt.image_kind(b"GIF89a") == "gif"
    assert prompt.image_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert prompt.image_kind(b"<html>not an image</html>") is None


def test_images_are_saved_and_listed_for_the_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt, "fetch", lambda url: PNG)
    listed = prompt.attachments(7, [UPLOAD, UPLOAD, UPLOAD + "2"], tmp_path / "images")
    first, second = (tmp_path / "images" / "issue-7-1.png", tmp_path / "images" / "issue-7-2.png")
    assert listed == f"{first}\n{second}"
    assert first.read_bytes() == PNG


def test_a_bad_download_is_skipped_and_never_fails_the_run(monkeypatch, tmp_path):
    answers = {
        "gone": OSError("404"),
        "page": b"<html>",
        "huge": b"\x89PNG\r\n\x1a\n" + b"0" * prompt.IMAGE_LIMIT,
        "good": PNG,
    }

    def fetch(url):
        if isinstance(answers[url], Exception):
            raise answers[url]
        return answers[url]

    monkeypatch.setattr(prompt, "fetch", fetch)
    assert prompt.attachments(7, list(answers), tmp_path) == str(tmp_path / "issue-7-1.png")
    assert prompt.attachments(7, ["gone"], tmp_path / "empty") == "(none)"
    assert not (tmp_path / "empty").exists()


def test_no_more_than_a_few_images_are_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt, "fetch", lambda url: PNG)
    listed = prompt.attachments(7, [f"u{n}" for n in range(20)], tmp_path)
    assert len(listed.splitlines()) == prompt.IMAGES_KEPT


def test_dead_links_do_not_buy_more_downloads(monkeypatch, tmp_path):
    tried = []

    def fetch(url):
        tried.append(url)
        raise OSError("timed out")

    monkeypatch.setattr(prompt, "fetch", fetch)
    assert prompt.attachments(7, [f"u{n}" for n in range(20)], tmp_path) == "(none)"
    assert len(tried) == prompt.IMAGES_KEPT


def test_a_download_has_a_timeout_and_a_size_bound(monkeypatch):
    asked = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            asked["size"] = size
            return PNG

    def urlopen(url, timeout):
        asked["url"], asked["timeout"] = url, timeout
        return Response()

    monkeypatch.setattr(prompt.urllib.request, "urlopen", urlopen)
    assert prompt.fetch(UPLOAD) == PNG
    assert asked == {"url": UPLOAD, "timeout": prompt.IMAGE_TIMEOUT, "size": prompt.IMAGE_LIMIT + 1}


def issue_with_images(monkeypatch, tmp_path, images):
    issue = {"title": "T", "body": "B", "body_html": f'<img src="{UPLOAD}">'}
    comments = [
        {"user": {"login": "maintainer", "type": "User"}, "body": "m", "body_html": f'<img src="{UPLOAD}m">'},
        {"user": {"login": "stranger", "type": "User"}, "body": "s", "body_html": f'<img src="{UPLOAD}s">'},
    ]
    fetched = []
    monkeypatch.setattr(prompt, "api", lambda path, *args: issue)
    monkeypatch.setattr(prompt, "api_pages", lambda path, *args: comments)
    monkeypatch.setattr(prompt, "can_write", lambda repo, login: login == "maintainer")
    monkeypatch.setattr(prompt, "fetch", lambda url: fetched.append(url) or PNG)
    return prompt.issue_values("o/r", 7, images), fetched


def test_a_strangers_comment_image_is_not_downloaded(monkeypatch, tmp_path):
    values, fetched = issue_with_images(monkeypatch, tmp_path, tmp_path)
    assert fetched == [UPLOAD, UPLOAD + "m"]
    assert values["attachments"].splitlines() == [
        str(tmp_path / "issue-7-1.png"),
        str(tmp_path / "issue-7-2.png"),
    ]


def test_without_a_directory_nothing_is_downloaded(monkeypatch, tmp_path):
    values, fetched = issue_with_images(monkeypatch, tmp_path, None)
    assert values["attachments"] == "(none)" and fetched == []
