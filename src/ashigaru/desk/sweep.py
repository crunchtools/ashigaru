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
   else's is never stored: it is listed for Scott in the run's `outsiders`
   while it is open, and once a hit shows it closed the last day's runs forget it,
   except a dependency bot's PR (BOTS: Dependabot, Renovate), which is no one's
   to look at by default: its subject is recorded, read back every run while it
   is open, and once it has been open `bot_after` it is queued, auto on
   exedore-max, to land or close (a green one the bot or the repo's own
   automation merges first, so one still open is stuck). A
   writer's PR is linked or queued by intake's rules, as a webhook's is (it
   becomes the `pr` of the open item it names, `Ashigaru-Item: a676`, rather
   than a second item). The mark moves to this run's start once the search
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
from .model import Kind, Lane, Mode

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
DEFERRED_FOR = timedelta(days=1)
"""How long a hit the sweep could not judge may hold the mark back before the search reads on."""
KEPT = timedelta(days=30)
"""How long a run's row is kept."""
LOCK = "sweep-lock"
"""Held by the run under way as a lease, `<heartbeat> <run id>`: the run renews it every quarter
lease, so one killed mid-run (podman stops with SIGKILL after 10 s) frees it within a lease."""
LEASE = 60.0
"""Seconds a lease lasts unrenewed."""
WRITERS = frozenset({"OWNER", "MEMBER"})
BOTS = {"dependabot[bot]": "Dependabot", "renovate[bot]": "Renovate"}
"""The dependency bots whose PRs land themselves, by login, with the name a task calls them by."""
BOT_LOGINS = json.dumps(sorted(BOTS))
BOT_AFTER = timedelta(days=1)
"""How long a dependency bot's PR may stay open before it is queued; ASHIGARU_DEPENDABOT_AFTER."""
ACTOR = "desk"
SOURCE = "sweep"
LOG = logging.getLogger(__name__)

OPEN_SUBJECTS = f"""
    SELECT subject_key FROM items WHERE {store.OPEN} AND subject_key IS NOT NULL
    UNION SELECT pr FROM items WHERE {store.OPEN} AND pr IS NOT NULL
"""
PENDING = """
    SELECT subject_key FROM subjects
    WHERE author IN (SELECT value FROM json_each(:bots)) AND kind = 'story' AND state = 'open'
      AND subject_key NOT IN (
        SELECT subject_key FROM items WHERE subject_key IS NOT NULL
    )
"""
"""The dependency bots' open PRs with no item, read back every run until one is queued or closes."""
LISTED = """
    SELECT DISTINCT json_extract(o.value, '$.subject_key') FROM sweeps s, json_each(s.outsiders) o
    WHERE json_extract(o.value, '$.author') IN (SELECT value FROM json_each(:bots))
      AND json_extract(o.value, '$.subject_key') NOT IN (SELECT subject_key FROM subjects)
      AND json_extract(o.value, '$.subject_key') NOT IN (
          SELECT subject_key FROM items WHERE subject_key IS NOT NULL
      )
"""
"""The dependency bots' PRs that runs before PENDING listed as outsiders, read back once to record them."""
BACKFILLED = "sweep-bot-backfill"
"""Set once a run has read LISTED back without an error, so no later run scans old runs again. It was
`sweep-dependabot-backfill` before Renovate was a BOT: the new name reads old runs back once more."""
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
FORGET = """
    UPDATE sweeps SET outsiders = (
        SELECT json_group_array(json(o.value)) FROM json_each(sweeps.outsiders) o
        WHERE json_extract(o.value, '$.subject_key') NOT IN (SELECT value FROM json_each(:keys))
    )
    WHERE finished_at >= :since AND EXISTS (
        SELECT 1 FROM json_each(sweeps.outsiders) o
        WHERE json_extract(o.value, '$.subject_key') IN (SELECT value FROM json_each(:keys))
    )
"""
"""Drop the closed subjects from the outsiders of the runs NEEDS YOU reads (OUTSIDERS: the last day),
in one statement per run."""


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
    bot_after: timedelta = BOT_AFTER


@dataclasses.dataclass
class Counts:
    subjects: int = 0
    new: int = 0
    closed: int = 0
    outsiders: list[dict[str, str | None]] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    writers: dict[tuple[str, str], bool] = dataclasses.field(default_factory=dict)
    """The forge's answer for each (repo, author) asked this run."""
    lost: threading.Event = dataclasses.field(default_factory=threading.Event)
    """Set when the lease could not be renewed: another run may start, so this one stops."""
    unasked: dict[tuple[str, str], str] = dataclasses.field(default_factory=dict)
    """Why the forge could not answer for a (repo, author): its hits wait for the next run."""
    forgotten: set[str] = dataclasses.field(default_factory=set)
    """Outsiders' subjects the search found closed, for FORGET to drop from earlier runs."""

    def error(self) -> str | None:
        more = f" (and {len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        return f"{self.errors[0]}{more}" if self.errors else None


