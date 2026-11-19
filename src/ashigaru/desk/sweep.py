"""The sweep: the Desk reads the forge back on a timer, in case a webhook was missed.

Webhooks are the fast path; this is the fallback. Every EVERY seconds, at
startup, and as soon as an admin asks (POST /v1/sweep), one run:

1. reads each open item's subject and PR from the forge and records it, as a
   delivery would, settling what it moves (subjects.py); one it cannot read is
   reported and skipped;
2. searches the org for subjects updated since the `sweep-search` mark, less
   OVERLAP for the search index's lag. A subject with no item is queued, as a
   writer's, only when the search's author_association says OWNER or MEMBER or
   its author is trusted (ASHIGARU_FORGE_AUTHORS, the app's bot). Anyone
   else's is never stored: it is listed for Scott in the run's `outsiders`. A
   writer's PR whose title or body names an open item, `(a676)`, becomes that
   item's `pr`. The mark moves to this run's start once the search was read
   whole, or to the last hit's time when the forge's cap cut it short;
3. settles every item against the subjects (subjects.hygiene).

It never writes to the forge, and never re-judges an item it already has:
trust is intake's, from signed deliveries. One run at a time: a run takes the
`sweep-lock` mark, and one older than twice the interval was abandoned. A
subject it cannot read, for any reason, is reported and skipped; the run's row
always finishes, with its counts, outsiders and first error, and the board
shows it. The forge calls run outside any transaction, so no write lock waits
on the network.
"""

import asyncio
import dataclasses
import http.client
import json
import logging
import re
import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from starlette.concurrency import run_in_threadpool

from . import intake, store, subjects
from .forges import Found, IntakeItem, Search
from .model import CLOSED, Kind, Wrap

EVERY = 600.0
"""Seconds between runs; ASHIGARU_SWEEP_EVERY."""
MARK = "sweep-search"
"""The forge time the last finished run's search started from, so the next one reads on."""
FIRST = timedelta(days=1)
"""How far back the very first search reaches."""
OVERLAP = timedelta(minutes=10)
"""How far each search reaches back past the mark: the search index lags the forge."""
LOCK = "sweep-lock"
"""Held by the run under way, as its start time; abandoned after twice the interval."""
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
FINISH = """
    UPDATE sweeps SET finished_at = ?, subjects = ?, new = ?, closed = ?, error = ?, outsiders = ?
    WHERE id = ?
"""
UNREADABLE = (OSError, ValueError, http.client.HTTPException)
"""What a forge call raises when it cannot say: the network, a bad answer, a body cut short."""
LAST = "SELECT * FROM sweeps ORDER BY id DESC LIMIT 1"


class Reader(Protocol):
    """What a sweep asks of a forge adapter (forges.Forge has both)."""

    def subject(self, repo: str, number: int, token: str) -> IntakeItem: ...

    def updated(self, org: str, since: str, token: str) -> Search: ...


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
    outsiders: list[dict[str, str | None]] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    def error(self) -> str | None:
        more = f" (and {len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        return f"{self.errors[0]}{more}" if self.errors else None


OUTSIDERS = """
    SELECT DISTINCT json_extract(o.value, '$.subject_key') AS subject_key,
           json_extract(o.value, '$.ref') AS ref, json_extract(o.value, '$.author') AS author
    FROM sweeps s, json_each(s.outsiders) o
    WHERE s.finished_at >= :since AND json_extract(o.value, '$.subject_key') NOT IN (
        SELECT subject_key FROM items WHERE subject_key IS NOT NULL
    )
"""
"""The outsiders the runs of the last day listed that still have no item: Scott's to look at."""


def outsiders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    since = _stamp(datetime.now(UTC) - FIRST)
    return [dict(row) for row in conn.execute(OUTSIDERS, {"since": since})]


