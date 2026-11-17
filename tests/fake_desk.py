"""A fake Desk for the CLI's tests: a real HTTP server on loopback that answers from a route table
and records each request, so the client is tested over the wire, timeouts included."""

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

Answer = tuple[int, Any]
REDIRECTS = frozenset({301, 302, 303, 307, 308})


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any


@dataclass
class FakeDesk:
    url: str = ""
    routes: dict[tuple[str, str], Answer | Callable[[Seen], Answer]] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)
    delay: float = 0.0

    def on(self, method: str, path: str, answer: Answer | Callable[[Seen], Answer]) -> None:
        self.routes[(method, path)] = answer

    def calls(self, method: str, path: str) -> list[Seen]:
        return [s for s in self.seen if (s.method, s.path) == (method, path)]


def _handler(desk: FakeDesk) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _answer(self) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            seen = Seen(
                self.command,
                parts.path,
                parse_qs(parts.query),
                dict(self.headers.items()),
                json.loads(raw) if raw else None,
            )
            desk.seen.append(seen)
            time.sleep(desk.delay)
            route = desk.routes.get((self.command, parts.path), (404, {"detail": "not found"}))
            status, body = route(seen) if callable(route) else route
            payload = json.dumps(body).encode()
            self.send_response(status)
            if status in REDIRECTS:
                self.send_header("Location", "https://elsewhere.example/")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

    return Handler


@contextmanager
def serve() -> Iterator[FakeDesk]:
    desk = FakeDesk()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(desk))
    server.daemon_threads = True
    desk.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield desk
    finally:
        server.shutdown()
        server.server_close()
