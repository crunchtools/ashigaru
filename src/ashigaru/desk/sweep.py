"""The sweep: the Desk reads the forge back on a timer, in case a webhook was missed.

Webhooks are the fast path; this is the fallback. Every EVERY seconds, at
startup, and as soon as an admin asks (POST /v1/sweep), one run:

1. reads each open item's subject and PR from the forge and records it, as a
   delivery would, settling what it moves (subjects.py); one it cannot read is
   reported and skipped;
2. searches the org for subjects updated since the `sweep-search` mark. One
   with no item is queued by intake's rules, judged by the search's own
   author_association: an OWNER or MEMBER, or a trusted author
   (ASHIGARU_FORGE_AUTHORS, the app's bot), is a writer; another bot is never
   recorded; anyone else is held for Josui. A trusted author's PR whose title
   or body names an open item, `(a676)`, becomes that item's `pr`. The mark
   moves only when the whole search was read;
3. settles every item against the subjects (subjects.hygiene).

It never writes to the forge, and never re-judges an item it already has:
trust is intake's, from signed deliveries. A run's counts and its error, if
any, land in `sweeps`, which the board shows. The forge calls run outside any
transaction, so no write lock waits on the network.
"""

import asyncio
import dataclasses
import logging
import re
import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from starlette.concurrency import run_in_threadpool

from . import intake, store, subjects
from .forges import Found, IntakeItem
from .model import CLOSED, Kind, Wrap

EVERY = 600.0
"""Seconds between runs; ASHIGARU_SWEEP_EVERY."""
MARK = "sweep-search"
"""The forge time the last finished run's search started from, so the next one reads on."""
FIRST = timedelta(days=1)
"""How far back the very first search reaches."""
WRITERS = frozenset({"OWNER", "MEMBER"})
LINK = re.compile(r"\((a\d+)\)")
ACTOR = "desk"
SOURCE = "sweep"
LOG = logging.getLogger(__name__)

OPEN_SUBJECTS = f"""
    SELECT subject_key FROM items WHERE {store.OPEN} AND subject_key IS NOT NULL
    UNION SELECT pr FROM items WHERE {store.OPEN} AND pr IS NOT NULL
"""
START = "INSERT INTO sweeps (trigger, started_at) VALUES (?, ?)"
FINISH = "UPDATE sweeps SET finished_at = ?, subjects = ?, new = ?, closed = ?, error = ? WHERE id = ?"
LAST = "SELECT * FROM sweeps ORDER BY id DESC LIMIT 1"


class Reader(Protocol):
    """What a sweep asks of a forge adapter (forges.Forge has both)."""

    def subject(self, repo: str, number: int, token: str) -> IntakeItem: ...

    def updated(self, org: str, since: str, token: str) -> list[Found]: ...


@dataclasses.dataclass(frozen=True)
class Settings:
    """What a sweep reads the forge with: the adapter, its token, the org it searches, and the
    accounts whose own subjects are trusted as a writer's."""

    forge: Reader
    token: str
    org: str
    authors: frozenset[str] = frozenset()
    every: float = EVERY


@dataclasses.dataclass
class Counts:
    subjects: int = 0
    new: int = 0
    closed: int = 0


def last(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The newest run, finished or not."""
    row = conn.execute(LAST).fetchone()
    return dict(row) if row else None


def _number(key: str) -> tuple[str, int] | None:
    """(owner/repo, N) for a forge subject key; None for anything else (RT#N, repo:alert)."""
    repo, _, number = key.partition("#")
    return (repo, int(number)) if "/" in repo and number.isdigit() else None


def _why(error: Exception) -> str:
    return f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__


def _record(conn: sqlite3.Connection, item: IntakeItem, counts: Counts) -> None:
    with store.transaction(conn):
        if subjects.record(conn, item, SOURCE):
            counts.closed += len(subjects.settle(conn, item.subject_key))
        counts.subjects += 1


def _found(conn: sqlite3.Connection, settings: Settings, hit: Found, counts: Counts) -> None:
    """A search hit: queued by intake's rules if new; a writer's PR becomes the `pr` of each open item
    its title or body names that has none; then recorded and settled."""
    item = hit.item
    writes = hit.association in WRITERS or item.author in settings.authors
    named = LINK.findall(f"{item.title}\n{hit.body}") if writes and item.kind == Kind.STORY else []
    with store.transaction(conn):
        if store.item_by_subject(conn, item.subject_key) is None:
            judged = dataclasses.replace(item, author_writes=writes, judged_at=0)
            counts.new += intake.upsert(conn, judged) is not None
        for work in named:
            found = store.get_item(conn, work)
            if found and not found["pr"] and found["state"] not in CLOSED and found["wrap"] != Wrap.CLEARED:
                store.update_item(conn, work, {"pr": item.subject_key}, ACTOR, SOURCE)
        subjects.record(conn, item, SOURCE)
        counts.closed += len(subjects.settle(conn, item.subject_key))
        counts.subjects += 1


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _since(conn: sqlite3.Connection, started: datetime) -> str:
    mark = store.get_mark(conn, MARK)
    return mark["value"] if mark else _stamp(started - FIRST)


def run(db_path: str, settings: Settings, trigger: str) -> dict[str, Any]:
    """One sweep, start to finish, on its own connection. Returns its `sweeps` row."""
    conn = store.connect(db_path)
    try:
        started = datetime.now(UTC)
        run_id = conn.execute(START, (trigger, _stamp(started))).lastrowid
        counts, errors = Counts(), []
        keys = [row[0] for row in conn.execute(OPEN_SUBJECTS)]
        for key in keys:
            try:
                if found := _number(key):
                    _record(conn, settings.forge.subject(*found, settings.token), counts)
            except (OSError, ValueError) as failed:
                errors.append(f"{key}: {_why(failed)}")
                LOG.warning("sweep %s could not read %s: %s", run_id, key, errors[-1], exc_info=failed)
        try:
            for hit in settings.forge.updated(settings.org, _since(conn, started), settings.token):
                if hit.item.subject_key not in keys:
                    _found(conn, settings, hit, counts)
            store.set_mark(conn, MARK, _stamp(started), ACTOR)
        except (OSError, ValueError) as failed:
            errors.insert(0, f"search: {_why(failed)}")
            LOG.warning("sweep %s search stopped: %s", run_id, errors[0], exc_info=failed)
        error = (
            f"{errors[0]}" + (f" (and {len(errors) - 1} more)" if len(errors) > 1 else "") if errors else None
        )
        counts.closed += len(subjects.hygiene(conn))
        conn.execute(FINISH, (store.now(), counts.subjects, counts.new, counts.closed, error, run_id))
        return last(conn) or {}
    finally:
        conn.close()


class Sweeper:
    """The timer: a run at startup, then every `settings.every` seconds or as soon as `request()`
    wakes it. One run at a time, in a worker thread; lives on the event loop."""

    def __init__(self, db_path: str, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings
        self.running = False
        self._wake = asyncio.Event()

    def request(self) -> None:
        self._wake.set()

    async def loop(self) -> None:
        trigger = "startup"
        while True:
            self.running = True
            try:
                await run_in_threadpool(run, self.db_path, self.settings, trigger)
            except Exception:  # the timer outlives any one run
                LOG.exception("sweep failed")
            finally:
                self.running = False
            try:
                await asyncio.wait_for(self._wake.wait(), self.settings.every)
                trigger = "request"
            except TimeoutError:
                trigger = "timer"
            self._wake.clear()