OUTSIDERS = """
    SELECT DISTINCT json_extract(o.value, '$.subject_key') AS subject_key,
           json_extract(o.value, '$.ref') AS ref, json_extract(o.value, '$.author') AS author
    FROM sweeps s, json_each(s.outsiders) o
    WHERE s.finished_at >= :since AND coalesce(json_extract(o.value, '$.author'), '') NOT IN (
        SELECT value FROM json_each(:bots)
      )
      AND json_extract(o.value, '$.subject_key') NOT IN (
        SELECT subject_key FROM items WHERE subject_key IS NOT NULL
    )
    LIMIT :limit
"""
"""The outsiders the runs of the last day listed that still have no item: Scott's to look at. A
dependency bot is never one: the sweep queues its stuck PRs itself."""


def outsiders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    since = _stamp(datetime.now(UTC) - FIRST)
    found = conn.execute(OUTSIDERS, {"since": since, "limit": OUTSIDERS_SHOWN, "bots": BOT_LOGINS})
    return [dict(row) for row in found]


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


class LeaseLostError(Exception):
    """The run's lease could not be renewed: it stops before another run's work overlaps it."""


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


def _stuck(
    conn: sqlite3.Connection, settings: Settings, item: IntakeItem, started: datetime, counts: Counts
) -> None:
    """Queue a dependency bot's PR still open `bot_after` after it was opened and with no item, auto
    on exedore-max, to land or close; the caller holds the transaction. Its task names the subject and
    never quotes it, as intake's do."""
    if item.author not in BOTS or item.kind != Kind.STORY or item.closed or not item.opened_at:
        return
    if item.opened_at > _stamp(started - settings.bot_after):
        return
    if store.item_by_subject(conn, item.subject_key) is not None:
        return
    bot = BOTS[item.author]
    fields = {
        "kind": Kind.STORY,
        "subject_key": item.subject_key,
        "task": f"Land or close {bot} {item.subject_key}: make CI green or close it with a reason",
        "done_when": "PR merged-ready green or closed",
        "ref": item.ref,
        "repo": item.repo,
        "lane": Lane.EXEDORE_MAX,
        "mode": Mode.AUTO,
        "extra": {
            "source": item.source,
            "title": item.title,
            "author": item.author,
            "opened_at": item.opened_at,
        },
    }
    store.insert_item(conn, fields, ACTOR, SOURCE)
    counts.new += 1


def _outsider(
    conn: sqlite3.Connection, settings: Settings, item: IntakeItem, counts: Counts, started: datetime
) -> None:
    """A new subject by someone neither a member nor trusted: listed for Scott while it is open, never
    stored, and forgotten once it is closed (FORGET). A dependency bot's is recorded instead, and
    queued once it is stuck (`_stuck`)."""
    if item.author not in BOTS and item.closed:
        counts.outsiders = [found for found in counts.outsiders if found["subject_key"] != item.subject_key]
        counts.forgotten.add(item.subject_key)
        return
    if item.author not in BOTS:
        outsider = {"subject_key": item.subject_key, "ref": item.ref, "author": item.author}
        if outsider not in counts.outsiders and len(counts.outsiders) < OUTSIDERS_SHOWN:
            counts.outsiders.append(outsider)
        return
    with store.transaction(conn):
        subjects.record(conn, item, SOURCE)
        _stuck(conn, settings, item, started, counts)
        counts.subjects += 1


def _found(
    conn: sqlite3.Connection, settings: Settings, hit: Found, counts: Counts, started: datetime
) -> None:
    """A search hit. A member's or trusted author's new subject goes through intake as a delivery's
    does: linked to the item it names, held back for Josui when a seat may have opened it, else queued,
    claimable only when the forge says its author may write to the repo (org membership is not write
    access), else held; anyone else's is an `_outsider`. A subject with an item, or linked to one,
    is recorded and settled."""
    item = dataclasses.replace(hit.item, body=hit.body)
    member = hit.association in WRITERS or item.author in settings.authors
    if not member and store.item_by_subject(conn, item.subject_key) is None:
        _outsider(conn, settings, item, counts, started)
        return
    asked = (item.repo or "", item.author or "")
    if asked in counts.unasked:
        raise Unanswered(f"may {item.author} write? {counts.unasked[asked]}")
    writes = item.author in settings.authors or (member and counts.writers.get(asked, False))
    with store.transaction(conn):
        placed: str | None = None
        if store.item_by_subject(conn, item.subject_key) is None:
            placed = intake.upsert(conn, dataclasses.replace(item, author_writes=writes, judged_at=0))
            counts.new += placed is not None and store.item_by_subject(conn, item.subject_key) is not None
        if placed is not None or store.item_by_subject(conn, item.subject_key) is not None:
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
def _renewing(db_path: str, settings: Settings, run_id: int, lost: threading.Event) -> Iterator[None]:
    """Renew run_id's lease every quarter lease, on its own connection, until the run ends. A renewal
    that fails, or finds the lease no longer this run's, sets `lost` and stops renewing."""
    done = threading.Event()

    def renew() -> None:
        conn = store.connect(db_path)
        try:
            while not done.wait(settings.lease / 4):
                beat = f"{_beat(datetime.now(UTC))} {run_id}"
                if not conn.execute(RENEW, (beat, store.now(), LOCK, f"% {run_id}")).rowcount:
                    LOG.error("sweep run %s no longer holds the lease", run_id)
                    lost.set()
                    return
        except sqlite3.Error:
            LOG.exception("sweep run %s could not renew its lease", run_id)
            lost.set()
        finally:
            conn.close()

    renewer = threading.Thread(target=renew, name=f"sweep-lease-{run_id}", daemon=True)
    renewer.start()
    try:
        yield
    finally:
        done.set()
        renewer.join()


