"""The Desk's REST API over httpx. Redirects are not followed: the bearer key goes only where the
config points."""

from typing import Any

import httpx

from .config import Config


class DeskError(Exception):
    """The Desk answered with an error; `detail` is its `{"detail": ...}`."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


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
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None
    ) -> Any:
        try:
            response = self._http.request(method, path, params=params, json=body)
        except httpx.TransportError as exc:
            raise Unreachable(str(exc) or type(exc).__name__) from exc
        if response.is_redirect:
            raise DeskError(response.status_code, "the Desk redirected; check its URL")
        if response.is_error:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = response.text[:200] or response.reason_phrase
            raise DeskError(response.status_code, detail if isinstance(detail, str) else str(detail))
        return response.json() if response.content else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, body={} if body is None else body)

    def close(self) -> None:
        self._http.close()
