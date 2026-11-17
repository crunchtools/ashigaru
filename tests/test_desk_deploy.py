"""How the service is configured and checked where it runs: proxies, settings, health, version."""

import importlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import ashigaru
from ashigaru.desk import __main__ as desk_main
from ashigaru.desk.api import create_app

FORWARDED = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}


def probe(db: Path, trusted: list[str]) -> dict[str, str]:
    app = create_app(db, trusted_proxies=trusted)

    @app.get("/probe")
    def where(request: Request) -> dict[str, str]:
        return {"client": request.client.host if request.client else "", "scheme": request.url.scheme}

    with TestClient(app) as client:
        return dict(client.get("/probe", headers=FORWARDED).json())


def test_forwarded_headers_count_only_from_a_trusted_proxy(db: Path) -> None:
    # TestClient connects as "testclient", so that is the proxy's address here.
    assert probe(db, ["testclient"]) == {"client": "203.0.113.7", "scheme": "https"}
    assert probe(db, ["10.0.0.0/8"]) == {"client": "testclient", "scheme": "http"}
    assert probe(db, []) == {"client": "testclient", "scheme": "http"}


def test_settings_come_from_the_documented_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASHIGARU_BIND", "10.8.0.1")
    monkeypatch.setenv("ASHIGARU_TRUSTED_PROXIES", " 10.88.0.1, 172.16.0.0/12 ,")
    assert desk_main.setting("ASHIGARU_BIND", "127.0.0.1") == "10.8.0.1"
    assert desk_main.proxies(desk_main.setting("ASHIGARU_TRUSTED_PROXIES")) == ["10.88.0.1", "172.16.0.0/12"]
    with pytest.raises(KeyError):
        desk_main.setting("ASHIGARU_UNDOCUMENTED")
    docs = (Path(__file__).resolve().parents[1] / "docs" / "desk-api.md").read_text()
    assert all(f"`{name}`" in docs for name in desk_main.SETTINGS)


class Healthz(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[int]:
    httpd = HTTPServer(("127.0.0.1", 0), Healthz)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()


def test_health_checks_the_local_listener(server: int) -> None:
    assert desk_main.main(["health", "--host", "0.0.0.0", "--port", str(server)]) == 0
    assert desk_main.main(["health", "--host", "127.0.0.1", "--port", "1"]) == 1


def test_version_comes_from_the_installed_package(monkeypatch: pytest.MonkeyPatch) -> None:
    version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    assert ashigaru.__version__ == version

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", missing)
    assert importlib.reload(ashigaru).__version__ == "0"
    monkeypatch.undo()
    importlib.reload(ashigaru)


def test_serve_hands_the_settings_to_the_app_and_turns_off_uvicorns_proxy_trust(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[FastAPI, dict[str, object]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: calls.append((app, options)))
    monkeypatch.setenv("ASHIGARU_TRUSTED_PROXIES", "10.88.0.1")
    argv = ["serve", "--db", str(db), "--host", "10.8.0.1", "--port", "9000"]
    assert desk_main.main(argv) == 0
    [(app, options)] = calls
    assert options == {"host": "10.8.0.1", "port": 9000, "proxy_headers": False, "server_header": False}
    [middleware] = [entry for entry in app.user_middleware if entry.cls is ProxyHeadersMiddleware]
    assert middleware.kwargs == {"trusted_hosts": ["10.88.0.1"]}
    assert app.state.db_path == str(db)

    calls.clear()
    monkeypatch.delenv("ASHIGARU_TRUSTED_PROXIES")
    assert desk_main.main(argv) == 0
    assert not [entry for entry in calls[0][0].user_middleware if entry.cls is ProxyHeadersMiddleware]
