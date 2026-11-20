"""The Desk's REST API over httpx. Redirects are not followed: the bearer key goes only where the
config points."""

from pathlib import Path
from typing import Any

import httpx

from .config import Config

EPOCH_HEADER = "X-Ashigaru-Epoch"


def epoch_file() -> Path:
    """The epoch of the session the Desk registered for this seat, "N\n", beside the task marker."""
    return Path.home() / ".local/state/ashigaru/epoch"


def epoch() -> int | None:
    """The epoch in epoch_file(), or None when it is missing or not an integer."""
    try:
        return int(epoch_file().read_text().strip())
    except (OSError, ValueError):
        return None


class DeskError(Exception):
    """The Desk answered with an error; `detail` is its `{"detail": ..., "code": ...}`, and `code` is
    None when it sent none, as an old Desk does."""

    def __init__(self, status: int, detail: str, code: str | None = None) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.code = code


class Unreachable(Exception):
    """No answer from the Desk: DNS, connect, TLS or a timeout."""


class Desk:
    def __init__(
        self, config: Config, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = httpx.Client(
            base_url=config.url,
            headers={"Authorization": f"Bearer {config.api_key}", "User-Agent": "ashigaru-cli"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        **options: Any,
    ) -> Any:
        """`options` are `timeout`, which replaces the client's for this request, for a long-poll, and
        `headers`, added to it, such as If-Match. The seat's epoch, when epoch_file() holds one, goes on
        every request."""
        timeout, headers = options.get("timeout"), options.get("headers")
        wait = httpx.USE_CLIENT_DEFAULT if timeout is None else httpx.Timeout(timeout)
        held = epoch()
        sent = ({EPOCH_HEADER: str(held)} if held is not None else {}) | (headers or {})
        try:
            response = self._http.request(method, path, params=params, json=body, timeout=wait, headers=sent)
        except httpx.TransportError as exc:
            raise Unreachable(str(exc) or type(exc).__name__) from exc
        if response.is_redirect:
            raise DeskError(response.status_code, "the Desk redirected; check its URL")
        if response.is_error:
            code = None
            try:
                answer = response.json()
                detail = answer.get("detail")
                code = answer.get("code") if isinstance(answer.get("code"), str) else None
            except (ValueError, AttributeError):
                detail = response.text[:200] or response.reason_phrase
            raise DeskError(response.status_code, detail if isinstance(detail, str) else str(detail), code)
        return response.json() if response.content else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self.request("POST", path, body={} if body is None else body, headers=headers)

    def close(self) -> None:
        self._http.close()
