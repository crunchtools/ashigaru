"""Normalized CI: one result per (repo, commit), and `desk_wait` on it.

Forge adapters write checks (forges/); this module reads them back as one
CIResult and wakes whoever waits on that commit. A waiter holds no thread: it
awaits an event the webhook sets once its delivery commits, and rereads SQLite
every RECHECK seconds in case another process took the delivery.

A commit's own checks are workflows; a branch requires jobs ("sdlc ci"), and a
workflow the forge never started sends no delivery at all. So a result that would
be success is first held against what the target branch requires, read from the
forge: until every required check has a conclusion it stays pending, and when
those cannot be read it is never success.
"""

import asyncio
import contextlib
import logging
import sqlite3
import time
import urllib.error
from collections.abc import Callable, Iterable

from starlette.concurrency import run_in_threadpool

from . import store
from .forges import CIUpdate, Forge
from .model import MAX_WAIT, Check, CIResult, CIStatus, Verdict

RECHECK = 15.0
FAILED = frozenset({Verdict.FAILURE, Verdict.CANCELLED})

Key = tuple[str, str]
Required = Callable[[str, str, str | None], tuple[str, dict[str, Verdict]]]
"""(repo, sha, branch or None for the default) -> (the branch, the verdict of each check it requires,
pending for one that has not reported). Raises OSError or ValueError when the forge cannot say."""
LOG = logging.getLogger(__name__)


def key(repo: str, sha: str) -> Key:
    """The (repo, sha) a result is stored under: owner/name and the full commit id, lower case."""
    return repo.strip().lower(), sha.strip().lower()


def status_of(verdicts: Iterable[Verdict]) -> CIStatus:
    seen = list(verdicts)
    if not seen or Verdict.PENDING in seen:
        return CIStatus.PENDING
    return CIStatus.FAILURE if FAILED & set(seen) else CIStatus.SUCCESS


def result(conn: sqlite3.Connection, repo: str, sha: str) -> CIResult:
    repo, sha = key(repo, sha)
    checks = [Check.model_validate(row) for row in store.ci_checks(conn, repo, sha)]
    return CIResult(
        repo=repo,
        sha=sha,
        status=status_of(check.verdict for check in checks),
        checks=checks,
        failing=[check.name for check in checks if check.verdict in FAILED],
        pending=[check.name for check in checks if check.verdict == Verdict.PENDING],
    )


def from_forge(forge: Forge, token: str) -> Required:
    """The forge's required-check lookup, with its API token."""

    def required(repo: str, sha: str, branch: str | None) -> tuple[str, dict[str, Verdict]]:
        return forge.required(repo, sha, branch, token)

    return required


def gate(current: CIResult, required: Required, branch: str | None) -> CIResult:
    """`current` held against the checks `branch` requires: success only when each has a conclusion,
    failure as soon as one failed. A required check that has not reported joins `pending`."""
    try:
        branch, verdicts = required(current.repo, current.sha, branch)
    except (OSError, ValueError) as error:
        why = f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        LOG.warning("required checks of %s@%s unread: %s", current.repo, current.sha[:12], why)
        held = CIStatus.PENDING if current.status == CIStatus.SUCCESS else current.status
        unverified = f"the required checks could not be read ({why})"
        return current.model_copy(update={"status": held, "unverified": unverified})
    waiting = sorted(name for name, verdict in verdicts.items() if verdict == Verdict.PENDING)
    failed = sorted(name for name, verdict in verdicts.items() if verdict in FAILED)
    status: CIStatus = current.status
    if failed:
        status = CIStatus.FAILURE
    elif waiting and status == CIStatus.SUCCESS:
        status = CIStatus.PENDING
    return current.model_copy(
        update={
            "status": status,
            "branch": branch,
            "required": sorted(verdicts),
            "pending": current.pending + [name for name in waiting if name not in current.pending],
            "failing": current.failing + [name for name in failed if name not in current.failing],
        }
    )


def record(conn: sqlite3.Connection, update: CIUpdate, source: str) -> Key:
    """Store one check; the caller holds the transaction. Returns the commit to wake waiters on."""
    repo, sha = key(update.repo, update.sha)
    store.write_check(
        conn,
        {
            "repo": repo,
            "sha": sha,
            "key": update.key,
            "name": update.name,
            "verdict": update.verdict,
            "run": update.run,
            "url": update.url,
            "source": source,
            "updated_at": update.updated_at,
        },
    )
    return repo, sha


class Waiters:
    """Who waits on which commit, and how to read what a branch requires. Lives on the event loop;
    notify() only from it."""

    def __init__(self, required: Required) -> None:
        self.required = required
        self._waiting: dict[Key, set[asyncio.Event]] = {}

    def notify(self, keys: Iterable[Key]) -> None:
        for commit in set(keys):
            for event in self._waiting.get(commit, ()):
                event.set()

    async def wait(
        self, conn: sqlite3.Connection, repo: str, sha: str, timeout: float, branch: str | None = None
    ) -> CIResult:
        """The result as soon as it is no longer pending, or as it stands after `timeout` seconds. The
        forge is asked what `branch` requires only once the commit's own checks pass, or at the end."""
        commit = key(repo, sha)
        deadline = time.monotonic() + min(timeout, MAX_WAIT)
        event = asyncio.Event()
        self._waiting.setdefault(commit, set()).add(event)
        try:
            while True:
                # Registered before the read, so a delivery that commits after it still wakes us.
                event.clear()
                current = await run_in_threadpool(result, conn, *commit)
                left = deadline - time.monotonic()
                if current.status == CIStatus.SUCCESS or left <= 0 and current.status == CIStatus.PENDING:
                    current = await run_in_threadpool(gate, current, self.required, branch)
                    left = deadline - time.monotonic()
                if current.status != CIStatus.PENDING or left <= 0:
                    return current
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), min(left, RECHECK))
        finally:
            waiting = self._waiting.get(commit, set())
            waiting.discard(event)
            if not waiting:
                self._waiting.pop(commit, None)
