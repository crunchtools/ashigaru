"""Regenerate a lockfile after the agent's patch changed its manifest.

The code agent has no shell, so it can edit `pyproject.toml` but not run `uv lock`. The
publish job holds the write credential and runs no model; it relocks here, between applying
the patch and committing. Only the lock command runs: never `uv sync`, tests or a build.

NOTHING FROM A DEPENDENCY MAY EXECUTE HERE. Resolution is not inert: `uv lock` builds the
metadata of any requirement it cannot read statically -- a source distribution, or a path or
VCS requirement with `dynamic` fields -- by calling that package's PEP 517 backend, which
runs its `setup.py`. The patch being resolved is a model's work on text a stranger may have
influenced, and this job holds the app token in its environment and the checkout credential
in `.git/config`. That is the inversion the authority model exists to prevent, and it would
be reached without anyone choosing it, unlike the `Bash` the agent is refused.

So `--no-build`, which makes uv refuse such a requirement instead of building it, and a
scrubbed environment. Measured, not assumed: with the flag off, a path dependency declaring
`dynamic = ["version"]` ran its `setup.py` during `uv lock`; with it on, uv exited with
"can't be installed because it is marked as `--no-build` but has no binary distribution" and
the backend did not run. The cost is that a dependency with no wheel cannot be relocked
without a person, which is the right way round.

`relock` returns a refusal, or None when the lockfile is fresh. Anything but the lockfile
changing, a failed resolution or a missing `uv` is a refusal, and the caller hands over.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from common import DIFF_HEADER

# One entry per ecosystem: manifest, lockfile, and the lock command. Never a sync, never a
# build -- see the module docstring for why `--no-build` is load-bearing and not a tidy-up.
ECOSYSTEMS = (("pyproject.toml", "uv.lock", ("uv", "lock", "--no-build")),)
TIMEOUT = 600
TAIL = 2000
KEEP_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CACHE_HOME", "SSL_CERT_FILE")
"""What the lock command inherits. Everything else, the app token above all, is dropped:
`--no-build` means nothing of the dependency's should run, and this is what that is worth if
one day something does."""


def environment() -> dict[str, str]:
    """The lock command's environment: what `KEEP_ENV` names, and a marker uv reads itself."""
    kept = {name: os.environ[name] for name in KEEP_ENV if name in os.environ}
    # Belt and braces: uv honours this even if the flag is ever dropped from the command.
    return kept | {"UV_NO_BUILD": "1"}


def changed_files(patch: str) -> set[str]:
    """Every path a git diff names, either side of a rename."""
    return {path for match in DIFF_HEADER.finditer(patch) for path in match.groups()}


def wanted(patch: str, root: Path) -> tuple[str, tuple[str, ...]] | None:
    """The lockfile and lock command to run, or None when the patch needs no relock.

    Only a changed root manifest triggers one, and only when the repository uses its lockfile:
    the lockfile is present, or the patch names it (so a patch that deletes it alongside a
    manifest change gets it regenerated, not dropped).
    """
    changed = changed_files(patch)
    for manifest, lockfile, command in ECOSYSTEMS:
        if manifest in changed and ((root / lockfile).is_file() or lockfile in changed):
            return lockfile, command
    return None


def stray_changes(root: Path) -> set[str]:
    """Paths modified or created in the worktree but not staged: what the lock command did.

    The patch was applied with `--index`, so its changes are staged and are not listed here.
    """

    def lines(*args: str) -> set[str]:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
        return {line for line in done.stdout.splitlines() if line}

    return lines("diff", "--name-only") | lines("ls-files", "--others", "--exclude-standard")


def stage_lockfile(root: Path, lockfile: str) -> None:
    subprocess.run(["git", "add", "--", lockfile], cwd=root, check=True, capture_output=True)


def relock(
    patch: str,
    root: Path,
    run=subprocess.run,
    strays=stray_changes,
    stage=stage_lockfile,
) -> str | None:
    """Regenerate the lockfile in `root` when the patch changed its manifest.

    `patch` is the already-applied diff and `root` the worktree it was applied to with
    `--index`. The other three are seams for tests; production uses the defaults:
    `run(command, cwd=, capture_output=, text=, timeout=, check=)` runs the lock command like
    `subprocess.run` and returns a `CompletedProcess`, raising `FileNotFoundError` when the
    tool is missing and `TimeoutExpired` on timeout; `strays(root)` returns the unstaged paths
    the lock command modified or created; `stage(root, lockfile)` adds the fresh lockfile to
    the index, and is called only when the lockfile is the sole stray.

    Returns None when nothing was needed or the lockfile is fresh and staged, or the reason
    the run must hand over instead of pushing.
    """
    target = wanted(patch, root)
    if target is None:
        return None
    lockfile, command = target
    try:
        done = run(command, cwd=root, capture_output=True, text=True, timeout=TIMEOUT,
                   check=False, env=environment())  # fmt: skip
    except FileNotFoundError:
        return f"`{command[0]}` is not available on the publish runner, so `{lockfile}` is stale."
    except subprocess.TimeoutExpired:
        return f"`{' '.join(command)}` timed out, so `{lockfile}` could not be regenerated."
    if done.returncode != 0:
        output = (done.stderr or done.stdout or "").strip()[-TAIL:]
        return f"`{' '.join(command)}` failed, so `{lockfile}` is stale.\n\n```\n{output}\n```"
    outside = sorted(strays(root) - {lockfile})
    if outside:
        listed = ", ".join(f"`{path}`" for path in outside)
        return f"`{' '.join(command)}` changed files other than `{lockfile}`: {listed}."
    stage(root, lockfile)
    return None
