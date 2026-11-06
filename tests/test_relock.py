"""A manifest bump gets a fresh lockfile on the publish side; anything odd hands over."""

import subprocess
from pathlib import Path

import relock as relock_module
from common import touches_protected
from relock import relock, wanted

MANIFEST = (
    "diff --git a/pyproject.toml b/pyproject.toml\n"
    "--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1 @@\n-a\n+b\n"
)
OTHER = MANIFEST.replace("pyproject.toml", "src/app.py")
WORKFLOW = MANIFEST.replace("pyproject.toml", ".github/workflows/ci.yml")


def repo(tmp_path: Path, lock: bool = True) -> Path:
    (tmp_path / "pyproject.toml").write_text("x")
    if lock:
        (tmp_path / "uv.lock").write_text("x")
    return tmp_path


def done(code=0, err=""):
    return lambda command, **kwargs: subprocess.CompletedProcess(command, code, "", err)


def test_only_a_changed_manifest_with_a_lockfile_triggers_a_relock(tmp_path):
    assert wanted(MANIFEST, repo(tmp_path)) == ("uv.lock", ("uv", "lock", "--no-build"))
    assert wanted(OTHER, repo(tmp_path)) is None
    bare = tmp_path / "bare"
    bare.mkdir()
    assert wanted(MANIFEST, repo(bare, lock=False)) is None


def test_a_patch_that_deletes_the_lockfile_still_gets_it_regenerated(tmp_path):
    deleted = "diff --git a/uv.lock b/uv.lock\ndeleted file mode 100644\n"
    assert wanted(MANIFEST + deleted, repo(tmp_path, lock=False)) == ("uv.lock", ("uv", "lock", "--no-build"))


def test_no_command_runs_when_nothing_needs_a_relock(tmp_path):
    def boom(command, **kwargs):
        raise AssertionError("ran")

    assert relock(OTHER, repo(tmp_path), run=boom) is None


def test_a_clean_relock_stages_the_lockfile(tmp_path):
    staged = []
    got = relock(MANIFEST, repo(tmp_path), run=done(), strays=lambda root: {"uv.lock"},
                 stage=lambda root, name: staged.append(name))  # fmt: skip
    assert got is None and staged == ["uv.lock"]


def test_a_failed_resolution_hands_over_with_the_output(tmp_path):
    got = relock(MANIFEST, repo(tmp_path), run=done(1, "no solution"), strays=lambda root: set())
    assert "no solution" in got and "stale" in got


def test_a_relock_that_touches_other_files_hands_over(tmp_path):
    staged = []
    got = relock(MANIFEST, repo(tmp_path), run=done(), strays=lambda root: {"uv.lock", "x.py"},
                 stage=lambda root, name: staged.append(name))  # fmt: skip
    assert "x.py" in got and not staged


def test_a_missing_uv_hands_over_not_a_traceback(tmp_path):
    def missing(command, **kwargs):
        raise FileNotFoundError

    assert "not available" in relock(MANIFEST, repo(tmp_path), run=missing)


def test_a_relock_is_no_route_to_the_workflows(tmp_path):
    assert touches_protected(WORKFLOW)  # a patch naming .github/ is refused before any relock runs
    got = relock(MANIFEST, repo(tmp_path), run=done(), strays=lambda root: {".github/workflows/ci.yml"})
    assert ".github/workflows/ci.yml" in got


def test_the_lock_command_refuses_to_build_a_dependency():
    """Resolution is not inert: uv calls a requirement's PEP 517 backend to read metadata it
    cannot read statically, and this job holds the app token. Measured: without the flag a path
    dependency declaring `dynamic = ["version"]` ran its setup.py during `uv lock`."""
    for _manifest, _lockfile, command in relock_module.ECOSYSTEMS:
        assert "--no-build" in command


def test_the_lock_command_does_not_inherit_the_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "ghs_the-app-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    passed = {}

    def record(command, **kwargs):
        passed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    relock(MANIFEST, repo(tmp_path), run=record, strays=lambda root: {"uv.lock"}, stage=lambda *a: None)
    env = passed["env"]
    assert "GH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["UV_NO_BUILD"] == "1"


def test_the_agent_still_has_no_shell():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/code.yml").read_text()
    line = next(row for row in text.splitlines() if "default:" in row and "Read,Edit" in row)
    assert "Bash" not in line
