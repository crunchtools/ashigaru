"""Run the repository's formatter over the Python a patch touched, before Ashigaru commits.

python-sdlc runs `ruff check --fix` and `ruff format` only in its commit tier, a local hook,
and CI runs the push tier, which has neither. Ashigaru commits on a runner where no hook
runs, so its patches reached main unformatted (crunchtools/constitution#130) and nothing
said so. This is that commit tier, for the files the patch named.

ruff reads its own configuration and nothing else: no plugin, no dependency's code, so
running it here is not the execution `relock` has to guard against. It is pinned to the
version the python-sdlc image bakes, so this and a developer's hook format alike.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from relock import changed_files, environment

RUFF_VERSION = "0.17.1"
"""python-sdlc's Containerfile RUFF_VERSION. Move it when that moves."""
RUFF = ("uvx", "--from", f"ruff=={RUFF_VERSION}", "ruff")
STEPS = (("check", "--fix", "--exit-zero", "--quiet"), ("format", "--quiet"))
TIMEOUT = 300


def configured(root: Path) -> bool:
    """True when the repository configures ruff, so formatting it is its own rule."""
    if (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        return True
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        return False
    try:
        return "ruff" in tomllib.loads(manifest.read_text()).get("tool", {})
    except tomllib.TOMLDecodeError:
        return False


def python_files(patch: str, root: Path) -> list[str]:
    """The `.py` files the patch names that still exist after it."""
    return sorted(path for path in changed_files(patch) if path.endswith(".py") and (root / path).is_file())


def tidy(patch: str, root: Path, run=subprocess.run) -> str:
    """Format and stage the patch's Python files; returns what was done, for the log.

    `run` is a seam for tests with `subprocess.run`'s signature. A missing or failing ruff
    is reported and left to the pull request's own checks; it never stops the publish.
    """
    files = python_files(patch, root)
    if not files or not configured(root):
        return "tidy: nothing to format"
    for step in STEPS:
        try:
            done = run([*RUFF, *step, "--", *files], cwd=root, capture_output=True, text=True,
                       timeout=TIMEOUT, check=False, env=environment())  # fmt: skip
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            return f"tidy: ruff {step[0]} did not run ({type(error).__name__}); the patch is unformatted"
        if done.returncode != 0:
            return f"tidy: ruff {step[0]} exited {done.returncode}: {(done.stderr or '').strip()[-500:]}"
    run(["git", "add", "--", *files], cwd=root, check=True, capture_output=True)
    return f"tidy: formatted {len(files)} file(s)"
