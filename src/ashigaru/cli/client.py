"""The Desk's REST API over httpx. Redirects are not followed: the bearer key goes only where the
config points."""

import dataclasses
import os
from typing import Any

import httpx

from .config import Config

SESSION_HEADER = "X-Ashigaru-Session"
# What Claude Code puts in the environment of a seat's shell commands: the session they run in.
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


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


@dataclasses.dataclass(frozen=True)
class Send:
    """How one request goes beyond its method, path, query and body: `timeout` replaces the client's
    (a long-poll), `headers` are added (If-Match)."""

    timeout: float | None = None
    headers: dict[str, str] = dataclasses.field(default_factory=dict)


class Desk:
    def __init__(
        self,
        config: Config,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        session: str | None = None,
    ) -> None:
        """`session` is the Claude Code session a hook runs for, else the one the CLI runs in
        (SESSION_ENV), sent on every request so the Desk refuses a session it has ended or another
        session's on the seat. Outside a seat there is none, and nothing is sent."""
        session = session or os.environ.get(SESSION_ENV) or None
        self._session = {SESSION_HEADER: session} if session else {}
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
        send: Send | None = None,
    ) -> Any:
        """`send` says how: a timeout replacing the client's, for a long-poll, and headers added to the
        request, such as If-Match. The session goes on every request."""
        timeout, headers = (send.timeout, send.headers) if send else (None, {})
        wait = httpx.USE_CLIENT_DEFAULT if timeout is None else httpx.Timeout(timeout)
        sent = self._session | (headers or {})
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
        return self.request("POST", path, body={} if body is None else body, send=Send(headers=headers or {}))

    def close(self) -> None:
        self._http.close()
