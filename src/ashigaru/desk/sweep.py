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
   writer's PR whose title or body names an open item in its repo, `(a676)`,
   becomes that item's `pr`. The mark moves to this run's start once the search
   was read whole, or to the last hit's time when the forge's cap cut it short,
   but never past a hit it could not judge, which the next run reads again;
3. settles every item against the subjects (subjects.hygiene).

It never writes to the forge, and never re-judges an item it already has:
trust is intake's, from signed deliveries. One run at a time: a run holds the
`sweep-lock` mark as a LEASE it renews, so a run killed mid-way frees it within
a lease; a run that finds a live lease writes a skipped row saying whose, and the
timer tries again once that lease could lapse. A subject it cannot read, or
whose author's write access the forge cannot say, is reported and skipped; the run's row
always finishes, with its counts, outsiders and first error, and the board
shows it. The forge calls run outside any transaction, so no write lock waits
on the network.
"""

import asyncio
import contextlib
import dataclasses
import http.client
import itertools
import json
import logging
import re
import sqlite3
import threading
import urllib.error
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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
OUTSIDERS_SHOWN = 50
"""The most outsiders NEEDS YOU lists; a run keeps at most this many too."""
FETCHERS = 8
"""Subjects read from the forge at once."""
READ_BATCH = 4 * FETCHERS
"""Subjects queued for reading at once."""
KEPT = timedelta(days=30)
"""How long a run's row is kept."""
LOCK = "sweep-lock"
"""Held by the run under way as a lease, `<heartbeat> <run id>`: the run renews it every quarter
lease, so one killed mid-run (podman stops with SIGKILL after 10 s) frees it within a lease."""
LEASE = 60.0
"""Seconds a lease lasts unrenewed."""
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
SKIP = "INSERT INTO sweeps (trigger, started_at, finished_at, skipped) VALUES (?, ?, ?, ?)"
RENEW = "UPDATE marks SET value = ?, updated_at = ? WHERE name = ? AND value LIKE ?"
RELEASE = "DELETE FROM marks WHERE name = ? AND value LIKE ?"
FINISH = """
    UPDATE sweeps SET finished_at = ?, subjects = ?, new = ?, closed = ?, error = ?, outsiders = ?
    WHERE id = ?
"""
UNREADABLE = (OSError, ValueError, http.client.HTTPException)
"""What a forge call raises when it cannot say: the network, a bad answer, a body cut short."""
LAST = "SELECT * FROM sweeps ORDER BY id DESC LIMIT 1"
RUN = "SELECT * FROM sweeps WHERE id = ?"
KNOWN = "SELECT subject_key FROM items WHERE subject_key IN (SELECT value FROM json_each(?))"


class Reader(Protocol):
    """What a sweep asks of a forge adapter (forges.Forge has all three)."""

    def subject(self, repo: str, number: int, token: str) -> IntakeItem: ...

    def updated(self, org: str, since: str, token: str) -> Search: ...

    def may_write(self, repo: str, account: str, token: str) -> bool: ...


@dataclasses.dataclass(frozen=True)
class Settings:
    """What a sweep reads the forge with: the adapter, its token, the org it searches, and the
    accounts whose own subjects are trusted as a writer's."""

    forge: Reader
    token: str
    org: str
    authors: frozenset[str] = frozenset()
    every: float = EVERY
    lease: float = LEASE


@dataclasses.dataclass
class Counts:
    subjects: int = 0
    new: int = 0
    closed: int = 0
    outsiders: list[dict[str, str | None]] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    writers: dict[tuple[str, str], bool] = dataclasses.field(default_factory=dict)
    """The forge's answer for each (repo, author) asked this run."""
    unasked: dict[tuple[str, str], str] = dataclasses.field(default_factory=dict)
    """Why the forge could not answer for a (repo, author): its hits wait for the next run."""

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
    LIMIT :limit
"""
"""The outsiders the runs of the last day listed that still have no item: Scott's to look at."""


def outsiders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    since = _stamp(datetime.now(UTC) - FIRST)
    return [dict(row) for row in conn.execute(OUTSIDERS, {"since": since, "limit": OUTSIDERS_SHOWN})]


def _decoded(row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "outsiders": json.loads(row["outsiders"])}