def last(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The newest run, finished or not."""
    row = conn.execute(LAST).fetchone()
    return {**dict(row), "outsiders": json.loads(row["outsiders"])} if row else None


def _number(key: str) -> tuple[str, int] | None:
    """(owner/repo, N) for a forge subject key; None for anything else (RT#N, repo:alert)."""
    repo, _, number = key.partition("#")
    return (repo, int(number)) if "/" in repo and number.isdigit() else None


def _why(error: Exception) -> str:
    return f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__


def _found(conn: sqlite3.Connection, settings: Settings, hit: Found, counts: Counts) -> None:
    """A search hit. A writer's new subject is queued, and a writer's PR becomes the `pr` of each open
    item its title or body names that has none; anyone else's is listed, never stored. A subject
    with an item is recorded and settled."""
    item = hit.item
    writes = hit.association in WRITERS or item.author in settings.authors
    if not writes and store.item_by_subject(conn, item.subject_key) is None:
        outsider = {"subject_key": item.subject_key, "ref": item.ref, "author": item.author}
        counts.outsiders += [outsider] if outsider not in counts.outsiders else []
        return
    named = LINK.findall(f"{item.title}\n{hit.body}") if writes and item.kind == Kind.STORY else []
    with store.transaction(conn):
        if store.item_by_subject(conn, item.subject_key) is None:
            counts.new += (
                intake.upsert(conn, dataclasses.replace(item, author_writes=True, judged_at=0)) is not None
            )
        for work in named:
            found = store.get_item(conn, work)
            if found and not found["pr"] and found["state"] not in CLOSED and found["wrap"] != Wrap.CLEARED:
                store.update_item(conn, work, {"pr": item.subject_key}, ACTOR, SOURCE)
        if store.item_by_subject(conn, item.subject_key) is not None or named:
            subjects.record(conn, item, SOURCE)
            counts.closed += len(subjects.settle(conn, item.subject_key))
        counts.subjects += 1


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _since(conn: sqlite3.Connection, started: datetime) -> str:
    mark = store.get_mark(conn, MARK)
    since = datetime.fromisoformat(mark["value"]) - OVERLAP if mark else started - FIRST
    return _stamp(since)


def _locked(conn: sqlite3.Connection, settings: Settings, started: datetime) -> bool:
    """Take the lock unless a run that is not abandoned holds it."""
    with store.transaction(conn):
        held = store.get_mark(conn, LOCK)
        alive = _stamp(started - timedelta(seconds=2 * settings.every))
        if held and held["value"] > alive:
            return True
        store.set_mark(conn, LOCK, _stamp(started), ACTOR)
    return False


def _read(conn: sqlite3.Connection, settings: Settings, keys: list[str], counts: Counts) -> None:
    """Each open item's subject, read back: recorded and settled; one it cannot read is an error."""
    for key in keys:
        try:
            if not (found := _number(key)):
                continue
            item = settings.forge.subject(*found, settings.token)
            with store.transaction(conn):
                if subjects.record(conn, item, SOURCE):
                    counts.closed += len(subjects.settle(conn, item.subject_key))
                counts.subjects += 1
        except UNREADABLE as failed:
            counts.errors.append(f"{key}: {_why(failed)}")
            LOG.warning("sweep could not read %s: %s", key, counts.errors[-1], exc_info=failed)


def _search(
    conn: sqlite3.Connection, settings: Settings, keys: list[str], started: datetime, counts: Counts
) -> None:
    try:
        search = settings.forge.updated(settings.org, _since(conn, started), settings.token)
    except UNREADABLE as failed:
        counts.errors.insert(0, f"search: {_why(failed)}")
        LOG.warning("sweep search stopped: %s", counts.errors[0], exc_info=failed)
        return
    for hit in search.hits:
        try:
            if hit.item.subject_key not in keys:
                _found(conn, settings, hit, counts)
        except UNREADABLE as failed:
            counts.errors.append(f"{hit.item.subject_key}: {_why(failed)}")
    reached = _stamp(started) if search.complete or not search.hits else search.hits[-1].item.updated_at
    if not search.complete:
        counts.errors.insert(0, "search: cut at the forge's cap; the next run reads on")
    store.set_mark(conn, MARK, reached or _stamp(started), ACTOR)


def run(db_path: str, settings: Settings, trigger: str) -> dict[str, Any] | None:
    """One sweep, start to finish, on its own connection. Returns its `sweeps` row, or None when
    another run holds the lock."""
    conn = store.connect(db_path)
    started = datetime.now(UTC)
    try:
        if _locked(conn, settings, started):
            return None
        run_id = conn.execute(START, (trigger, _stamp(started))).lastrowid
        counts = Counts()
        try:
            keys = [row[0] for row in conn.execute(OPEN_SUBJECTS)]
            _read(conn, settings, keys, counts)
            _search(conn, settings, keys, started, counts)
            counts.closed += len(subjects.hygiene(conn))
        except Exception as failed:  # the row still finishes, saying why it stopped
            counts.errors.insert(0, f"stopped: {_why(failed)}")
            raise
        finally:
            outsiders = json.dumps(counts.outsiders)
            finish = (
                store.now(),
                counts.subjects,
                counts.new,
                counts.closed,
                counts.error(),
                outsiders,
                run_id,
            )
            conn.execute(FINISH, finish)
            conn.execute("DELETE FROM marks WHERE name = ?", (LOCK,))
        return last(conn)
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
