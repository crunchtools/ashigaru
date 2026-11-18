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
from ashigaru.cli import commands, config, hooks
from ashigaru.cli.__main__ import main
from ashigaru.cli.client import Desk, DeskError, Unreachable
from ashigaru.cli.wait import CIWaiter, PollWaiter

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
    assert lines[0].split() == ["SEAT", "STATUS", "ITEM", "FOR", "TURN", "TASK", "SEEN"]
    assert lines[1].startswith("Vertex 1") and "a1" in lines[1]


def test_agents_shows_missing_seats(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    never = {"name": "Max 4", "status": "free", "turn": "idle", "updated_at": None, "missing": True}
    desk.on("GET", "/v1/seats", (200, [SEAT | {"updated_at": "2026-11-17T10:00:00Z"}, never]))
    assert main(["agents"]) == 0
    last = capsys.readouterr().out.splitlines()[-1].split()
    assert last[:3] == ["Max", "4", "missing"] and last[-1] == "never"


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
    assert main(["work"]) == 0
    out = capsys.readouterr().out
    assert "needs a key" in out and "Max 2" in out and "missing" in out
    assert "1 of 2 up, 1 items open" in out
    assert "a3" in out and "vertex" in out
    assert "NEW" in out and "title of a8" in out and "fatherlinux" in out
    assert "held" in out and "events 40 to 57" in out
    assert "REOPENED" not in out


def test_work_shows_what_reopened(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    caught = round_()["catch_up"] | {"reopened": [caught_row("a7", state="done")]}
    desk.on("GET", "/v1/work", (200, round_(catch_up=caught)))
    assert main(["work"]) == 0
    out = capsys.readouterr().out
    assert "REOPENED" in out and "title of a7" in out


def test_work_all_and_since_are_sent(desk: FakeDesk) -> None:
    desk.on("GET", "/v1/work", (200, round_()))
    assert main(["work"]) == 0
    assert main(["work", "--all", "--since", "2026-11-01T00:00:00Z"]) == 0
    first, second = desk.calls("GET", "/v1/work")
    assert first.query == {}
    assert second.query == {"all": ["true"], "since": ["2026-11-01T00:00:00Z"]}


def test_work_advance_posts(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    moved = round_()
    moved["catch_up"] = moved["catch_up"] | {"advanced": True}
    desk.on("POST", "/v1/work", (200, moved))
    assert main(["work", "--advance", "--since", "2026-11-01T00:00:00Z"]) == 0
    assert [s.method for s in desk.seen] == ["POST"]
    assert desk.seen[0].query == {"since": ["2026-11-01T00:00:00Z"]}
    assert "sweep mark moved" in capsys.readouterr().out


def test_work_says_when_a_list_was_cut(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    cut = round_(truncated=True)
    cut["catch_up"] = cut["catch_up"] | {"truncated": True}
    desk.on("GET", "/v1/work", (200, cut))
    assert main(["work"]) == 0
    out = capsys.readouterr().out
    assert "the first 1000" in out and "the next --advance reads on" in out


def test_work_json_prints_the_whole_round(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("GET", "/v1/work", (200, round_()))
    assert main(["work", "--json"]) == 0
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


def test_assign_task_keeps_the_id_when_the_assign_is_refused(
    desk: FakeDesk, capsys: pytest.CaptureFixture[str]
) -> None:
    desk.on("POST", "/v1/items", (201, item(id="a8")))
    desk.on("POST", "/v1/items/a8/assign", (409, {"detail": "no free seat in lane vertex"}))
    desk.on("GET", "/v1/items/a8", (200, item(id="a8", lane="vertex")))
    assert main(["assign", "--task", "t", "--lane", "vertex", "--subject-key", "RT#1"]) == 1
    out = capsys.readouterr()
    assert out.out.splitlines()[1].startswith("a8") and "recorded a8, not assigned" in out.err
    assert desk.calls("POST", "/v1/items")[0].body == {"task": "t", "lane": "vertex", "subject_key": "RT#1"}


def test_assign_task_does_not_hide_a_desk_failure(desk: FakeDesk, capsys: pytest.CaptureFixture[str]) -> None:
    desk.on("POST", "/v1/items", (201, item(id="a8")))
    desk.on("POST", "/v1/items/a8/assign", (500, {"detail": "boom"}))
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
    assert client.request("GET", "/v1/ci/wait", timeout=2.0)["status"] == "success"


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
    with pytest.raises(TimeoutError, match="still pending"):
        waiter.wait("crunchtools/ashigaru", SHA, 30)
    assert [seen.query["timeout"] for seen in desk.calls("GET", "/v1/ci/wait")] == [["30"]]


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
