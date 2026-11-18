"""`ashigaru wait`: block until an item settles, or until a commit's CI does.

An item has no callback, so `PollWaiter` reads it every 30 to 60 seconds. A
commit does: `CIWaiter` long-polls `GET /v1/ci/wait`, which the Desk answers as
soon as a webhook delivery settles the commit, or after at most 90 seconds with
`pending`, when it asks again. Nothing polls the forge.
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
CI_PENDING = "pending"
CI_SUCCEEDED = "success"
MAX_WAIT = 90
"""The most one GET /v1/ci/wait blocks, server side (Cloudflare drops a request at 100s)."""
SLACK = 15.0
"""Added to the client's timeout on a long-poll, so the Desk answers before the client gives up."""


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


class CIWaiter:
    """Long-polls the Desk's CI result for a commit until it is not pending. A blip is retried after
    `pause` seconds; a 404 means this Desk serves no CI wait."""

    def __init__(
        self,
        desk: "Desk",
        pause: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.desk, self.pause, self.sleep, self.clock = desk, pause, sleep, clock

    def _ask(self, repo: str, sha: str, seconds: int, branch: str | None) -> dict[str, Any] | None:
        params: dict[str, str | int] = {"repo": repo, "sha": sha, "timeout": seconds}
        if branch is not None:
            params["branch"] = branch
        try:
            result: dict[str, Any] = self.desk.request(
                "GET",
                "/v1/ci/wait",
                params=params,
                timeout=seconds + SLACK,
            )
        except Unreachable:
            return None
        except DeskError as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                raise DeskError(exc.status, "this Desk serves no CI wait (GET /v1/ci/wait)") from exc
            if exc.status < HTTPStatus.INTERNAL_SERVER_ERROR:
                raise
            return None
        return result

    def wait(self, repo: str, sha: str, timeout: float | None, branch: str | None = None) -> dict[str, Any]:
        """The commit's CI result once it is not pending. At the timeout, a TimeoutError naming what is
        still pending: success is never reported while a required check has not."""
        end = None if timeout is None else self.clock() + timeout
        last: dict[str, Any] | None = None
        while True:
            left = MAX_WAIT if end is None else max(0, min(MAX_WAIT, int(end - self.clock())))
            result = self._ask(repo, sha, left, branch)
            if result is not None and result["status"] != CI_PENDING:
                return result
            last = result or last
            if result is None:
                # A blip: pause, but never past the deadline.
                self.sleep(self.pause if end is None else max(0.0, min(self.pause, end - self.clock())))
            if end is not None and end - self.clock() < 1:
                raise TimeoutError(f"{repo}@{sha[:12]} after {timeout:g}s, {still(last)}")


def still(result: dict[str, Any] | None) -> str:
    """`pending: <names>`, or why nothing more can be said."""
    names = (result or {}).get("pending") or []
    unverified = (result or {}).get("unverified")
    said = f"pending: {', '.join(names)}" if names else "pending: no check has reported"
    return f"{said}; {unverified}" if unverified else said


def waiter_for(desk: "Desk") -> Waiter:
    """An item's waiter: items have no callback, so it polls."""
    return PollWaiter(desk)
