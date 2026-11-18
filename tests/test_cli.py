"""The ashigaru CLI against a fake Desk (tests/fake_desk.py)."""

import io
import json
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fake_desk import FakeDesk, Seen, serve

from ashigaru.cli import commands, config, hooks
from ashigaru.cli.__main__ import main
from ashigaru.cli.client import Desk, DeskError, Unreachable
from ashigaru.cli.wait import PollWaiter

KEY = "ash_k1_secret"
USAGE_ERROR = 2


def item(**fields: Any) -> dict[str, Any]:
    return {"id": "a1", "task": "build it", "mode": "auto", "state": "queued", "seat": None} | fields


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeDesk]:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("ASHIGARU_API_KEY_FILE", "ASHIGARU_SEAT"):
        monkeypatch.delenv(name, raising=False)
    with serve() as fake:
        monkeypatch.setenv("ASHIGARU_URL", fake.url)
        monkeypatch.setenv("ASHIGARU_API_KEY", KEY)
        yield fake


def write_config(home: Path, text: str, mode: int = 0o600) -> Path:
    path = home / ".config/ashigaru/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    path.chmod(mode)
    return path


# Config


def test_config_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_config(tmp_path, 'url = "https://desk.example/"\napi_key = "sekrit"\nseat = "Vertex 1"\n')
    loaded = config.load({})
    assert (loaded.url, loaded.api_key, loaded.seat) == ("https://desk.example", "sekrit", "Vertex 1")
    assert "sekrit" not in repr(loaded)


def test_environment_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_config(tmp_path, 'url = "https://file.example"\napi_key = "file"\n')
    keyfile = tmp_path / "key"
    keyfile.write_text("from-file\n")
    keyfile.chmod(0o600)
    env = {"ASHIGARU_URL": "https://env.example", "ASHIGARU_API_KEY": "env"}
    assert config.load(env).api_key == "env"
    assert config.load(env | {"ASHIGARU_API_KEY_FILE": str(keyfile)}).api_key == "from-file"
    assert config.load(env).url == "https://env.example"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_a_loose_config_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_config(tmp_path, 'url = "https://desk.example"\napi_key = "k"\n', mode)
    with pytest.raises(config.ConfigError, match="chmod 600"):
        config.load({})


def test_a_symlinked_config_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "real.toml"
    real.write_text('url = "https://desk.example"\napi_key = "k"\n')
    real.chmod(0o600)
    (tmp_path / ".config/ashigaru").mkdir(parents=True)
    (tmp_path / ".config/ashigaru/config.toml").symlink_to(real)
    with pytest.raises(config.ConfigError):
        config.load({})


@pytest.mark.parametrize(
    "url", ["http://desk.example", "ftp://127.0.0.1", "https://", "desk.example", "http://10.0.0.1"]
)
def test_the_key_only_travels_over_https_or_loopback(url: str) -> None:
    with pytest.raises(config.ConfigError, match="https"):
        config.check_url(url)


def test_loopback_http_is_allowed() -> None:
    assert config.check_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert config.check_url("http://localhost:8000") == "http://localhost:8000"


def test_missing_config_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("ASHIGARU_URL", "ASHIGARU_API_KEY", "ASHIGARU_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert main(["agents"]) == USAGE_ERROR
    assert "no Desk URL" in capsys.readouterr().err


def test_there_is_no_key_argument() -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["agents", "--api-key", KEY])
    assert exit_.value.code == USAGE_ERROR


# Client