def _read(
    conn: sqlite3.Connection, settings: Settings, keys: set[str], counts: Counts, started: datetime
) -> None:
    """Each open item's subject, and each PENDING one, read back FETCHERS at a time in batches of
    READ_BATCH, so the futures queued stay bounded however many items are open, then recorded and
    settled in key order, a stuck dependency bot's PR queued; one it cannot read is an error."""
    wanted = [(key, found) for key in sorted(keys) if (found := _number(key))]
    with ThreadPoolExecutor(FETCHERS) as pool:
        for batch in itertools.batched(wanted, READ_BATCH):
            reads = {key: pool.submit(settings.forge.subject, *found, settings.token) for key, found in batch}
            if counts.lost.is_set():
                raise LeaseLostError
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
                    _stuck(conn, settings, item, started, counts)
                    counts.subjects += 1


def _ask(conn: sqlite3.Connection, settings: Settings, hits: list[Found], counts: Counts) -> None:
    """Ask the forge, FETCHERS at a time, whether each member who authored a new subject
    (a hit with no item) may write to its repo, once per (repo, author), so `_found` reads the answers
    from `counts.writers`. A failed check is a no, as the forge's own permission check makes it.
    Org members are few, so this is a handful of calls."""
    asked = json.dumps(sorted({hit.item.subject_key for hit in hits}))
    known = {row[0] for row in conn.execute(KNOWN, (asked,))}
    pairs = {
        (hit.item.repo, hit.item.author)
        for hit in hits
        if hit.association in WRITERS
        and hit.item.repo
        and hit.item.author
        and hit.item.author not in settings.authors
        and hit.item.subject_key not in known
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
            LOG.warning("sweep could not ask whether %s may write to %s", pair[1], pair[0], exc_info=failed)


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
        if counts.lost.is_set():
            raise LeaseLostError
        try:
            if hit.item.subject_key not in keys:
                _found(conn, settings, hit, counts, started)
        except UNREADABLE as failed:
            counts.errors.append(f"{hit.item.subject_key}: {_why(failed)}")
            deferred.append(hit.item.updated_at or "")
    if search.complete or not search.hits:
        reached: str | None = _stamp(started)
    else:
        reached = search.reached or search.hits[-1].item.updated_at
    if not search.complete:
        counts.errors.insert(0, "search: cut at the forge's cap; the next run reads on")
    oldest = _stamp(started - DEFERRED_FOR)
    held = min([reached or _stamp(started), *(when for when in deferred if when >= oldest)])
    if any(when < oldest for when in deferred):
        counts.errors.insert(0, f"search: gave up on hits deferred past {DEFERRED_FOR}")
    if held:  # a hit with no time cannot be read from again: the mark stays
        store.set_mark(conn, MARK, held, ACTOR)


def _forget(conn: sqlite3.Connection, counts: Counts, started: datetime) -> None:
    """Drop the outsiders the search found closed from the last day's runs, in one statement."""
    if counts.forgotten:
        forgotten = {"keys": json.dumps(sorted(counts.forgotten)), "since": _stamp(started - FIRST)}
        with store.transaction(conn):
            conn.execute(FORGET, forgotten)


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
            with _renewing(db_path, settings, run_id, counts.lost):
                keys = {row[0] for row in conn.execute(OPEN_SUBJECTS)}
                keys |= {row[0] for row in conn.execute(PENDING, {"bots": BOT_LOGINS}) if row[0]}
                backfill = store.get_mark(conn, BACKFILLED) is None
                if backfill:
                    keys |= {row[0] for row in conn.execute(LISTED, {"bots": BOT_LOGINS}) if row[0]}
                _read(conn, settings, keys, counts, started)
                if backfill and not counts.errors:
                    store.set_mark(conn, BACKFILLED, _stamp(started), ACTOR)
                _search(conn, settings, keys, started, counts)
                _forget(conn, counts, started)
                counts.closed += len(subjects.hygiene(conn))
        except LeaseLostError:  # the next run reads it all again; the mark has not moved
            counts.errors.insert(0, "stopped: lost the lease")
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
