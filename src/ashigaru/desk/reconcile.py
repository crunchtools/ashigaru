"""Seat reconcile: the Desk's fallback for seat hooks that never arrived.

A seat's hooks are the fast path: `prompt-submit` marks its turn active, `stop`
idle, and `statusline` posts its usage every 30 seconds or so while Claude Code
runs, so any write to the seat row proves it alive. A `stop` hook killed by its
2-second alarm, a crash or a container restart leaves a turn `active` that no
hook will ever end. Every EVERY seconds, and at startup, one run:

1. ends a turn active longer than TURN_MAX whose row has not changed in
   HEARTBEAT: `turn` goes idle and `turn_reconciled_at` says the Desk, not the
   seat, did it (a new turn clears it);
2. drops the `task` of a seat with no hold of Scott's that has been idle for
   STALE_SEAT, as a /clear would have.

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
LOG = logging.getLogger(__name__)

END_TURNS = """
    UPDATE seats SET turn = 'idle', turn_reconciled_at = :now
    WHERE turn = 'active' AND turn_started_at < :began AND updated_at < :heard
"""
DROP_TASKS = """
    UPDATE seats SET task = NULL, waiting_on = NULL
    WHERE turn = 'idle' AND owner IS NULL AND task IS NOT NULL AND updated_at < :stale
"""


@dataclasses.dataclass(frozen=True)
class Settings:
    """How reconcile runs: every `every` seconds, ending a silent turn active past `turn_max`."""

    every: float = EVERY
    turn_max: timedelta = TURN_MAX


def run(db_path: str, settings: Settings) -> dict[str, int]:
    """One reconcile on its own connection: how many turns it ended and tasks it dropped."""
    now = datetime.now(UTC)
    conn = store.connect(db_path)
    try:
        with store.transaction(conn):
            ended = conn.execute(
                END_TURNS,
                {
                    "now": _stamp(now),
                    "began": _stamp(now - settings.turn_max),
                    "heard": _stamp(now - HEARTBEAT),
                },
            ).rowcount
            dropped = conn.execute(DROP_TASKS, {"stale": _stamp(now - STALE_SEAT)}).rowcount
    finally:
        conn.close()
    if ended or dropped:
        LOG.info("seat reconcile: %d turns ended, %d tasks dropped", ended, dropped)
    return {"turns": ended, "tasks": dropped}


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Reconciler:
    """The timer: a run at startup, then every `settings.every` seconds; lives on the event loop."""

    def __init__(self, db_path: str, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings

    async def loop(self) -> None:
        while True:
            try:
                await run_in_threadpool(run, self.db_path, self.settings)
            except Exception:  # the timer outlives any one run
                LOG.exception("seat reconcile failed")
            await asyncio.sleep(self.settings.every)
