"""The ashigaru CLI against a fake Desk (tests/fake_desk.py)."""

import io
import json
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fake_desk import FakeDesk, Seen, serve

from ashigaru import __version__
from ashigaru.cli import commands, config, hooks, render
from ashigaru.cli.__main__ import main
from ashigaru.cli.client import Desk, DeskError, Send, Unreachable
from ashigaru.cli.wait import CIWaiter, PollWaiter
from ashigaru.desk.model import LEASE

KEY = "ash_k1_secret"
USAGE_ERROR = 2
NARROW_TABLE = 40


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


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["--version"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out == f"{__version__}\n"


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


def test_a_stale_answer_outside_a_seat_session_says_so(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    desk.on("GET", "/v1/seats", took_over(None))
    assert main(["agents"]) == 1
    assert "no CLAUDE_CODE_SESSION_ID: not inside the seat's session" in capsys.readouterr().err
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert main(["agents"]) == 1
    assert "no CLAUDE_CODE_SESSION_ID" not in capsys.readouterr().err


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
    assert lines[0].split() == ["SEAT", "STATUS", "WORK", "TIME", "TURN", "SEEN", "TASK"]
    assert lines[1].startswith("Vertex 1") and "a1" in lines[1]


def test_agents_shows_missing_seats(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    never = {"name": "Max 4", "status": "free", "turn": "idle", "updated_at": None, "missing": True}
    desk.on("GET", "/v1/seats", (200, [SEAT | {"updated_at": "2026-11-17T10:00:00Z"}, never]))
    assert main(["agents"]) == 0
    lines = capsys.readouterr().out.splitlines()
    last = lines[-3].split()
    assert last[:3] == ["Max", "4", "missing"] and last[-1] == "never"
    assert lines[-1] == "1 of 2 up"


def test_agents_shows_the_hold(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/seats", (200, [SEAT | {"owner": "Scott", "hold": "busy-manual"}]))
    assert main(["agents"]) == 0
    assert "busy-manual" in capsys.readouterr().out


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


def seat_row(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "status": "free", "turn": "idle", "missing": False} | fields


def round_(**fields: Any) -> dict[str, Any]:
    blocked = {
        "id": "a2",
        "seat": "Max 1",
        "mode": "manual",
        "task": "t",
        "state": "blocked",
        "wrap": None,
        "result_or_blocker": "needs a key",
        "next": None,
    }
    caught: dict[str, Any] = {
        "since": None,
        "after": 40,
        "until": 57,
        "new": [],
        "reopened": [],
        "truncated": False,
    }
    return {
        "items": [blocked],
        "seats": [seat_row("Max 1"), seat_row("Max 2", missing=True)],
        "summary": {"up": 1, "seats": 2, "open": 1},
        "queue": {"queued": [item(id="a3", lane="vertex", waits_on=[])], "proposed": [], "truncated": False},
        "catch_up": caught | {"advanced": False},
    } | fields


def caught_row(id: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": id,
        "ref": f"https://github.com/crunchtools/x/issues/{id}",
        "title": f"title of {id}",
        "lane": "vertex",
        "held": False,
        "filed_by": "fatherlinux",
        "state": "queued",
    } | fields


def test_work_renders_the_whole_round(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    caught = round_()["catch_up"] | {"new": [caught_row("a8"), caught_row("a9", held=True, lane=None)]}
    desk.on("GET", "/v1/work", (200, round_(catch_up=caught)))
    assert main(["work", "--all"]) == 0
    out = capsys.readouterr().out
    assert "needs a key" in out and "Max 2" in out and "missing" in out
    assert "1 of 2 up, 1 open" in out
    assert "a3" in out and "vertex" in out
    assert "NEW" in out and "title of a8" in out and "fatherlinux" in out
    assert "held" in out and "events 40 to 57" in out
    assert "REOPENED" not in out


def test_the_round_shows_the_id_first_and_the_mode_in_its_own_column(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    served = round_()["items"][0] | {"outcome_label": "RT#1652", "step": "blocked: needs a key"}
    loose = served | {"id": "a4", "mode": "auto", "state": "review", "outcome_label": None, "step": None}
    desk.on("GET", "/v1/work", (200, round_(items=[served, loose])))
    assert main(["work", "--all"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["WORK", "TIME", "SEAT", "MODE", "STATE", "OUTCOME", "TASK", "NEXT"]
    assert "manual  blocked" in lines[1] and "RT#1652" in lines[1] and "blocked: needs a key" in lines[1]
    assert lines[2].split()[3:6] == ["auto", "review", "-"], "no outcome shows -"


STEP = "blocked: Dolza 2's review wants the lease renewer to log and stop, see r4630065164"
WIDE = 200
LONG_TASK = (
    "Move the check round server-side: the Desk keeps its own status from hooks and webhooks, "
    "with timers to reconcile what they miss, and Prime's round only reads it"
)


"""The header and the task's lines: narrower terminals give the task more of them."""


@pytest.mark.parametrize("columns", [120, WIDE])
def test_work_keeps_every_row_on_one_line(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, columns: int
) -> None:
    monkeypatch.setenv("COLUMNS", str(columns))
    row = round_()["items"][0] | {
        "id": "a686", "seat": "Exedore 3 - Max", "task": LONG_TASK,
        "outcome_label": "crunchtools/trentina#842",
        "step": STEP,
    }  # fmt: skip
    desk.on("GET", "/v1/work", (200, round_(items=[row])))
    assert main(["work", "--all"]) == 0
    lines = capsys.readouterr().out.split("\n\n")[0].splitlines()
    header, first = lines
    task_at, next_at = header.index("TASK"), header.index("NEXT")
    assert all(len(line) <= columns for line in lines)
    assert first[next_at:].startswith("blocked:"), "NEXT under its header"
    assert (first[next_at:] == STEP) is (columns == WIDE), "NEXT whole when there is room, else clipped"
    task = first[task_at:next_at].strip()
    assert LONG_TASK.startswith(task.removesuffix("…")) and task.endswith("…"), "the task is clipped"


def test_assign_sets_an_items_outcome_before_assigning_it(desk: FakeDesk) -> None:
    desk.on("PATCH", "/v1/items/a1", (200, item(parent="a9")))
    desk.on("POST", "/v1/items/a1/assign", (200, item(seat="Vertex 1", state="assigned")))
    assert main(["assign", "a1", "--seat", "Vertex 1", "--outcome", "board", "--outcome-text", "do it"]) == 0
    patch, assigned = desk.seen
    assert patch.body == {"outcome": "board", "outcome_text": "do it"}
    assert assigned.body == {"seat": "Vertex 1"}


def test_assign_passes_an_empty_outcome_text_on(desk: FakeDesk) -> None:
    desk.on("PATCH", "/v1/items/a1", (200, item()))
    desk.on("POST", "/v1/items/a1/assign", (200, item(seat="Vertex 1", state="assigned")))
    assert main(["assign", "a1", "--seat", "Vertex 1", "--outcome-text", ""]) == 0
    patch, _ = desk.seen
    assert patch.body == {"outcome_text": ""}


def test_work_shows_what_reopened(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    caught = round_()["catch_up"] | {"reopened": [caught_row("a7", state="done")]}
    desk.on("GET", "/v1/work", (200, round_(catch_up=caught)))
    assert main(["work", "--all"]) == 0
    out = capsys.readouterr().out
    assert "REOPENED" in out and "title of a7" in out


def test_work_all_and_since_are_sent(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/work", (200, round_()))
    assert main(["work", "--since", "2026-11-01T00:00:00Z"]) == 0
    assert main(["work", "--all", "--since", "2026-11-01T00:00:00Z"]) == 0
    first, second = desk.calls("GET", "/v1/work")
    assert first.query == {"since": ["2026-11-01T00:00:00Z"]}
    assert second.query == {"all": ["true"], "since": ["2026-11-01T00:00:00Z"]}


def test_work_advance_posts(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    moved = round_()
    moved["catch_up"] = moved["catch_up"] | {"advanced": True}
    desk.on("POST", "/v1/work", (200, moved))
    assert main(["work", "--advance", "--all"]) == 0
    assert [s.method for s in desk.seen] == ["POST"]
    assert desk.seen[0].query == {"all": ["true"]}
    assert "sweep mark moved" in capsys.readouterr().out


def test_work_refuses_since_with_advance(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["work", "--advance", "--since", "2026-11-01T00:00:00Z"]) == 1
    assert desk.seen == []
    assert "--since cannot go with --advance" in capsys.readouterr().err


def test_work_says_when_a_list_was_cut(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    cut = round_(truncated=True)
    cut["catch_up"] = cut["catch_up"] | {"truncated": True}
    desk.on("GET", "/v1/work", (200, cut))
    assert main(["work", "--all"]) == 0
    out = capsys.readouterr().out
    assert "the first 1000" in out and "the next --advance reads on" in out


def test_work_json_prints_the_whole_round(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/work", (200, round_()))
    assert main(["work", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == round_()


def test_queue_shows_what_each_item_waits_on(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    queued = item(
        id="a3", lane="vertex", waits_on=[{"id": "a1", "state": "working"}, {"id": "a9", "state": "unknown"}]
    )
    proposed = item(
        id="a4", state="proposed", parent="a0", filed_by="Max 1", expires_at="2026-12-01T00:00:00Z"
    )
    desk.on("GET", "/v1/queue", (200, {"queued": [queued], "proposed": [proposed]}))
    assert main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "a1 working, a9 unknown" in out and "2026-12-01" in out


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


def test_assign_records_a_new_item_and_assigns_it_in_one_request(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items", (201, item(id="a7", seat="Max 3", state="assigned")))
    argv = ["assign", "--task", "ship it", "--lane", "exedore-max", "--after", "a5", "--ref", "RT#9"]
    assert main(argv) == 0
    assert [(s.method, s.path) for s in desk.seen] == [("POST", "/v1/items")]
    assert desk.seen[0].body == {
        "task": "ship it",
        "lane": "exedore-max",
        "after": ["a5"],
        "ref": "RT#9",
        "assign": {"lane": "exedore-max"},
    }


def test_assign_task_by_seat_names_the_seat_in_the_create(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items", (201, item(id="a7", seat="Vertex 1", state="assigned")))
    assert main(["assign", "--task", "t", "--seat", "Vertex 1"]) == 0
    assert desk.seen[0].body == {"task": "t", "assign": {"seat": "Vertex 1"}}


def test_assign_needs_an_item(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["assign"]) == 1
    assert "name an item" in capsys.readouterr().err
    assert desk.seen == []


def test_claim_posts_an_empty_body(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items/a1/claim", (200, item(seat="Max 1", state="assigned")))
    assert main(["claim", "a1"]) == 0
    assert desk.seen[0].body == {}
    assert "Max 1" in capsys.readouterr().out


def test_claim_refused_exits_1(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items/a1/claim", (409, {"detail": "a1 is not queued"}))
    assert main(["claim", "a1"]) == 1
    assert "not queued" in capsys.readouterr().err


def test_file_sends_the_fields_given(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items/a1/file", (201, item(id="a9", state="proposed")))
    assert main(["file", "a1", "--task", "split it", "--done-when", "tests pass", "--note", "n"]) == 0
    assert main(["file", "a1", "--task", "bare"]) == 0
    full, bare = desk.seen
    assert full.body == {"task": "split it", "done_when": "tests pass", "note": "n"}
    assert bare.body == {"task": "bare"}


def test_file_needs_a_task(desk: FakeDesk) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["file", "a1"])
    assert stop.value.code == USAGE_ERROR


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_auto_and_manual(desk: FakeDesk, mode: str) -> None:
    desk.on("PATCH", "/v1/items/a1", (200, item(mode=mode)))
    assert main([mode, "a1"]) == 0
    assert desk.seen[0].body == {"mode": mode}


def test_wrap_asks_the_desk_to_clear_and_close(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items/a1/wrap", (200, item(state="done", wrap="cleared")))
    assert main(["wrap", "a1", "--learned", "poll less"]) == 0
    assert main(["wrap", "a1", "--state", "wontdo", "--force"]) == 0
    first, second = desk.calls("POST", "/v1/items/a1/wrap")
    assert first.body == {"stage": "cleared", "learned": "poll less", "state": "done", "force": False}
    assert second.body == {"stage": "cleared", "state": "wontdo", "force": True}


def test_wrap_marks_wrapping(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items/a1/wrap", (200, item(state="review", wrap="wrapping")))
    assert main(["wrap", "a1", "--stage", "wrapping"]) == 0
    assert desk.seen[0].body["stage"] == "wrapping"


def test_wrap_says_why_the_desk_refused(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items/a1/wrap", (409, {"detail": "a1: no CC-DONE yet; send /cc seat first"}))
    assert main(["wrap", "a1"]) == 1
    assert "/cc seat" in capsys.readouterr().err


# Items


def test_create_queues_without_assigning(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items", (201, item(id="a7", subject_key="crunchtools/ashigaru#5")))
    argv = [
        "create",
        "--task",
        "t",
        "--subject-key",
        "crunchtools/ashigaru#5",
        "--after",
        "a1",
        "--priority",
        "2",
    ]
    assert main(argv) == 0
    assert desk.seen[0].body == {
        "task": "t",
        "subject_key": "crunchtools/ashigaru#5",
        "after": ["a1"],
        "priority": 2,
    }
    assert capsys.readouterr().out.splitlines()[1].startswith("a7")
    assert desk.calls("POST", "/v1/items/a7/assign") == []


def test_create_prints_the_item_holding_a_subject_key(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("POST", "/v1/items", (409, {"detail": "subject_key RT#9 is taken by a3"}))
    desk.on("GET", "/v1/items", (200, [item(id="a3", subject_key="RT#9")]))
    assert main(["create", "--task", "t", "--subject-key", "RT#9", "--json"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["id"] == "a3" and "a3 exists" in out.err
    assert desk.calls("GET", "/v1/items")[0].query == {"subject_key": ["RT#9"], "limit": ["1"]}


def test_create_prints_the_item_holding_an_id(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items", (409, {"detail": "id a3 is taken"}))
    desk.on("GET", "/v1/items/a3", (200, item(id="a3")))
    assert main(["create", "--task", "t", "--id", "a3"]) == 1
    assert capsys.readouterr().out.splitlines()[1].startswith("a3")


def test_create_looks_up_a_taken_id_before_the_subject_key(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("POST", "/v1/items", (409, {"detail": "id a3 is taken"}))
    desk.on("GET", "/v1/items/a3", (200, item(id="a3")))
    desk.on("GET", "/v1/items", (200, [item(id="a5", subject_key="RT#9")]))
    assert main(["create", "--task", "t", "--id", "a3", "--subject-key", "RT#9"]) == 1
    assert capsys.readouterr().out.splitlines()[1].startswith("a3")
    desk.on("GET", "/v1/items/a3", (404, {"detail": "no such item"}))
    assert main(["create", "--task", "t", "--id", "a3", "--subject-key", "RT#9"]) == 1
    assert capsys.readouterr().out.splitlines()[1].startswith("a5")


def test_assign_task_refused_records_nothing_and_says_why(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("POST", "/v1/items", (409, {"detail": "no free seat in lane vertex"}))
    assert main(["assign", "--task", "t", "--lane", "vertex"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "no free seat in lane vertex" in out.err
    assert [(s.method, s.path) for s in desk.seen] == [("POST", "/v1/items")]


def test_assign_task_does_not_hide_a_desk_failure(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items", (500, {"detail": "boom"}))
    assert main(["assign", "--task", "t", "--lane", "vertex"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "500: boom" in out.err


def test_get_by_id_subject_key_or_ref(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/items/a1", (200, item()))
    desk.on("GET", "/v1/items", lambda seen: (200, [item(id="a2")] if "ref" in seen.query else []))
    assert main(["get", "a1"]) == 0
    assert main(["get", "--ref", "https://rt.example/9", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 2)[2])["id"] == "a2"
    assert main(["get", "--subject-key", "RT#404"]) == 1
    assert "no item with subject_key RT#404" in capsys.readouterr().err
    assert main(["get"]) == 1


def test_list_takes_several_states_and_the_learned_filters(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on(
        "GET", "/v1/items", (200, [item(state="done", learned="x", blocked_reason=None, result="shipped")])
    )
    argv = ["list", "--state", "done", "--state", "blocked", "--learned", "--no-memorized", "--limit", "5"]
    assert main(argv) == 0
    assert desk.seen[0].query == {
        "state": ["done", "blocked"], "learned": ["true"], "memorized": ["false"], "limit": ["5"]
    }  # fmt: skip
    assert "shipped" in capsys.readouterr().out


def test_update_sets_and_clears(desk: FakeDesk) -> None:
    desk.on("PATCH", "/v1/items/a1", (200, item(state="review")))
    argv = ["update", "a1", "--state", "review", "--check-after", "2026-11-19T00:00:00Z", "--memorized"]
    assert main([*argv, "--clear", "next", "--filed-by", "Max 1"]) == 0
    assert desk.seen[0].body == {
        "state": "review", "check_after": "2026-11-19T00:00:00Z", "memorized": True, "filed_by": "Max 1",
        "next": None,
    }  # fmt: skip
    assert main(["update", "a1", "--clear", "after"]) == 0
    assert desk.seen[-1].body == {"after": None}
    assert main(["update", "a1"]) == 1


def test_mark_reads_and_sets(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    mark = {"name": "sweep", "value": "2026-11-18T06:00:00Z", "updated_at": "x", "updated_by": "Josui Prime"}
    desk.on("GET", "/v1/marks/sweep", (200, mark))
    desk.on("PUT", "/v1/marks/sweep", (200, mark))
    assert main(["mark", "sweep"]) == 0
    assert "2026-11-18T06:00:00Z" in capsys.readouterr().out
    assert main(["mark", "sweep", "--set", "2026-11-18T06:00:00Z"]) == 0
    assert desk.calls("PUT", "/v1/marks/sweep")[0].body == {"value": "2026-11-18T06:00:00Z"}


def test_report(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/items/a1/report", (200, item(state="review")))
    assert main(["report", "a1", "done", "--text", "PR #5", "--learned", "none"]) == 0
    assert desk.seen[0].body == {"verb": "DONE", "text": "PR #5", "learned": "none"}
    assert main(["report", "a1", "cc-done"]) == 0
    assert desk.seen[1].body == {"verb": "CC-DONE"}


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


SHA = "0123456789abcdef0123456789abcdef01234567"


def ci(status: str, **extra: Any) -> dict[str, Any]:
    return {"repo": "crunchtools/ashigaru", "sha": SHA, "status": status, "checks": [], "failing": []} | extra


def test_wait_on_a_commit_long_polls_until_it_is_not_pending(desk: FakeDesk) -> None:
    replies = [(200, ci("pending")), (502, {"detail": "proxy"}), (200, ci("success"))]
    answers = iter(replies)
    desk.on("GET", "/v1/ci/wait", lambda seen: next(answers))
    clock = Clock()
    waiter = CIWaiter(Desk(config.load()), sleep=clock.sleep, clock=lambda: clock.now)
    assert waiter.wait("crunchtools/ashigaru", SHA, None)["status"] == "success"
    queries = [seen.query for seen in desk.calls("GET", "/v1/ci/wait")]
    assert queries[0] == {"repo": ["crunchtools/ashigaru"], "sha": [SHA], "timeout": ["90"]}
    assert len(queries) == len(replies) and clock.slept == [5.0]


def test_wait_on_a_commit_rides_out_no_answer() -> None:
    class Flaky:
        def __init__(self) -> None:
            self.answers: list[Any] = [Unreachable("connect"), ci("failure")]

        def request(self, *args: Any, **kwargs: Any) -> Any:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    clock = Clock()
    waiter = CIWaiter(cast("Desk", Flaky()), sleep=clock.sleep, clock=lambda: clock.now)
    assert waiter.wait("crunchtools/ashigaru", SHA, None)["status"] == "failure"
    assert clock.slept == [5.0]


def test_a_long_poll_outlasts_the_clients_timeout(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/ci/wait", (200, ci("success")))
    desk.delay = 0.3
    client = Desk(config.load(), timeout=0.1)
    with pytest.raises(Unreachable):
        client.request("GET", "/v1/ci/wait")
    assert client.request("GET", "/v1/ci/wait", send=Send(timeout=2.0))["status"] == "success"


def test_wait_on_a_commit_pauses_no_longer_than_its_deadline(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/ci/wait", (503, {"detail": "busy"}))
    clock = Clock()
    waiter = CIWaiter(Desk(config.load()), pause=5.0, sleep=clock.sleep, clock=lambda: clock.now)
    with pytest.raises(TimeoutError):
        waiter.wait("crunchtools/ashigaru", SHA, 3)
    assert clock.slept == [3.0] and len(desk.calls("GET", "/v1/ci/wait")) == 1


def test_wait_on_a_commit_stops_on_a_refusal(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/ci/wait", (422, {"detail": "sha must be the full commit id"}))
    clock = Clock()
    waiter = CIWaiter(Desk(config.load()), sleep=clock.sleep, clock=lambda: clock.now)
    with pytest.raises(DeskError, match="422"):
        waiter.wait("crunchtools/ashigaru", "abc", None)
    assert clock.slept == [] and len(desk.calls("GET", "/v1/ci/wait")) == 1


def test_wait_on_a_commit_gives_up_at_its_timeout(desk: FakeDesk) -> None:
    clock = Clock()

    def held(seen: Seen) -> tuple[int, Any]:
        clock.now += int(seen.query["timeout"][0])  # the Desk holds the request that long
        return 200, ci("pending")

    desk.on("GET", "/v1/ci/wait", held)
    waiter = CIWaiter(Desk(config.load()), sleep=clock.sleep, clock=lambda: clock.now)
    with pytest.raises(TimeoutError, match="pending: no check has reported"):
        waiter.wait("crunchtools/ashigaru", SHA, 30)
    assert [seen.query["timeout"] for seen in desk.calls("GET", "/v1/ci/wait")] == [["30"]]


def test_wait_on_a_commit_names_the_required_checks_still_pending_at_its_timeout(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    """memory#45: Gatehouse passed, but the branch's required "sdlc ci" never started."""
    gatehouse = {"name": "Gatehouse", "verdict": "success", "url": None, "updated_at": "2026-11-18T00:00:00Z"}
    held = ci("pending", checks=[gatehouse], pending=["sdlc ci"], branch="main", required=["sdlc ci"])
    desk.on("GET", "/v1/ci/wait", (200, held))
    assert main(["wait", "crunchtools/memory", SHA, "--branch", "main", "--timeout", "0"]) == 1
    captured = capsys.readouterr()
    assert "success" not in captured.out + captured.err
    assert captured.err.strip().endswith("pending: sdlc ci")
    assert desk.calls("GET", "/v1/ci/wait")[0].query["branch"] == ["main"]


def test_wait_shows_what_a_commit_still_waits_on(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    unread = "the required checks could not be read (HTTPError)"
    desk.on("GET", "/v1/ci/wait", (200, ci("failure", pending=["Container"], unverified=unread)))
    assert main(["wait", "crunchtools/ashigaru", SHA]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[1:3] == ["pending: Container", unread]


def test_wait_takes_a_branch_only_for_a_commit(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["wait", "a1", "--branch", "main"]) == 1
    assert "--branch is for a commit" in capsys.readouterr().err and not desk.seen


@pytest.mark.parametrize(("status", "code"), [("success", 0), ("failure", 1)])
def test_wait_on_a_commit_exit_follows_the_verdict(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str], status: str, code: int
) -> None:
    check = {"name": "sdlc ci", "verdict": status, "url": None, "updated_at": "2026-11-18T00:00:00Z"}
    desk.on("GET", "/v1/ci/wait", (200, ci(status, checks=[check])))
    assert main(["wait", "crunchtools/ashigaru", SHA]) == code
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"crunchtools/ashigaru@{SHA[:12]}  {status}"
    assert out[2].split() == ["CHECK", "VERDICT", "URL"] and out[3].split() == ["sdlc", "ci", status]


def test_wait_on_a_commit_says_when_the_desk_has_no_ci_wait(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["wait", "crunchtools/ashigaru", SHA]) == 1
    assert "serves no CI wait" in capsys.readouterr().err
    assert main(["wait", "a", "b", "c"]) == 1


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


SESSION = "/v1/seats/Max%201/session"


def test_session_start_registers_the_session_and_keeps_nothing(seat: FakeDesk) -> None:
    seat.on("POST", SESSION, (200, {"epoch": 4, "seat": SEAT}))
    assert hook("session-start", {"session_id": "s1", "source": "clear"}) == 0
    (call,) = seat.calls("POST", SESSION)
    assert call.body == {"session_id": "s1", "source": "clear"} and call.headers["X-Ashigaru-Session"] == "s1"
    assert patches(seat) == []
    hook("stop", {})
    assert "X-Ashigaru-Epoch" not in seat.calls("PATCH", "/v1/seats/Max%201")[0].headers
    assert not (Path.home() / ".local").exists()


@pytest.mark.parametrize("missing", [404, 405])
def test_session_start_falls_back_to_the_patch_on_an_old_desk(seat: FakeDesk, missing: int) -> None:
    seat.on("POST", SESSION, (missing, {"detail": "nope"}))
    assert hook("session-start", {"session_id": "s1", "source": "clear"}) == 0
    assert patches(seat) == [{"turn": "idle", "task": None}]


def test_a_hook_names_its_session_and_without_one_the_seats_env_session(
    seat: FakeDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook("stop", {"session_id": "s1"})
    hook("stop", {})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env1")
    hook("stop", {})
    sent = [s.headers.get("X-Ashigaru-Session") for s in seat.calls("PATCH", "/v1/seats/Max%201")]
    assert sent == ["s1", None, "env1"]


def test_a_hooks_event_session_wins_over_the_env_session(
    seat: FakeDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env1")
    hook("stop", {"session_id": "s1"})
    assert seat.calls("PATCH", "/v1/seats/Max%201")[0].headers["X-Ashigaru-Session"] == "s1"


def test_session_start_does_not_register_a_compact(seat: FakeDesk) -> None:
    seat.on("POST", SESSION, (200, {"epoch": 4, "seat": SEAT}))
    hook("session-start", {"session_id": "s1", "source": "compact"})
    assert not seat.seen


def test_session_start_tells_the_model_a_resumed_session_is_stale(
    seat: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    seat.on("POST", SESSION, (409, {"detail": "its lease lapsed", "code": "stale-session"}))
    assert hook("session-start", {"session_id": "s1", "source": "resume"}) == 0
    out = capsys.readouterr().out
    assert out.startswith("Ashigaru Desk: this session is stale: its lease lapsed.") and "/clear" in out
    assert patches(seat) == []


def test_every_typed_prompt_is_sent_as_the_task_and_the_desk_keeps_the_first(
    seat: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    hook("prompt-submit", {"session_id": "s1", "prompt": "<cross-session-message from=x>do a1"})
    hook("prompt-submit", {"session_id": "s1", "prompt": "  fix the build\n"})
    hook("prompt-submit", {"session_id": "s1", "prompt": "and the docs"})
    hook("prompt-submit", {"session_id": "s2", "prompt": "x" * 300})
    assert patches(seat) == [
        {"turn": "active"},
        {"turn": "active", "task": "fix the build"},
        {"turn": "active", "task": "and the docs"},
        {"turn": "active", "task": "x" * 120},
    ]
    assert capsys.readouterr().out == ""


def fire(prompt: str) -> str:
    """The system entry Claude Code appends as a scheduled task fires: its copy of the prompt has
    its whitespace collapsed and is cut at 200 characters."""
    copy = " ".join(prompt.split())[: hooks.SCHEDULED_PROMPT_LENGTH]
    return json.dumps({"type": "system", "subtype": hooks.SCHEDULED_FIRE, "prompt": copy}) + "\n"


def test_a_scheduled_prompt_is_not_the_task(seat: FakeDesk, tmp_path: Path) -> None:
    metsuke = "Metsuke report run.\nReport: daily-briefing. run_id: r1"
    long = "Metsuke report run. " + "y" * 300
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(entry({"type": "text", "text": "hi"}) + fire(metsuke) + fire(long))
    for prompt in (metsuke, long):
        hook("prompt-submit", {"session_id": "s1", "prompt": prompt, "transcript_path": str(transcript)})
    hook(
        "prompt-submit", {"session_id": "s1", "prompt": "Metsuke report", "transcript_path": str(transcript)}
    )
    assert patches(seat) == [
        {"turn": "active"},
        {"turn": "active"},
        {"turn": "active", "task": "Metsuke report"},
    ]


def test_a_prompt_fired_in_an_earlier_turn_and_typed_again_is_the_task(
    seat: FakeDesk, tmp_path: Path
) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(fire("status") + entry({"type": "text", "text": "done"}))
    hook("prompt-submit", {"session_id": "s1", "prompt": "status", "transcript_path": str(transcript)})
    assert patches(seat) == [{"turn": "active", "task": "status"}]


def test_a_scheduled_prompt_is_found_in_the_tail_of_a_long_transcript(seat: FakeDesk, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x" * hooks.WAITING_TAIL + "\n" + fire("run the report"))
    hook(
        "prompt-submit", {"session_id": "s1", "prompt": "run the report", "transcript_path": str(transcript)}
    )
    assert patches(seat) == [{"turn": "active"}]


def test_a_missing_transcript_leaves_a_typed_prompt_the_task(seat: FakeDesk, tmp_path: Path) -> None:
    hook("prompt-submit", {"session_id": "s1", "prompt": "go", "transcript_path": str(tmp_path / "gone")})
    assert patches(seat) == [{"turn": "active", "task": "go"}]


def test_a_typed_prompt_after_a_clear_is_sent_as_the_task(seat: FakeDesk) -> None:
    hook("prompt-submit", {"session_id": "s1", "prompt": "first"})
    hook("session-start", {"session_id": "s1", "source": "clear"})
    hook("prompt-submit", {"session_id": "s1", "prompt": "second"})
    assert patches(seat)[-1] == {"turn": "active", "task": "second"}


def test_stop(seat: FakeDesk) -> None:
    assert hook("stop", {"hook_event_name": "Stop"}) == 0
    assert patches(seat) == [{"turn": "idle"}]


def entry(*blocks: dict[str, Any]) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}}) + "\n"


def text(words: str) -> dict[str, Any]:
    return {"type": "text", "text": words}


def test_stop_reports_the_final_message_as_waiting_on(seat: FakeDesk) -> None:
    hook("stop", {"hook_event_name": "Stop", "last_assistant_message": "  Merge it?\n"})
    hook("stop", {"last_assistant_message": "ok"})
    assert patches(seat) == [
        {"turn": "idle", "waiting_on": "Merge it?"},
        {"turn": "idle", "waiting_on": "ok"},
    ]


def test_stop_falls_back_to_the_last_text_in_the_transcript(seat: FakeDesk, tmp_path: Path) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        entry(text("old"))
        + "not json\n"
        + entry(text("Ship it?"), {"type": "tool_use", "name": "Bash"}, text("Yes or no?"))
        + entry({"type": "tool_use", "name": "Bash"})
        + entry({"type": "thinking", "thinking": "hm"})
    )
    hook("stop", {"hook_event_name": "Stop", "transcript_path": str(transcript)})
    assert patches(seat) == [{"turn": "idle", "waiting_on": "Ship it?\n\nYes or no?"}]


def test_stop_reads_only_the_tail_of_a_transcript(seat: FakeDesk, tmp_path: Path) -> None:
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(entry(text("first")) + entry(text("x" * hooks.WAITING_TAIL)))
    hook("stop", {"transcript_path": str(transcript)})
    assert patches(seat) == [{"turn": "idle"}]


def test_stop_keeps_the_end_of_a_long_message(seat: FakeDesk) -> None:
    hook("stop", {"hook_event_name": "Stop", "last_assistant_message": "a" * 600 + "\nQuestion?"})
    waiting = patches(seat)[0]["waiting_on"]
    assert waiting == "…" + "a" * 489 + "\nQuestion?"
    assert len(waiting) == hooks.WAITING_LENGTH


@pytest.mark.parametrize("kind", ["StopFailure", "Notification"])
def test_stop_failure_and_idle_prompt_send_no_waiting_on(seat: FakeDesk, kind: str) -> None:
    seat.on(
        "GET",
        "/v1/seats/Max%201",
        (200, SEAT | {"turn": "active", "turn_started_at": "2020-01-01T00:00:00Z"}),
    )
    hook("stop", {"hook_event_name": kind, "last_assistant_message": "Merge it?"})
    assert patches(seat) == [{"turn": "idle"}]


def test_stop_without_a_transcript_sends_only_idle(seat: FakeDesk, tmp_path: Path) -> None:
    hook("stop", {"hook_event_name": "Stop", "transcript_path": str(tmp_path / "gone.jsonl")})
    assert patches(seat) == [{"turn": "idle"}]


def test_stop_still_goes_idle_when_the_transcript_cannot_be_read(
    seat: FakeDesk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook("stop", {"hook_event_name": "Stop", "transcript_path": str(tmp_path)})
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(entry(text("unread")))

    def refused(path: str) -> str:
        raise PermissionError(path)

    monkeypatch.setattr(hooks, "_last_message", refused)
    hook("stop", {"hook_event_name": "Stop", "transcript_path": str(transcript)})
    assert patches(seat) == [{"turn": "idle"}, {"turn": "idle"}]


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


def test_a_stop_the_desk_ended_tells_the_model_and_wakes_it(
    seat: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    seat.on("PATCH", "/v1/seats/Max%201", took_over(None))
    assert hook("stop", {"hook_event_name": "Stop", "session_id": "s1"}) == hooks.WAKE
    out = capsys.readouterr()
    assert out.out == "" and "this session is stale" in out.err and "/clear" in out.err


@pytest.mark.parametrize(
    ("event", "answer"),
    [
        ({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": True}, None),
        ({"hook_event_name": "Stop", "session_id": "s1"}, "s2"),
        ({"hook_event_name": "Notification", "session_id": "s1"}, None),
    ],
    ids=["the stop the notice caused", "a newer session holds the seat", "a notification"],
)
def test_a_stale_stop_is_not_told_twice_or_to_a_session_that_was_superseded(
    seat: FakeDesk, capsys: pytest.CaptureFixture[str], event: dict[str, Any], answer: str | None
) -> None:
    seat.on("PATCH", "/v1/seats/Max%201", took_over(answer))
    seat.on(
        "GET",
        "/v1/seats/Max%201",
        (200, SEAT | {"turn": "active", "turn_started_at": "2000-01-01T00:00:00Z"}),
    )
    assert hook("stop", event) == 0
    assert "this session is stale" not in capsys.readouterr().err
    assert len(patches(seat)) == 1


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


NEXT = "/v1/seats/Max%201/next"


@pytest.fixture
def waking(seat: FakeDesk, monkeypatch: pytest.MonkeyPatch) -> FakeDesk:
    monkeypatch.setattr(hooks, "NEXT_SETTLE", 0)
    monkeypatch.setattr(hooks, "NEXT_RETRY", 0)
    monkeypatch.setattr(hooks, "NEXT_PAUSE", 0)
    return seat


def test_next_wakes_the_seat_with_the_brief_on_stderr(
    waking: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([(204, None), (204, None), (200, {"item": {"id": "a7"}, "brief": "Desk assignment a7"})])
    waking.on("POST", NEXT, lambda seen: next(answers))
    assert hook("next", {"hook_event_name": "SessionStart", "source": "clear"}) == hooks.WAKE
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() == "Desk assignment a7"
    asks = waking.calls("POST", NEXT)
    assert len(asks) == len(["204", "204", "200"]) and asks[0].query == {"wait": [str(hooks.NEXT_WAIT)]}


@pytest.mark.parametrize(
    "refused",
    [(401, None), (403, None), (404, None), (409, None), (409, "unseen"), (409, "no-lane")],
)
def test_next_keeps_checking_in_while_the_seat_cannot_take_work(
    waking: FakeDesk,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    refused: tuple[int, str | None],
) -> None:
    """a831: its asks are all that keeps an idle seat's lease, so a seat outside autodispatch (no lane,
    the switch off) that stopped asking went missing while up."""
    pauses: list[float] = []
    monkeypatch.setattr(hooks.time, "sleep", pauses.append)
    status, code = refused
    no = (status, {"detail": "seat 'Max 1' has no lane", "code": code})
    answers = iter([no, no, (200, {"brief": "Desk assignment a7"})])
    waking.on("POST", NEXT, lambda seen: next(answers))
    assert hook("next", {}) == hooks.WAKE
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() == "Desk assignment a7"
    assert len(waking.calls("POST", NEXT)) == len(["refused", "refused", "brief"])
    assert pauses.count(hooks.NEXT_CHECK_IN) == len(["refused", "refused"])


def test_next_checks_in_well_inside_the_idle_lease() -> None:
    assert LEASE.total_seconds() / 2 >= hooks.NEXT_CHECK_IN
    assert LEASE.total_seconds() / 2 >= hooks.NEXT_BACKOFF
    assert LEASE.total_seconds() / 2 >= hooks.NEXT_WAIT + hooks.NEXT_GRACE


def test_next_keeps_checking_in_for_a_key_the_desk_refuses(
    desk: FakeDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "NEXT_SETTLE", 0)
    monkeypatch.setattr(hooks, "NEXT_CHECK_IN", 0)
    monkeypatch.delenv("ASHIGARU_SEAT", raising=False)
    answers = iter([(401, {"detail": "revoked"}), (200, {"name": "Max 1", "role": "seat", "key_id": "k"})])
    desk.on("GET", "/v1/me", lambda seen: next(answers))
    desk.on("POST", NEXT, (200, {"brief": "b"}))
    assert hook("next", {}) == hooks.WAKE and len(desk.calls("GET", "/v1/me")) == len(["401", "200"])


@pytest.mark.parametrize("code", ["busy", "held"])
def test_next_asks_again_of_a_seat_that_is_busy_for_now(
    waking: FakeDesk, capsys: pytest.CaptureFixture[str], code: str
) -> None:
    """Race R2: a /clear lands before Prime's wrap, so the seat is still busy with the old item."""
    busy = (409, {"detail": "seat is busy with a3", "code": code})
    answers = iter([busy, busy, (200, {"brief": "Desk assignment a7"})])
    waking.on("POST", NEXT, lambda seen: next(answers))
    assert hook("next", {}) == hooks.WAKE
    assert capsys.readouterr().err.strip() == "Desk assignment a7"
    assert len(waking.calls("POST", NEXT)) == len(["busy", "busy", "brief"])


def stale() -> tuple[int, Any]:
    return 409, {"detail": "the Desk ended it", "code": "stale-session"}


def took_over(by: str | None) -> tuple[int, Any]:
    """The Desk's stale-session answer naming `by` as the seat's session, as dispatch.fence does."""
    return 409, {
        "detail": f"seat 'Max 1' is in session {by} at epoch 8; this request has ended",
        "code": "stale-session",
        "session": by,
    }


def test_next_tells_the_model_its_session_was_ended(
    waking: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    waking.on("POST", NEXT, stale())
    assert hook("next", {}) == hooks.WAKE
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Ashigaru Desk: this session is stale: the Desk")
    assert "Ask for /clear to rejoin." in captured.err and len(waking.calls("POST", NEXT)) == 1


def test_next_stops_silently_when_a_newer_session_took_the_seat(
    waking: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    waking.on("POST", NEXT, took_over("s2"))
    assert hook("next", {"session_id": "s1"}) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "" and len(waking.calls("POST", NEXT)) == 1


@pytest.mark.parametrize("current", [None, "s1"])
def test_next_tells_the_model_when_the_desk_names_no_newer_session(
    waking: FakeDesk, capsys: pytest.CaptureFixture[str], current: str | None
) -> None:
    """None: the Desk ended the session. s1: the seat's current session is this one, so nothing took over."""
    waking.on("POST", NEXT, took_over(current))
    assert hook("next", {"session_id": "s1"}) == hooks.WAKE
    assert capsys.readouterr().err.startswith("Ashigaru Desk: this session is stale:")


@pytest.fixture
def run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    return tmp_path / "run"


def test_next_registers_a_new_session_whose_session_start_ran_out_of_time(
    waking: FakeDesk, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """a1709: the /clear's SessionStart POST gave up at HOOK_SECONDS, so the Desk still named the old
    session and every request of the new one was stale; its next took that for a newer session and
    stopped, and the seat went dark."""
    answers = iter([took_over("s0"), (200, {"brief": "Desk assignment a7"})])
    waking.on("POST", NEXT, lambda seen: next(answers))
    waking.on("POST", SESSION, (200, {"epoch": 25, "seat": SEAT}))
    assert hook("next", {"session_id": "s1", "source": "clear"}) == hooks.WAKE
    assert capsys.readouterr().err.strip() == "Desk assignment a7"
    (call,) = waking.calls("POST", SESSION)
    assert call.body == {"session_id": "s1", "source": "clear", "rejoin": True}
    assert call.headers["X-Ashigaru-Session"] == "s1"
    assert hooks.registered_mark("s1", {"XDG_RUNTIME_DIR": str(run_dir)}).exists()


def test_next_registers_a_lost_session_only_once(waking: FakeDesk, run_dir: Path) -> None:
    waking.on("POST", NEXT, took_over("s0"))
    waking.on("POST", SESSION, (200, {"epoch": 25, "seat": SEAT}))
    assert hook("next", {"session_id": "s1", "source": "startup"}) == 0
    assert len(waking.calls("POST", SESSION)) == 1 and len(waking.calls("POST", NEXT)) == len(
        ["stale", "stale"]
    )


def test_an_older_sessions_next_never_takes_the_seat_back(waking: FakeDesk, run_dir: Path) -> None:
    """The /clear's own session-start registered s2; s1's next, marked by its session-start, stops."""
    waking.on("POST", SESSION, (200, {"epoch": 4, "seat": SEAT}))
    assert hook("session-start", {"session_id": "s1", "source": "clear"}) == 0
    waking.on("POST", NEXT, took_over("s2"))
    assert hook("next", {"session_id": "s1", "source": "clear"}) == 0
    assert len(waking.calls("POST", SESSION)) == 1


def test_a_session_the_desk_answered_never_rejoins(
    waking: FakeDesk, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """a1709 review: s1's SessionStart landed after its hook gave up, so s1 has no mark; its next was
    answered, then /clear made s2 the seat's. s1 must stop, not take the seat back."""
    answers = iter([(204, None), took_over("s2"), took_over("s2")])
    waking.on("POST", NEXT, lambda seen: next(answers))
    assert hook("next", {"session_id": "s1", "source": "startup"}) == 0
    assert not waking.calls("POST", SESSION) and capsys.readouterr() == ("", "")
    assert hooks.registered_mark("s1", {"XDG_RUNTIME_DIR": str(run_dir)}).exists()


def test_a_rejoin_the_desk_refuses_as_superseded_stops_silently(
    waking: FakeDesk, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    waking.on("POST", NEXT, took_over("s2"))
    waking.on("POST", SESSION, took_over("s2"))
    assert hook("next", {"session_id": "s1", "source": "clear"}) == 0
    assert len(waking.calls("POST", SESSION)) == 1 and capsys.readouterr() == ("", "")


def test_a_refused_rejoin_is_an_error_and_is_tried_again_at_the_check_in_cadence(
    waking: FakeDesk, run_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(hooks.time, "sleep", pauses.append)
    asks = iter([took_over("s0"), (200, {"brief": "Desk assignment a7"})])
    waking.on("POST", NEXT, lambda seen: next(asks))
    joins = iter([(403, {"detail": "not your seat"}), (200, {"epoch": 25, "seat": SEAT})])
    waking.on("POST", SESSION, lambda seen: next(joins))
    assert hook("next", {"session_id": "s1", "source": "clear"}) == hooks.WAKE
    err = capsys.readouterr().err
    assert "ashigaru hook next: error: rejoin refused" in err and err.strip().endswith("Desk assignment a7")
    assert len(waking.calls("POST", SESSION)) == len(["403", "200"]) and hooks.NEXT_CHECK_IN in pauses


def test_a_mark_is_written_and_trusted_only_in_a_private_directory(seat: FakeDesk, run_dir: Path) -> None:
    seat.on("POST", SESSION, (200, {"epoch": 4, "seat": SEAT}))
    (run_dir / "ashigaru").mkdir(parents=True)
    (run_dir / "ashigaru").chmod(0o777)
    hook("session-start", {"session_id": "s1", "source": "clear"})
    env = {"XDG_RUNTIME_DIR": str(run_dir)}
    assert not hooks.registered_mark("s1", env).exists() and not hooks._unmarked("s1", env)


def test_a_mark_that_cannot_be_read_is_not_taken_for_missing(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    assert not hooks._unmarked("s1", {"XDG_RUNTIME_DIR": str(tmp_path / "file")})
    assert hooks._unmarked("s1", {"XDG_RUNTIME_DIR": str(tmp_path / "none")})


@pytest.mark.parametrize("source", ["resume", None])
def test_next_registers_only_a_session_that_started(
    waking: FakeDesk, run_dir: Path, source: str | None
) -> None:
    waking.on("POST", NEXT, took_over("s0"))
    assert hook("next", {"session_id": "s1", "source": source}) == 0
    assert not waking.calls("POST", SESSION)


def test_a_stale_answer_is_superseded_only_when_it_names_another_session() -> None:
    assert hooks._Stale("x", "s2").superseded("s1")
    assert not hooks._Stale("x", "s1").superseded("s1")
    assert not hooks._Stale("x", None).superseded("s1")


def test_a_desk_error_carries_the_session_a_stale_answer_names(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/me", took_over("s2"))
    with pytest.raises(DeskError) as raised:
        Desk(config.load()).get("/v1/me")
    assert (raised.value.code, raised.value.session) == ("stale-session", "s2")


def test_the_cli_names_the_session_it_runs_in_and_never_an_epoch(
    desk: FakeDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk.on("PATCH", "/v1/items/a1", (200, item()))
    assert main(["update", "a1", "--note", "x"]) == 0
    assert "X-Ashigaru-Session" not in desk.seen[0].headers
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env1")
    assert main(["update", "a1", "--note", "x"]) == 0
    assert desk.seen[1].headers["X-Ashigaru-Session"] == "env1"
    assert all("X-Ashigaru-Epoch" not in s.headers for s in desk.seen)


def test_every_hook_and_the_cli_write_nothing_under_home(
    waking: FakeDesk, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    waking.on("POST", SESSION, (200, {"epoch": 4, "seat": SEAT}))
    waking.on("POST", NEXT, (200, {"brief": "Desk assignment a7"}))
    waking.on("POST", "/v1/seats/Max%201/usage", (200, SEAT))
    waking.on("GET", "/v1/seats", (200, []))
    for source in ("startup", "clear", "resume"):
        assert hook("session-start", {"session_id": "s1", "source": source}) == 0
    assert hook("prompt-submit", {"session_id": "s1", "prompt": "fix it"}) == 0
    assert hook("stop", {"session_id": "s1"}) == 0
    assert hook("next", {"session_id": "s1"}) == hooks.WAKE
    assert hook("statusline", statusline()) == 0
    assert main(["agents"]) == 0
    assert len(waking.seen) >= len(["session x3", "prompt", "stop", "next", "usage", "agents"])
    assert list(home.rglob("*")) == []


@pytest.mark.parametrize("passing", [500, 502, 503, 504, 429])
def test_next_rides_out_a_desk_that_is_restarting(waking: FakeDesk, passing: int) -> None:
    answers = iter([(passing, {"detail": "later"}), (passing, {"detail": "later"}), (200, {"brief": "b"})])
    waking.on("POST", NEXT, lambda seen: next(answers))
    assert hook("next", {}) == hooks.WAKE


def test_next_never_wakes_on_an_empty_answer(waking: FakeDesk) -> None:
    given: list[tuple[int, Any]] = [
        (200, None),
        (200, {"brief": ""}),
        (200, {"item": {}}),
        (200, {"brief": "b"}),
    ]
    answers = iter(given)
    waking.on("POST", NEXT, lambda seen: next(answers))
    assert hook("next", {}) == hooks.WAKE and len(waking.calls("POST", NEXT)) == len(given)


def test_next_finds_its_seat_through_a_restart(desk: FakeDesk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "NEXT_SETTLE", 0)
    monkeypatch.setattr(hooks, "NEXT_RETRY", 0)
    monkeypatch.delenv("ASHIGARU_SEAT", raising=False)
    answers = iter([(503, {"detail": "starting"}), (200, {"name": "Max 1", "role": "seat", "key_id": "k"})])
    desk.on("GET", "/v1/me", lambda seen: next(answers))
    desk.on("POST", NEXT, (200, {"brief": "b"}))
    assert hook("next", {}) == hooks.WAKE


def test_next_gives_up_at_its_deadline(waking: FakeDesk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "NEXT_SECONDS", 0.3)
    waking.on("POST", NEXT, (204, None))
    started = time.monotonic()
    assert hook("next", {}) == 0
    assert time.monotonic() - started < 1.0


def test_next_asks_again_after_an_unreachable_desk(waking: FakeDesk, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = iter([hooks.Unreachable("refused"), hooks.Unreachable("refused")])

    def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
        error = next(calls, None)
        if error is not None:
            raise error
        return original(self, *args, **kwargs)

    original = hooks.Desk.request
    monkeypatch.setattr(hooks.Desk, "request", flaky)
    waking.on("POST", NEXT, (200, {"brief": "b"}))
    assert hook("next", {}) == hooks.WAKE and len(waking.calls("POST", NEXT)) == 1


def test_next_asks_only_for_waits_that_end_before_its_deadline(
    waking: FakeDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "NEXT_SECONDS", 3.0)
    monkeypatch.setattr(hooks, "NEXT_GRACE", 1)
    waking.on("POST", NEXT, (204, None))
    assert hook("next", {}) == 0
    asks = [int(seen.query["wait"][0]) for seen in waking.calls("POST", NEXT)]
    assert asks and max(asks) < hooks.NEXT_WAIT and all(ask + 1 <= hooks.NEXT_SECONDS for ask in asks)


def statusline(session: str = "s1", **fields: Any) -> dict[str, Any]:
    return {
        "session_id": session,
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "cost": {"total_cost_usd": 1.25},
        "context_window": {"total_input_tokens": 41000, "context_window_size": 200000},
    } | fields


def usage_posts(desk: FakeDesk) -> list[Any]:
    return [s.body for s in desk.calls("POST", "/v1/seats/Max%201/usage")]


def assistant(message_id: str, **usage: int) -> str:
    return json.dumps({"type": "assistant", "message": {"id": message_id, "usage": usage}}) + "\n"


@pytest.fixture
def statusline_seat(seat: FakeDesk, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDesk:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    seat.on("POST", "/v1/seats/Max%201/usage", (200, SEAT))
    return seat


def test_the_status_line_reports_usage(statusline_seat: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    assert hook("statusline", statusline()) == 0
    assert usage_posts(statusline_seat) == [
        {
            "session_id": "s1",
            "model": "claude-opus-5-5",
            "context_used": 41000,
            "context_window": 200000,
            "cost_usd": 1.25,
        }
    ]
    assert capsys.readouterr() == ("", "")


def test_the_status_line_posts_once_per_interval_per_session(
    statusline_seat: FakeDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(3):
        hook("statusline", statusline())
    hook("statusline", statusline("s2"))
    assert [post["session_id"] for post in usage_posts(statusline_seat)] == ["s1", "s2"]
    monkeypatch.setattr(hooks, "STATUSLINE_EVERY", 0.0)
    hook("statusline", statusline())
    assert [post["session_id"] for post in usage_posts(statusline_seat)] == ["s1", "s2", "s1"]


def test_the_status_line_counts_session_tokens_from_the_transcript(
    statusline_seat: FakeDesk, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hooks, "STATUSLINE_EVERY", 0.0)
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": "hi"}})
        + "\n"
        + assistant("m1", input_tokens=10, cache_read_input_tokens=100, output_tokens=5)
        + assistant("m1", input_tokens=10, cache_read_input_tokens=100, output_tokens=5)
        + "not json\n"
        + "42\n"
    )
    hook("statusline", statusline(transcript_path=str(transcript)))
    with transcript.open("a") as handle:
        handle.write(assistant("m2", input_tokens=1, cache_creation_input_tokens=20, output_tokens=4))
        handle.write('{"type": "assistant", "message": {"id": "m3", "usage": {"input_tokens": 7')
    hook("statusline", statusline(transcript_path=str(transcript)))
    assert [post["session_tokens"] for post in usage_posts(statusline_seat)] == [115, 140]


def test_the_status_line_carries_a_line_across_the_read_limit(
    statusline_seat: FakeDesk, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hooks, "STATUSLINE_EVERY", 0.0)
    first, second = assistant("m1", input_tokens=3), assistant("m2", output_tokens=4)
    monkeypatch.setattr(hooks, "TRANSCRIPT_CHUNK", len(first) + 5)
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(first + second)
    for _ in range(3):
        hook("statusline", statusline(transcript_path=str(transcript)))
    assert [post["session_tokens"] for post in usage_posts(statusline_seat)] == [3, 7, 7]


def test_the_status_line_skips_a_state_directory_another_user_can_write(
    statusline_seat: FakeDesk, tmp_path: Path
) -> None:
    shared = tmp_path / "run" / "ashigaru"
    shared.mkdir(parents=True)
    shared.chmod(0o777)
    hook("statusline", statusline())
    assert usage_posts(statusline_seat) == []
    assert list(shared.iterdir()) == []


def test_the_status_line_is_silent_on_every_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(hooks, "STATUSLINE_EVERY", 0.0)
    for name in ("ASHIGARU_URL", "ASHIGARU_API_KEY", "ASHIGARU_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert hook("statusline", statusline()) == 0
    monkeypatch.setenv("ASHIGARU_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ASHIGARU_API_KEY", KEY)
    monkeypatch.setenv("ASHIGARU_SEAT", "Max 1")
    assert hook("statusline", statusline()) == 0
    assert hook("statusline", {"model": {"id": "x"}}) == 0
    assert hooks.run("statusline", io.StringIO("not json")) == 0
    assert capsys.readouterr() == ("", "")


def test_the_status_line_drops_state_a_day_old(statusline_seat: FakeDesk, tmp_path: Path) -> None:
    stale = hooks.usage_state("gone", {"XDG_RUNTIME_DIR": str(tmp_path / "run")})
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    day_ago = time.time() - hooks.STATE_KEPT.total_seconds() - 60
    os.utime(stale, (day_ago, day_ago))
    hook("statusline", statusline())
    assert not stale.exists()
    assert hooks.usage_state("s1", {"XDG_RUNTIME_DIR": str(tmp_path / "run")}).exists()


def test_a_slow_desk_never_holds_the_status_line(
    statusline_seat: FakeDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "HOOK_SECONDS", 0.3)
    statusline_seat.delay = 2.0
    started = time.monotonic()
    assert hook("statusline", statusline()) == 0
    assert time.monotonic() - started < 1.0


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


def test_mcp_mint_says_when_the_rollback_fails_too(
    desk: FakeDesk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def full(descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    desk.on("POST", MCP_OF_VERTEX, (201, CODE | {"url": URL}))
    monkeypatch.setattr(commands.os, "fsync", full)
    out = tmp_path / "vertex1.mcp"
    assert main(["mcp", "mint", "Vertex 1", "--out", str(out)]) == 1
    assert "ashigaru mcp revoke c0de01" in capsys.readouterr().err and not out.exists()


def test_status_renders_the_board(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    task = "Constitution: fleet rule for build-artifact images, written out in full so nothing clips it"
    busy = {
        "seat": "Exedore 2 - Max", "mode": "auto", "work": "a676", "state": "working", "turn": "active",
        "since": "2026-11-19T10:00:00Z", "active": "2026-11-19T10:01:00Z", "task": task, "missing": False,
    }  # fmt: skip
    board = {
        "busy": [busy],
        "free": ["Dolza 1 - Max", "Dolza 2 - Max", "Dolza 4 - Max", "Dolza 1 - Vertex"],
        "missing": ["Exedore 4 - Max"],
        "summary": {"up": 11, "seats": 12, "open": 9},
        "needs_you": [
            {"why": "waiting", "work": "a351", "seat": "Dolza 2 - Vertex", "mode": "manual",
             "state": "working", "text": "Keep slide 4?"},
            {"why": "blocked", "work": "a191", "seat": None, "mode": "manual", "state": "blocked",
             "text": "which deck is canonical?"},
        ],
    }  # fmt: skip
    desk.on("GET", "/v1/status", (200, board))
    assert main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["OUTCOME", "MODE", "SEAT", "WORK", "TIME", "ACTIVE", "TASK"]
    assert lines[1].endswith(task)
    assert "4 free: Dolza 1/2/4 - Max, Dolza 1 - Vertex" in lines[3]
    assert "1 missing: Exedore 4 - Max" in lines[3] and "11 of 12 up, 9 open" in lines[3]
    assert lines[5] == "NEEDS YOU"
    assert (
        lines[6].split()[:3] == ["a351", "manual", "waiting"]
        and "Dolza 2 - Vertex: Keep slide 4?" in lines[6]
    )
    assert lines[7].split()[:3] == ["a191", "manual", "blocked"]


@pytest.mark.parametrize(
    ("state", "said"),
    [
        ({"configured": False, "every": None, "running": False, "last": None}, "no sweep runs here"),
        ({"configured": True, "every": 600, "running": False, "last": None}, "every 10m; no sweep yet"),
        (
            {
                "configured": True, "every": 600, "running": True, "requested": True,
                "last": {"started_at": "2026-11-19T10:00:00Z", "finished_at": None},
            },
            "run requested; running now; every 10m; sweep running for",
        ),
        (
            {
                "configured": True, "every": 600, "running": False,
                "last": {
                    "started_at": "2026-11-19T10:00:00Z", "finished_at": "2026-11-19T10:00:05Z",
                    "subjects": 31, "new": 2, "closed": 2, "error": "search: HTTP 403",
                },
            },
            "ago: search: HTTP 403 (31 subjects, 2 new, 2 closed)",
        ),
    ],
)  # fmt: skip
def test_sweep_says_whether_it_runs_and_how_the_last_run_went(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str], state: dict[str, Any], said: str
) -> None:
    desk.on("GET", "/v1/sweep", (200, state))
    assert main(["sweep"]) == 0
    assert said in capsys.readouterr().out


def test_sweep_run_posts(desk: FakeDesk) -> None:
    desk.on("POST", "/v1/sweep", (200, {"configured": True, "every": 600, "running": True, "last": None}))
    assert main(["sweep", "--run"]) == 0
    assert [seen.method for seen in desk.seen] == ["POST"]


def changed(**extra: Any) -> dict[str, Any]:
    row = {
        "id": "a685", "kind": "story", "task": "Merge the sweep fix", "state": "done", "mode": "auto",
        "seat": None, "new": False, "fields": ["result", "state"], "by": ["desk"], "writes": 2,
        "at": "2026-11-19T11:02:00Z", "step": "merged at abc123",
    }  # fmt: skip
    return {"since": None, "seen_at": "2026-11-19T10:00:00Z", "after": 4, "until": 9,
            "items": [row | extra], "truncated": False, "acked": False}  # fmt: skip


def test_changes_renders_each_item_and_the_window(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/changes", (200, changed()))
    assert main(["changes", "--since", "2026-11-19T00:00:00Z"]) == 0
    assert desk.seen[0].query == {"since": ["2026-11-19T00:00:00Z"]}
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["WORK", "WHEN", "MODE", "STATE", "BY", "CHANGED", "TASK", "NEXT"]
    assert lines[1].split()[:1] == ["a685"] and "result, state" in lines[1] and "merged at abc123" in lines[1]
    assert lines[2].startswith("1 changed since you looked") and "events 4 to 9" in lines[2]


def test_changes_ack_posts_and_refuses_since(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/changes", (200, changed(new=True) | {"acked": True}))
    assert main(["changes", "--ack"]) == 0
    assert [s.method for s in desk.seen] == ["POST"]
    out = capsys.readouterr().out
    assert "seen mark moved" in out and " new " in out
    assert main(["changes", "--ack", "--since", "2026-11-19T00:00:00Z"]) == 1
    assert "--since cannot go with --ack" in capsys.readouterr().err


def test_status_lists_what_changed_since_you_looked(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    board = {"busy": [], "free": [], "missing": [], "summary": {"up": 0, "seats": 0, "open": 0},
             "needs_you": [], "changes": changed()}  # fmt: skip
    desk.on("GET", "/v1/status", (200, board))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "CHANGED SINCE YOU LOOKED" in out and "a685" in out


def test_a_first_look_renders_as_the_last_day() -> None:
    first = changed() | {"seen_at": None, "items": []}
    assert render.changes(first).startswith("0 changed in the last day")


def test_table_clips_past_a_short_tail_and_never_widens_it() -> None:
    columns = [("NAME", lambda row: row["name"]), ("NEXT", lambda row: render.Tail(row["next"]))]
    out = render.table([{"name": "n" * 50, "next": "soon"}], columns, width=NARROW_TABLE)
    assert all(len(line) <= NARROW_TABLE for line in out.splitlines())
    assert "soon" in out and "…" in out


def board_(**fields: Any) -> dict[str, Any]:
    busy = {
        "seat": "Dolza 1 - Max", "mode": "auto", "work": "a7", "state": "working", "since": None,
        "active": None, "turn": "active", "task": "Retry PyPI timeouts in the pip-audit step",
        "outcome_label": "crunchtools/python-sdlc#66", "pr": "crunchtools/python-sdlc#67",
        "done_when": "PR open", "check_after": None, "step": "Open the PR",
    }  # fmt: skip
    blocked = {
        "why": "blocked", "work": "a9", "seat": None, "mode": "manual", "state": "blocked",
        "text": "Scott files it", "task": "File the RHEL Jira", "outcome_label": "RT#1656",
    }  # fmt: skip
    held = {"why": "held", "work": "a10", "mode": "manual", "state": "queued", "text": "x"}
    changed = {**busy, "id": "a7", "at": "2026-11-19T18:00:00Z", "by": ["Dolza 1 - Max"], "new": False}
    return {
        "busy": [busy], "free": ["Exedore 4 - Max"], "missing": [],
        "summary": {"up": 12, "seats": 12, "open": 3}, "needs_you": [blocked, held],
        "changes": {"items": [changed], "seen_at": None, "after": 0, "until": 9, "truncated": False},
    } | fields  # fmt: skip


def test_bare_work_is_the_seat_board_in_one_call_and_marks_what_changed_seen(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("GET", "/v1/status", (200, board_()))
    desk.on("POST", "/v1/changes", (200, {}))
    assert main(["work"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[1].split() == ["SEAT", "MODE", "STATE", "OUTCOME", "WORK", "TASK", "NEXT"]
    assert "python-sdlc#66" in out and "crunchtools/python-sdlc#66" not in out, "the owner is dropped"
    assert "NEEDS YOU" in out and "Scott files it" in out
    assert "1 PRs and issues wait for a lane label" in out, "held subjects fold into one line"
    assert "CHANGED SINCE YOU LOOKED" in out
    assert not desk.calls("GET", "/v1/work"), "one call"
    assert len(desk.calls("POST", "/v1/changes")) == 1


def test_work_markdown_links_the_outcome_and_pads_every_table_alike(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASHIGARU_MD_COLUMNS", "136")
    desk.on("GET", "/v1/status", (200, board_()))
    desk.on("POST", "/v1/changes", (200, {}))
    assert main(["work", "--markdown"]) == 0
    out = capsys.readouterr().out
    assert "[python-sdlc#66](https://github.com/crunchtools/python-sdlc/issues/66)" in out
    assert "[RT#1656](https://rt.fatherlinux.com/Ticket/Display.html?id=1656)" in out
    heads = [line for line in out.splitlines() if line.startswith("| SEAT")]
    assert len(heads) == len(("seats", "needs you", "changed")), "three tables"
    assert len(set(heads)) == 1, "the three tables share their widths"
    assert "🟢 working" in out


def test_work_wide_adds_the_id_time_pr_done_when_and_check(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("GET", "/v1/status", (200, board_(changes=None)))
    assert main(["work", "--wide"]) == 0
    head = capsys.readouterr().out.splitlines()[1]
    for column in ("TIME", "PR", "WORK", "DONE WHEN", "CHECK"):
        assert column in head


def test_the_board_acks_only_what_it_showed_and_after_printing(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("GET", "/v1/status", (200, board_()))
    desk.on("POST", "/v1/changes", (200, {}))
    assert main(["work"]) == 0
    (posted,) = desk.calls("POST", "/v1/changes")
    assert posted.body == {"until": 9}, "no further than the window shown"


def test_a_cut_change_list_stays_unseen(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    board = board_()
    many = [board["changes"]["items"][0] | {"id": f"a{n}"} for n in range(12)]
    desk.on("GET", "/v1/status", (200, board | {"changes": board["changes"] | {"items": many}}))
    assert main(["work"]) == 0
    assert "2 more, left unseen" in capsys.readouterr().out
    assert not desk.calls("POST", "/v1/changes")


def test_a_needs_you_row_with_no_state_renders(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    stale = {
        "why": "stale",
        "work": None,
        "seat": "Dolza 3 - Max",
        "mode": "manual",
        "state": None,
        "text": "x",
    }
    desk.on("GET", "/v1/status", (200, board_(needs_you=[stale], changes=None)))
    assert main(["work"]) == 0
    assert main(["work", "--markdown"]) == 0


def test_a_bad_markdown_width_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASHIGARU_MD_COLUMNS", "abc")
    assert config.markdown_columns() > 0


def test_a_board_with_nothing_changed_acks_nothing(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    board = board_()
    desk.on("GET", "/v1/status", (200, board | {"changes": board["changes"] | {"items": []}}))
    assert main(["work"]) == 0
    assert not desk.calls("POST", "/v1/changes")


@pytest.mark.parametrize(
    ("value", "header", "cell"),
    [
        ("Fix *bold* and `code` | pipes", "TASK", r"Fix \*bold\* and \`code\` \| pipes"),
        ("[python-sdlc#66](https://github.com/x/python-sdlc/issues/66)", "OUTCOME",
         "[python-sdlc#66](https://github.com/x/python-sdlc/issues/66)"),
        ("[a-very-long-repository-name#1](https://x/1)", "OUTCOME", "[a-very-long-reposit…](https://x/1)"),
        ("Exedore 12 - Max-Overflow-Seat", "SEAT", "Exedore 12 - Max-…"),
        ("working", "STATE", "🟢 working"),
        (None, "TASK", ""),
    ],
)  # fmt: skip
def test_a_markdown_cell_escapes_clips_and_marks(value: Any, header: str, cell: str) -> None:
    assert render._md_cell(value, header) == cell


def test_shown_width_counts_a_link_by_its_label_an_escape_once_and_an_emoji_twice() -> None:
    assert render._shown("[RT#1](https://x)") == len("RT#1")
    assert render._shown(r"a\*b") == len("a*b")
    assert render._shown("🟢 x") == len("🟢 x") + 1


# Versioned writes


def test_if_version_sends_if_match(desk: FakeDesk) -> None:
    desk.on("PATCH", "/v1/items/a1", (200, item(version=5)))
    desk.on("POST", "/v1/items/a1/report", (200, item(version=6)))
    for path in ("wrap", "assign", "claim", "file"):
        desk.on("POST", f"/v1/items/a1/{path}", (200, item(version=7)))
    for argv in (
        ["update", "a1", "--note", "x"],
        ["report", "a1", "DONE", "--text", "t"],
        ["wrap", "a1"],
        ["assign", "a1", "--seat", "Vertex 1"],
        ["claim", "a1"],
        ["file", "a1", "--task", "t"],
    ):
        assert main([*argv, "--if-version", "4"]) == 0
    assert [s.headers.get("If-Match") for s in desk.seen] == ["4"] * 6
    assert main(["update", "a1", "--note", "x"]) == 0
    assert "If-Match" not in desk.seen[-1].headers


def test_a_stale_version_prints_the_desks_reason_and_the_item_as_it_stands(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("PATCH", "/v1/items/a1", (412, {"detail": "a1 moved on to version 6", "code": "version"}))
    desk.on("GET", "/v1/items/a1", (200, item(version=6, state="review")))
    assert main(["update", "a1", "--note", "x", "--if-version", "4"]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert "a1 moved on to version 6" in captured.err
    assert lines[0].split()[-1] == "VERSION" and lines[1].split()[-1] == "6" and "review" in lines[1]


def test_get_shows_the_version(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/items/a1", (200, item(version=9)))
    assert main(["get", "a1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[-1] == "VERSION" and lines[1].split()[-1] == "9"


def test_a_desk_error_carries_its_code(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/items/a1", (409, {"detail": "busy", "code": "busy"}))
    desk.on("GET", "/v1/items/a2", (409, {"detail": "old desk"}))
    client = Desk(config.load())
    with pytest.raises(DeskError) as busy:
        client.get("/v1/items/a1")
    with pytest.raises(DeskError) as old:
        client.get("/v1/items/a2")
    client.close()
    assert (busy.value.code, old.value.code) == ("busy", None)
