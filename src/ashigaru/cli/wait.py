"""`ashigaru wait`: block until an item settles.

The Desk has no CI callback yet, so `PollWaiter` reads the item every 30 to 60
seconds. The seam: once the Desk serves `desk_wait` (GitHub's `workflow_run`
and `check_suite` completed events), a `CallbackWaiter` makes one long request
instead and `waiter_for` returns it; `commands.wait` and the CLI do not change.
"""

import time
from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from .client import DeskError, Unreachable

if TYPE_CHECKING:
    from .client import Desk

SETTLED = frozenset({"review", "done", "blocked", "wontdo", "abandoned"})
SUCCEEDED = frozenset({"review", "done"})


class Waiter(Protocol):
    def wait(self, item: str, timeout: float | None) -> dict[str, Any]: ...


class PollWaiter:
    """Polls the item, backing off from `first` to `cap` seconds. A blip (no answer, or a 5xx)
    is retried at the next poll rather than ending a long wait."""

    def __init__(
        self,
        desk: "Desk",
        first: float = 30.0,
        cap: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.desk, self.first, self.cap, self.sleep, self.clock = desk, first, cap, sleep, clock

    def _poll(self, item: str) -> dict[str, Any] | None:
        try:
            current: dict[str, Any] = self.desk.get(f"/v1/items/{quote(item, safe='')}")
        except Unreachable:
            return None
        except DeskError as exc:
            if exc.status < HTTPStatus.INTERNAL_SERVER_ERROR:
                raise
            return None
        return current

    def wait(self, item: str, timeout: float | None) -> dict[str, Any]:
        end = None if timeout is None else self.clock() + timeout
        delay = self.first
        while True:
            current = self._poll(item)
            if current is not None and current["state"] in SETTLED:
                return current
            if end is not None and self.clock() + delay > end:
                raise TimeoutError(f"{item} has not settled after {timeout:g}s")
            self.sleep(delay)
            delay = min(delay * 1.5, self.cap)


def waiter_for(desk: "Desk") -> Waiter:
    """The waiter this Desk supports: polling until it serves the CI callback."""
    return PollWaiter(desk)