def last(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The newest run, finished or not."""
    row = conn.execute(LAST).fetchone()
    return _decoded(row) if row else None


def _run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    return _decoded(conn.execute(RUN, (run_id,)).fetchone())


def _number(key: str) -> tuple[str, int] | None:
    """(owner/repo, N) for a forge subject key; None for anything else (RT#N, repo:alert)."""
    repo, _, number = key.partition("#")
    return (repo, int(number)) if "/" in repo and number.isdigit() else None


class Unanswered(ValueError):
    """The forge could not say whether a hit's author may write: the hit waits for the next run."""


def _why(error: Exception) -> str:
    """What stopped a read, in a few words: the HTTP status, the unanswered question, or the error's type."""
    match error:
        case urllib.error.HTTPError():
            return f"HTTP {error.code}"
        case Unanswered():
            return str(error)
        case _:
            return type(error).__name__


def _found(conn: sqlite3.Connection, settings: Settings, hit: Found, counts: Counts) -> None:
    """A search hit. A member's or trusted author's new subject is queued: claimable only when the
    forge says its author may write to the repo (org membership is not write access), else held;
    anyone else's is listed, never stored. A writer's PR becomes the `pr` of each open item its title
    or body names that has none. A subject with an item is recorded and settled."""
    item = hit.item
    member = hit.association in WRITERS or item.author in settings.authors
    known = store.item_by_subject(conn, item.subject_key) is not None
    if not member and not known:
        outsider = {"subject_key": item.subject_key, "ref": item.ref, "author": item.author}
        if outsider not in counts.outsiders and len(counts.outsiders) < OUTSIDERS_SHOWN:
            counts.outsiders.append(outsider)
        return
    named = LINK.findall(f"{item.title}\n{hit.body}") if member and item.kind == Kind.STORY else []
    asked = (item.repo or "", item.author or "")
    if asked in counts.unasked:
        raise Unanswered(f"may {item.author} write? {counts.unasked[asked]}")
    writes = item.author in settings.authors or (member and counts.writers.get(asked, False))
    with store.transaction(conn):
        if store.item_by_subject(conn, item.subject_key) is None:
            judged = dataclasses.replace(item, author_writes=writes, judged_at=0)
            counts.new += intake.upsert(conn, judged) is not None
        for work in named if writes else []:
            found = store.get_item(conn, work)
            if (
                found
                and (found["repo"] or "").lower() == (item.repo or "").lower()
                and not found["pr"]
                and found["state"] not in CLOSED
                and found["wrap"] != Wrap.CLEARED
            ):
                store.update_item(conn, work, {"pr": item.subject_key}, ACTOR, SOURCE)
        if store.item_by_subject(conn, item.subject_key) is not None or (named and writes):
            subjects.record(conn, item, SOURCE)
            counts.closed += len(subjects.settle(conn, item.subject_key))
        counts.subjects += 1


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _beat(when: datetime) -> str:
    """A lease's time, to the microsecond: a lease is shorter than a run."""
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _since(conn: sqlite3.Connection, started: datetime) -> str:
    mark = store.get_mark(conn, MARK)
    since = datetime.fromisoformat(mark["value"]) - OVERLAP if mark else started - FIRST
    return _stamp(since)


def _lease(conn: sqlite3.Connection, settings: Settings, trigger: str, started: datetime) -> tuple[int, bool]:
    """Start a run's row and take the lease for it: (its id, True). When a live lease is held, the
    row is a skipped one saying by which run: (its id, False)."""
    stamp = _stamp(started)
    with store.transaction(conn):
        held = store.get_mark(conn, LOCK)
        beat, _, holder = held["value"].partition(" ") if held else ("", "", "")
        if beat > _beat(started - timedelta(seconds=settings.lease)):
            why = f"run {holder or '?'} holds the lease, renewed {beat}"
            return int(conn.execute(SKIP, (trigger, stamp, stamp, why)).lastrowid or 0), False
        run_id = int(conn.execute(START, (trigger, stamp)).lastrowid or 0)
        store.set_mark(conn, LOCK, f"{_beat(started)} {run_id}", ACTOR)
    return run_id, True


@contextlib.contextmanager
def _renewing(db_path: str, settings: Settings, run_id: int) -> Iterator[None]:
    """Renew run_id's lease every quarter lease, on its own connection, until the run ends."""
    done = threading.Event()

    def renew() -> None:
        conn = store.connect(db_path)
        try:
            while not done.wait(settings.lease / 4):
                beat = f"{_beat(datetime.now(UTC))} {run_id}"
                conn.execute(RENEW, (beat, store.now(), LOCK, f"% {run_id}"))
        finally:
            conn.close()

    renewer = threading.Thread(target=renew, name=f"sweep-lease-{run_id}", daemon=True)
    renewer.start()
    try:
        yield
    finally:
        done.set()
        renewer.join()


def _read(conn: sqlite3.Connection, settings: Settings, keys: set[str], counts: Counts) -> None:
    """Each open item's subject, read back FETCHERS at a time in batches of READ_BATCH, so the futures
    queued stay bounded however many items are open, then recorded and settled in key order; one it
    cannot read is an error."""
    wanted = [(key, found) for key in sorted(keys) if (found := _number(key))]
    with ThreadPoolExecutor(FETCHERS) as pool:
        for batch in itertools.batched(wanted, READ_BATCH):
            reads = {key: pool.submit(settings.forge.subject, *found, settings.token) for key, found in batch}
            for key, read in reads.items():
                try:
                    item = read.result()
                except UNREADABLE as failed:
                    counts.errors.append(f"{key}: {_why(failed)}")
                    LOG.warning("sweep could not read %s: %s", key, counts.errors[-1], exc_info=failed)
                    continue
                with store.transaction(conn):
                    if subjects.record(conn, item, SOURCE):
                        counts.closed += len(subjects.settle(conn, item.subject_key))
                    counts.subjects += 1


def _ask(conn: sqlite3.Connection, settings: Settings, hits: list[Found], counts: Counts) -> None:
    """Ask the forge, FETCHERS at a time, whether each member who authored a hit that needs a
    decision (a new subject, or a PR naming an item) may write to its repo, once per (repo, author),
    so `_found` reads the answers from `counts.writers`. A failed check is a no, as the forge's own
    permission check makes it. Org members are few, so this is a handful of calls."""
    asked = json.dumps(sorted({hit.item.subject_key for hit in hits}))
    known = {row[0] for row in conn.execute(KNOWN, (asked,))}
    pairs = {
        (hit.item.repo, hit.item.author)
        for hit in hits
        if hit.association in WRITERS
        and hit.item.repo
        and hit.item.author
        and hit.item.author not in settings.authors
        and (LINK.search(f"{hit.item.title}\n{hit.body}") or hit.item.subject_key not in known)
    }
    with ThreadPoolExecutor(FETCHERS) as pool:
        answers = {
            pair: pool.submit(settings.forge.may_write, *pair, settings.token) for pair in sorted(pairs)
        }
    for pair, answer in answers.items():
        try:
            counts.writers[pair] = answer.result()
        except UNREADABLE as failed:
            counts.unasked[pair] = _why(failed)


def _search(
    conn: sqlite3.Connection, settings: Settings, keys: set[str], started: datetime, counts: Counts
) -> None:
    try:
        search = settings.forge.updated(settings.org, _since(conn, started), settings.token)
    except UNREADABLE as failed:
        counts.errors.insert(0, f"search: {_why(failed)}")
        LOG.warning("sweep search stopped: %s", counts.errors[0], exc_info=failed)
        return
    _ask(conn, settings, [hit for hit in search.hits if hit.item.subject_key not in keys], counts)
    deferred: list[str] = []
    for hit in search.hits:
        try:
            if hit.item.subject_key not in keys:
                _found(conn, settings, hit, counts)
        except UNREADABLE as failed:
            counts.errors.append(f"{hit.item.subject_key}: {_why(failed)}")
            deferred.append(hit.item.updated_at or "")
    reached = _stamp(started) if search.complete or not search.hits else search.hits[-1].item.updated_at
    if not search.complete:
        counts.errors.insert(0, "search: cut at the forge's cap; the next run reads on")
    held = min([reached or _stamp(started), *deferred])
    if held:  # a hit with no time cannot be read from again: the mark stays
        store.set_mark(conn, MARK, held, ACTOR)


def run(db_path: str, settings: Settings, trigger: str) -> dict[str, Any]:
    """One sweep, start to finish, on its own connection, holding the lease. Returns its `sweeps`
    row; a skipped one, saying why, when another run's lease is live."""
    conn = store.connect(db_path)
    started = datetime.now(UTC)
    try:
        run_id, holding = _lease(conn, settings, trigger, started)
        if not holding:
            return _run(conn, run_id)
        counts = Counts()
        try:
            with _renewing(db_path, settings, run_id):
                keys = {row[0] for row in conn.execute(OPEN_SUBJECTS)}
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
            conn.execute(RELEASE, (LOCK, f"% {run_id}"))
            conn.execute("DELETE FROM sweeps WHERE started_at < ?", (_stamp(started - KEPT),))
        return _run(conn, run_id)
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
            wait = self.settings.every
            try:
                done = await run_in_threadpool(run, self.db_path, self.settings, trigger)
                if done.get("skipped"):  # a dead run's lease: try again once it lapses
                    wait = min(wait, self.settings.lease)
            except Exception:  # the timer outlives any one run
                LOG.exception("sweep failed")
            finally:
                self.running = False
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
                trigger = "request"
            except TimeoutError:
                trigger = "timer"
            self._wake.clear()
