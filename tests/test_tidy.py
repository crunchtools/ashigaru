"""What the publish step formats, and what it leaves alone."""

import subprocess
from pathlib import Path

from tidy import RUFF, configured, python_files, tidy

PATCH = (
    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
)


def repo(tmp_path: Path, ruff: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x=1\n")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n" if ruff else "[project]\n")
    return tmp_path


class Runner:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, self.code, "", "boom")


def test_only_existing_python_files_are_named(tmp_path):
    assert python_files(PATCH, repo(tmp_path)) == ["src/app.py"]


def test_ruff_configured_in_pyproject_or_its_own_file(tmp_path):
    root = repo(tmp_path, ruff=False)
    assert not configured(root)
    (root / "ruff.toml").write_text("")
    assert configured(root)


def test_formats_then_stages_the_patch_files(tmp_path):
    run = Runner()
    assert tidy(PATCH, repo(tmp_path), run=run) == "tidy: formatted 1 file(s)"
    check, fmt, add = run.calls
    assert check[: len(RUFF)] == list(RUFF) and "--fix" in check and check[-1] == "src/app.py"
    assert "format" in fmt and fmt[-1] == "src/app.py"
    assert add == ["git", "add", "--", "src/app.py"]


def test_a_repo_without_ruff_is_left_alone(tmp_path):
    run = Runner()
    assert tidy(PATCH, repo(tmp_path, ruff=False), run=run) == "tidy: nothing to format"
    assert run.calls == []


def test_a_failing_ruff_stages_nothing(tmp_path):
    run = Runner(code=2)
    assert "exited 2" in tidy(PATCH, repo(tmp_path), run=run)
    assert all(call[0] != "git" for call in run.calls)


def test_a_missing_ruff_is_reported_not_raised(tmp_path):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    assert "did not run" in tidy(PATCH, repo(tmp_path), run=missing)


def test_a_symlinked_python_file_is_not_formatted(tmp_path):
    root = repo(tmp_path / "repo")
    outside = tmp_path / "outside.py"
    outside.write_text("x=1\n")
    (root / "src" / "app.py").unlink()
    (root / "src" / "app.py").symlink_to(outside)
    assert python_files(PATCH, root) == []
