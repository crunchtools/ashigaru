"""Seat reconcile: the Desk's fallback for seat hooks that never arrived.

A seat's hooks are the fast path: `prompt-submit` marks its turn active, `stop`
idle, and `statusline` posts its usage every 30 seconds or so while Claude Code
runs, so any write to the seat row proves it alive. A `stop` hook killed by its
2-second alarm, a crash or a container restart leaves a turn `active` that no
hook will ever end. Every EVERY seconds, and at startup, one run:

1. ends a turn active longer than TURN_MAX whose row has not changed in
   HEARTBEAT: `turn` goes idle and `turn_reconciled_at` says the Desk, not the
   seat, did it (a new turn clears it). Silence counts from no earlier than the
   Desk's own start, since no status line could reach a Desk that was down: a
   restart after an outage leaves live turns alone until they miss a heartbeat;
2. drops the `task` of a seat with no hold of Scott's that has been idle for
   STALE_SEAT, as a /clear would have;
3. flags a busy seat whose row has not been written for SILENT: `silent_since` says
   since when, and any write to the seat clears it. Like (1), it waits until the
   Desk has been up for SILENT.

It updates the rows directly, leaving `updated_at`, so a dead seat still goes
`missing` on time. It never closes or wraps an item: a manual hold left behind
by a /clear that never fired stays held, and the board lists it under NEEDS
YOU (`stale`) for Scott, since only he knows whether that work is finished.
"""

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime, timedelta

from starlette.concurrency import run_in_threadpool

from . import store
from .model import STALE_SEAT

EVERY = 300.0
"""Seconds between runs."""
TURN_MAX = timedelta(hours=3)
"""How long a turn may stay active before a silent one is ended; ASHIGARU_TURN_MAX, in seconds."""
HEARTBEAT = timedelta(minutes=10)
"""How long a seat with a live turn can go without a write: its statusline posts every 30 seconds."""
SILENT = timedelta(minutes=15)
"""How long a busy seat may go without a write before it is flagged; ASHIGARU_SILENT_AFTER, in seconds."""
LOG = logging.getLogger(__name__)

END_TURNS = """
    UPDATE seats SET turn = 'idle', turn_reconciled_at = :now
    WHERE turn = 'active' AND coalesce(turn_started_at, updated_at) < :began AND updated_at < :heard
"""
"""A turn with no start (active before turn_started_at existed) began no later than its last write."""
DROP_TASKS = """
    UPDATE seats SET task = NULL, waiting_on = NULL
    WHERE turn = 'idle' AND owner IS NULL AND task IS NOT NULL AND updated_at < :stale
"""
FLAG_SILENT = """
    UPDATE seats SET silent_since = :now
    WHERE status = 'busy' AND silent_since IS NULL AND updated_at < :quiet
"""
"""Leaves `updated_at` alone, like the others: the flag is the Desk's, not a write by the seat."""


@dataclasses.dataclass(frozen=True)
class Settings:
    """How reconcile runs: every `every` seconds, ending a silent turn active past `turn_max` and flagging
    a busy seat with no write for `silent`."""

    every: float = EVERY
    turn_max: timedelta = TURN_MAX
    silent: timedelta = SILENT


def run(db_path: str, settings: Settings, started: datetime | None = None) -> dict[str, int]:
    """One reconcile on its own connection: how many turns it ended, tasks it dropped and seats it flagged
    silent. A turn's silence is measured from the later of its row's last write and `started`, when the
    Desk came up."""
    now = datetime.now(UTC)
    heard = now - HEARTBEAT
    quiet = now - settings.silent
    conn = store.connect(db_path)
    try:
        with store.transaction(conn):
            ended = (
                conn.execute(
                    END_TURNS,
                    {"now": _stamp(now), "began": _stamp(now - settings.turn_max), "heard": _stamp(heard)},
                ).rowcount
                if started is None or started <= heard
                else 0
            )
            dropped = conn.execute(DROP_TASKS, {"stale": _stamp(now - STALE_SEAT)}).rowcount
            flagged = (
                conn.execute(FLAG_SILENT, {"now": _stamp(now), "quiet": _stamp(quiet)}).rowcount
                if started is None or started <= quiet
                else 0
            )
    finally:
        conn.close()
    if ended or dropped or flagged:
        LOG.info("seat reconcile: %d turns ended, %d tasks dropped, %d seats silent", ended, dropped, flagged)
    return {"turns": ended, "tasks": dropped, "silent": flagged}


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Reconciler:
    """The timer: a run at startup, then every `settings.every` seconds; lives on the event loop."""

    def __init__(self, db_path: str, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings
        self.started = datetime.now(UTC)

    async def loop(self) -> None:
        while True:
            try:
                await run_in_threadpool(run, self.db_path, self.settings, self.started)
            except Exception:  # the timer outlives any one run
                LOG.exception("seat reconcile failed")
            await asyncio.sleep(self.settings.every)