def test_the_key_goes_in_the_bearer_header_only(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/seats", (200, []))
    assert main(["agents"]) == 0
    (seen,) = desk.seen
    assert seen.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in str(seen.query)


def test_an_error_carries_the_desks_detail(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items/a1/assign", (409, {"detail": "Vertex 1 is busy"}))
    assert main(["assign", "a1", "--seat", "Vertex 1"]) == 1
    assert "409: Vertex 1 is busy" in capsys.readouterr().err


def test_a_redirect_is_not_followed(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/seats", (302, {}))
    with pytest.raises(DeskError, match="redirected"):
        Desk(config.load()).get("/v1/seats")
    assert len(desk.seen) == 1


def test_no_answer_is_unreachable() -> None:
    gone = Desk(config.Config(url="http://127.0.0.1:9", api_key="k"), timeout=1)
    with pytest.raises(Unreachable):
        gone.get("/v1/seats")


# Views


SEAT = {
    "name": "Vertex 1",
    "status": "busy",
    "owner": None,
    "item": "a1",
    "since": "2026-11-17T10:00:00Z",
    "turn": "active",
    "task": "build it",
}


def test_agents(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/seats", (200, [SEAT, SEAT | {"name": "Max 2", "status": "free", "item": None}]))
    assert main(["agents"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["SEAT", "STATUS", "ITEM", "FOR", "TURN", "TASK"]
    assert lines[1].startswith("Vertex 1") and "a1" in lines[1]


def test_json_is_the_desks_answer(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/seats", (200, [SEAT]))
    assert main(["agents", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [SEAT]


def test_usage(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    seat = {
        "name": "Max 1",
        "model": "claude-opus-5-5",
        "context_used": 50000,
        "context_window": 200000,
        "session_tokens": 1234567,
        "session_cost_usd": 3.5,
        "turn": "active",
        "turn_seconds": 3720,
        "billing": "max",
    }
    spend = {
        "billing": "vertex",
        "today_usd": 12.0,
        "month_usd": 300.25,
        "daily_limit_usd": 50.0,
        "monthly_limit_usd": None,
    }
    desk.on("GET", "/v1/usage", (200, {"seats": [seat], "spend": [spend]}))
    assert main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "50,000 (25%)" in out and "1,234,567" in out and "1h02m" in out
    assert "$300.25" in out and "$50.00" in out


def test_work_all(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    row = {
        "id": "a2",
        "seat": "Max 1",
        "mode": "manual",
        "task": "t",
        "state": "blocked",
        "wrap": None,
        "result_or_blocker": "needs a key",
        "next": None,
    }
    desk.on("GET", "/v1/check", (200, [row]))
    assert main(["work"]) == 0
    assert main(["work", "--all"]) == 0
    first, second = desk.calls("GET", "/v1/check")
    assert first.query == {} and second.query == {"all": ["true"]}
    assert "needs a key" in capsys.readouterr().out


def test_queue_shows_what_each_item_waits_on(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    def items(seen: Seen) -> tuple[int, Any]:
        if "state" not in seen.query:
            return 200, [item(state="working"), item(id="a3")]
        if seen.query["state"] == ["queued"]:
            return 200, [item(id="a3", after=["a1", "a9"], lane="vertex")]
        return 200, [
            item(id="a4", state="proposed", parent="a0", filed_by="Max 1", expires_at="2026-12-01T00:00:00Z")
        ]

    desk.on("GET", "/v1/items", items)
    data = commands.queue(Desk(config.load()))
    assert data["queued"][0]["waits_on"] == [
        {"id": "a1", "state": "working"},
        {"id": "a9", "state": "unknown"},
    ]
    assert [s.path for s in desk.seen if s.path.startswith("/v1/items/")] == ["/v1/items/a9"]
    assert main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "a1 working, a9 unknown" in out and "2026-12-01" in out


def test_queue_does_not_hide_a_refusal_as_unknown(desk: FakeDesk) -> None:
    def items(seen: Seen) -> tuple[int, Any]:
        return 200, [item(id="a3", after=["a9"])] if seen.query.get("state") == ["queued"] else []

    desk.on("GET", "/v1/items", items)
    desk.on("GET", "/v1/items/a9", (403, {"detail": "no"}))
    with pytest.raises(DeskError, match="403"):
        commands.queue(Desk(config.load()))


def test_outcomes(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    row = {
        "id": "a0",
        "state": "working",
        "ref": "RT#1",
        "task": "o",
        "total": 4,
        "done": 1,
        "blocked": 1,
        "open": 2,
    }
    desk.on("GET", "/v1/outcomes", (200, [row]))
    assert main(["outcomes"]) == 0
    assert "1/4" in capsys.readouterr().out


# Actions


def test_assign_by_seat(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items/a1/assign", (200, item(seat="Vertex 1", state="assigned")))
    assert main(["assign", "a1", "--seat", "Vertex 1"]) == 0
    assert desk.seen[0].body == {"seat": "Vertex 1"}


def test_assign_records_a_new_item_then_picks_by_lane(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items", (201, item(id="a7")))
    desk.on("POST", "/v1/items/a7/assign", (200, item(id="a7", seat="Max 3", state="assigned")))
    argv = ["assign", "--task", "ship it", "--lane", "exedore-max", "--after", "a5", "--ref", "RT#9"]
    assert main(argv) == 0
    create, pick = desk.seen
    assert create.body == {"task": "ship it", "lane": "exedore-max", "after": ["a5"], "ref": "RT#9"}
    assert pick.body == {"lane": "exedore-max"}


def test_assign_needs_an_item(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["assign"]) == 1
    assert "name an item" in capsys.readouterr().err
    assert desk.seen == []


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_auto_and_manual(desk: FakeDesk, mode: str) -> None:
    desk.on("PATCH", "/v1/items/a1", (200, item(mode=mode)))
    assert main([mode, "a1"]) == 0
    assert desk.seen[0].body == {"mode": mode}


def test_wrap_refuses_before_cc_done(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/items/a1", (200, item(seat="Max 1", state="review", memorized=False)))
    assert main(["wrap", "a1"]) == 1
    assert "/cc seat" in capsys.readouterr().err
    assert desk.calls("PATCH", "/v1/items/a1") == []


def test_wrap_refuses_a_seat_mid_turn(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/items/a1", (200, item(seat="Max 1", state="review", memorized=True)))
    desk.on("GET", "/v1/seats/Max%201", (200, SEAT | {"name": "Max 1"}))
    assert main(["wrap", "a1"]) == 1
    assert "mid-turn" in capsys.readouterr().err


def test_wrap_clears_and_closes(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/items/a1", (200, item(seat="Max 1", state="review", memorized=True)))
    desk.on("GET", "/v1/seats/Max%201", (200, SEAT | {"turn": "idle"}))
    desk.on("PATCH", "/v1/items/a1", (200, item(state="done", wrap="cleared")))
    assert main(["wrap", "a1", "--learned", "poll less"]) == 0
    assert desk.calls("PATCH", "/v1/items/a1")[0].body == {
        "wrap": "cleared",
        "learned": "poll less",
        "state": "done",
    }


def test_wrap_leaves_a_closed_state_alone(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/items/a1", (200, item(seat="Max 1", state="wontdo")))
    desk.on("PATCH", "/v1/items/a1", (200, item(state="wontdo", wrap="cleared")))
    assert main(["wrap", "a1", "--force"]) == 0
    assert desk.calls("PATCH", "/v1/items/a1")[0].body == {"wrap": "cleared"}


# Wait


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_wait_polls_with_backoff_through_blips(desk: FakeDesk) -> None:
    answers = iter(
        [
            (200, item(state="working")),
            (503, {"detail": "busy"}),
            (200, item(state="working")),
            (200, item(state="working")),
            (200, item(state="review")),
        ]
    )
    desk.on("GET", "/v1/items/a1", lambda seen: next(answers))
    clock = Clock()
    waiter = PollWaiter(Desk(config.load()), sleep=clock.sleep, clock=lambda: clock.now)
    assert waiter.wait("a1", None)["state"] == "review"
    assert clock.slept == [30.0, 45.0, 60.0, 60.0]


def test_wait_times_out(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/items/a1", (200, item(state="working")))
    clock = Clock()
    waiter = PollWaiter(Desk(config.load()), sleep=clock.sleep, clock=lambda: clock.now)
    with pytest.raises(TimeoutError):
        waiter.wait("a1", 100)
    assert clock.slept == [30.0, 45.0]


def test_wait_stops_on_a_refusal(desk: FakeDesk) -> None:
    waiter = PollWaiter(Desk(config.load()), sleep=lambda s: None)
    with pytest.raises(DeskError):
        waiter.wait("a1", None)


@pytest.mark.parametrize(("state", "code"), [("done", 0), ("review", 0), ("blocked", 1), ("abandoned", 1)])
def test_wait_exit_follows_the_state(desk: FakeDesk, state: str, code: int) -> None:
    desk.on("GET", "/v1/items/a1", (200, item(state=state)))
    assert main(["wait", "a1"]) == code


# Hooks


def hook(name: str, event: dict[str, Any]) -> int:
    return hooks.run(name, io.StringIO(json.dumps(event)))


@pytest.fixture
def seat(desk: FakeDesk, monkeypatch: pytest.MonkeyPatch) -> FakeDesk:
    monkeypatch.setenv("ASHIGARU_SEAT", "Max 1")
    desk.on("PATCH", "/v1/seats/Max%201", (200, SEAT))
    return desk


def patches(desk: FakeDesk) -> list[Any]:
    return [s.body for s in desk.calls("PATCH", "/v1/seats/Max%201")]


@pytest.mark.parametrize(
    ("source", "sent"),
    [
        ("startup", [{"turn": "idle"}]),
        ("resume", [{"turn": "idle"}]),
        ("clear", [{"turn": "idle", "task": None}]),
        ("compact", []),
    ],
)
def test_session_start(seat: FakeDesk, source: str, sent: list[Any]) -> None:
    assert hook("session-start", {"hook_event_name": "SessionStart", "source": source}) == 0
    assert patches(seat) == sent


def test_the_first_typed_prompt_is_the_task(seat: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    hook("prompt-submit", {"session_id": "s1", "prompt": "<cross-session-message from=x>do a1"})
    hook("prompt-submit", {"session_id": "s1", "prompt": "  fix the build\n"})
    hook("prompt-submit", {"session_id": "s1", "prompt": "and the docs"})
    hook("prompt-submit", {"session_id": "s2", "prompt": "x" * 300})
    assert patches(seat) == [
        {"turn": "active"},
        {"turn": "active", "task": "fix the build"},
        {"turn": "active"},
        {"turn": "active", "task": "x" * 120},
    ]
    assert capsys.readouterr().out == ""


def test_clear_lets_the_next_typed_prompt_be_the_task(seat: FakeDesk) -> None:
    hook("prompt-submit", {"session_id": "s1", "prompt": "first"})
    hook("session-start", {"session_id": "s1", "source": "clear"})
    hook("prompt-submit", {"session_id": "s1", "prompt": "second"})
    assert patches(seat)[-1] == {"turn": "active", "task": "second"}


def test_stop(seat: FakeDesk) -> None:
    assert hook("stop", {"hook_event_name": "Stop"}) == 0
    assert patches(seat) == [{"turn": "idle"}]


@pytest.mark.parametrize(("started", "sent"), [(10, []), (120, [{"turn": "idle"}])])
def test_idle_prompt_skips_a_turn_that_just_started(seat: FakeDesk, started: int, sent: list[Any]) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - started))
    seat.on("GET", "/v1/seats/Max%201", (200, SEAT | {"turn_started_at": stamp}))
    hook("stop", {"hook_event_name": "Notification"})
    assert patches(seat) == sent


def test_the_seat_defaults_to_the_keys_identity(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/me", (200, {"name": "Vertex 2", "role": "seat", "key_id": "k1"}))
    desk.on("PATCH", "/v1/seats/Vertex%202", (200, SEAT))
    hook("stop", {"hook_event_name": "Stop"})
    assert desk.calls("PATCH", "/v1/seats/Vertex%202")[0].body == {"turn": "idle"}


def test_a_slow_desk_never_holds_a_hook(seat: FakeDesk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "HOOK_SECONDS", 0.3)
    seat.delay = 2.0
    started = time.monotonic()
    assert hook("stop", {"hook_event_name": "Stop"}) == 0
    assert time.monotonic() - started < 1.0


def test_hooks_exit_0_on_every_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("ASHIGARU_URL", "ASHIGARU_API_KEY", "ASHIGARU_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert hook("stop", {}) == 0
    monkeypatch.setenv("ASHIGARU_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ASHIGARU_API_KEY", KEY)
    monkeypatch.setenv("ASHIGARU_SEAT", "Max 1")
    assert hook("stop", {}) == 0
    assert hooks.run("stop", io.StringIO("not json")) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("ashigaru hook stop") == len(["no config", "no answer", "not json"])


def test_the_hook_subcommand_reads_stdin(seat: FakeDesk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"})))
    assert main(["hook", "stop"]) == 0
    assert patches(seat) == [{"turn": "idle"}]


# Hosted MCP URLs

CODE = {
    "code_id": "c0de01",
    "identity": "Vertex 1",
    "label": "phone",
    "created_at": "2026-11-18T00:00:00Z",
    "revoked_at": None,
    "last_used_at": "2026-11-18T01:00:00Z",
}
URL = "https://desk.example/mcp/c0de01_s3cret"
PRIVATE = 0o600
MCP_OF_VERTEX = "/v1/identities/Vertex%201/mcp"


def test_mcp_mint_prints_the_url_once(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", MCP_OF_VERTEX, (201, CODE | {"url": URL}))
    assert main(["mcp", "mint", "Vertex 1", "--label", "phone"]) == 0
    assert capsys.readouterr().out == URL + "\n"
    assert desk.calls("POST", MCP_OF_VERTEX)[0].body == {"label": "phone"}


def test_mcp_mint_out_writes_a_private_new_file(
    desk: FakeDesk, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("POST", MCP_OF_VERTEX, (201, CODE | {"url": URL}))
    out = tmp_path / "vertex1.mcp"
    assert main(["mcp", "mint", "Vertex 1", "--out", str(out)]) == 0
    assert out.read_text() == URL + "\n" and stat.S_IMODE(out.stat().st_mode) == PRIVATE
    printed = capsys.readouterr().out
    assert "s3cret" not in printed and str(out) in printed and "c0de01" in printed
    assert main(["mcp", "mint", "Vertex 1", "--out", str(out)]) == 1
    assert "exists" in capsys.readouterr().err and len(desk.calls("POST", MCP_OF_VERTEX)) == 1


def test_mcp_mint_on_a_desk_without_mcp(
    desk: FakeDesk, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detail = "this Desk serves no MCP: it was installed without the `mcp` extra"
    desk.on("POST", MCP_OF_VERTEX, (409, {"detail": detail}))
    out = tmp_path / "vertex1.mcp"
    assert main(["mcp", "mint", "Vertex 1", "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert captured.err == f"ashigaru: 409: {detail}\n" and captured.out == ""
    assert not out.exists()


def test_mcp_list_never_shows_a_code(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/me", (200, {"name": "Josui Prime", "role": "admin", "key_id": "k1"}))
    desk.on("GET", "/v1/identities", (200, [{"name": "Josui Prime"}, {"name": "Vertex 1"}]))
    desk.on("GET", "/v1/identities/Josui%20Prime/mcp", (200, []))
    desk.on("GET", MCP_OF_VERTEX, (200, [CODE]))
    assert main(["mcp", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["ID", "IDENTITY", "LABEL", "CREATED", "LAST", "USED", "REVOKED"]
    assert [line.split()[:3] for line in lines[1:]] == [["c0de01", "Vertex", "1"]]


def test_mcp_list_for_a_seat_is_its_own(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/me", (200, {"name": "Vertex 1", "role": "seat", "key_id": "k1"}))
    desk.on("GET", MCP_OF_VERTEX, (200, [CODE]))
    assert commands.mcp_list(Desk(config.load())) == [CODE]
    assert not desk.calls("GET", "/v1/identities")


def test_mcp_revoke_finds_the_codes_identity(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", MCP_OF_VERTEX, (200, [CODE]))
    desk.on("DELETE", f"{MCP_OF_VERTEX}/c0de01", (200, CODE | {"revoked_at": "2026-11-18T02:00:00Z"}))
    assert main(["mcp", "revoke", "c0de01", "--identity", "Vertex 1"]) == 0
    assert "2026-11-18T02:00:00Z" in capsys.readouterr().out
    assert main(["mcp", "revoke", "nope", "--identity", "Vertex 1"]) == 1
    assert (
        "no MCP code nope" in capsys.readouterr().err
        and len(desk.calls("DELETE", f"{MCP_OF_VERTEX}/c0de01")) == 1
    )


def test_mcp_mint_revokes_a_url_it_could_not_write(
    desk: FakeDesk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def full(descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    desk.on("POST", MCP_OF_VERTEX, (201, CODE | {"url": URL}))
    desk.on("DELETE", f"{MCP_OF_VERTEX}/c0de01", (200, CODE | {"revoked_at": "2026-11-18T02:00:00Z"}))
    monkeypatch.setattr(commands.os, "fsync", full)
    out = tmp_path / "vertex1.mcp"
    assert main(["mcp", "mint", "Vertex 1", "--out", str(out)]) == 1
    assert "c0de01 was revoked" in capsys.readouterr().err
    assert len(desk.calls("DELETE", f"{MCP_OF_VERTEX}/c0de01")) == 1 and not out.exists()
