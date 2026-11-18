"""Normalized CI: one result per (repo, commit), and `desk_wait` on it.

Forge adapters write checks (forges/); this module reads them back as one
CIResult and wakes whoever waits on that commit. A waiter holds no thread: it
awaits an event the webhook sets once its delivery commits, and rereads SQLite
every RECHECK seconds in case another process took the delivery.
"""

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Iterable

from starlette.concurrency import run_in_threadpool

from . import store
from .forges import CIUpdate
from .model import MAX_WAIT, Check, CIResult, CIStatus, Verdict

RECHECK = 15.0
FAILED = frozenset({Verdict.FAILURE, Verdict.CANCELLED})

Key = tuple[str, str]


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
    """Who waits on which commit. Lives on the event loop; notify() only from it."""

    def __init__(self) -> None:
        self._waiting: dict[Key, set[asyncio.Event]] = {}

    def notify(self, keys: Iterable[Key]) -> None:
        for commit in set(keys):
            for event in self._waiting.get(commit, ()):
                event.set()

    async def wait(self, conn: sqlite3.Connection, repo: str, sha: str, timeout: float) -> CIResult:
        """The result as soon as it is no longer pending, or as it stands after `timeout` seconds."""
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
                if current.status != CIStatus.PENDING or left <= 0:
                    return current
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), min(left, RECHECK))
        finally:
            waiting = self._waiting.get(commit, set())
            waiting.discard(event)
            if not waiting:
                self._waiting.pop(commit, None)
