"""Seat reconcile: the Desk ends turns no hook ended and drops stale tasks, and leaves Scott's holds
for him under NEEDS YOU."""

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import __main__ as desk_main
from ashigaru.desk import dispatch, reconcile, store
from ashigaru.desk.api import create_app

JOSUI, SEAT = "Josui Prime", "Exedore 1 - Max"
TWO_HOURS = 7200.0
ONE_MINUTE = 60.0


def ago(**span: float) -> str:
    return (datetime.now(UTC) - timedelta(**span)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seat(conn: sqlite3.Connection, name: str, written: str, **fields: Any) -> None:
    """A seat row as its hooks left it, last written at `written`."""
    with store.transaction(conn):
        store.write_seat(conn, name, fields)
        conn.execute("UPDATE seats SET updated_at = ? WHERE name = ?", (written, name))


def row(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    found = store.get_seat(conn, name)
    assert found
    return found


def reconciling(db: Path) -> dict[str, int]:
    return reconcile.run(str(db), reconcile.Settings())


def test_a_silent_turn_past_the_limit_ends_without_touching_updated_at(
    conn: sqlite3.Connection, db: Path
) -> None:
    seat(conn, "dead", ago(minutes=30), turn="active")
    conn.execute("UPDATE seats SET turn_started_at = ? WHERE name = 'dead'", (ago(hours=4),))
    assert reconciling(db) == {"turns": 1, "tasks": 0, "silent": 0}
    found = row(conn, "dead")
    assert found["turn"] == "idle" and found["turn_reconciled_at"]
    assert found["updated_at"] == ago(minutes=30), "a dead seat still goes missing on time"


@pytest.mark.parametrize(
    ("started", "written"), [(ago(hours=4), ago(minutes=2)), (ago(hours=1), ago(minutes=30))]
)
def test_a_turn_still_heard_from_or_within_the_limit_is_left(
    conn: sqlite3.Connection, db: Path, started: str, written: str
) -> None:
    seat(conn, "alive", written, turn="active")
    conn.execute("UPDATE seats SET turn_started_at = ? WHERE name = 'alive'", (started,))
    assert reconciling(db)["turns"] == 0
    assert row(conn, "alive")["turn"] == "active"


def test_a_turn_with_no_start_is_aged_by_its_last_write(conn: sqlite3.Connection, db: Path) -> None:
    seat(conn, "legacy", ago(hours=4), turn="active")
    seat(conn, "recent", ago(minutes=30), turn="active")
    conn.execute("UPDATE seats SET turn_started_at = NULL")
    assert reconciling(db)["turns"] == 1
    assert (row(conn, "legacy")["turn"], row(conn, "recent")["turn"]) == ("idle", "active")


def test_a_new_turn_clears_the_reconciled_mark(conn: sqlite3.Connection, db: Path) -> None:
    seat(conn, "back", ago(minutes=30), turn="active")
    conn.execute("UPDATE seats SET turn_started_at = ? WHERE name = 'back'", (ago(hours=4),))
    reconciling(db)
    with store.transaction(conn):
        store.write_seat(conn, "back", {"turn": "active"})
    assert row(conn, "back")["turn_reconciled_at"] is None


def test_a_day_idle_drops_an_unheld_seats_task_and_keeps_a_hold(conn: sqlite3.Connection, db: Path) -> None:
    seat(conn, "idle", ago(hours=25), task="old first prompt", waiting_on="anything else?")
    seat(conn, "held", ago(hours=25), task="Scott's work", owner=store.MANUAL_OWNER, status="busy")
    seat(conn, "recent", ago(hours=1), task="today's first prompt")
    assert reconciling(db) == {"turns": 0, "tasks": 1, "silent": 1}
    assert (row(conn, "idle")["task"], row(conn, "idle")["waiting_on"]) == (None, None)
    assert row(conn, "held")["task"] == "Scott's work"
    assert row(conn, "recent")["task"] == "today's first prompt"


def test_a_hold_silent_for_a_day_needs_scott_and_is_never_closed(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    dispatch.report_seat(conn, SEAT, {"turn": "active", "task": "Scott's own work"}, SEAT)
    dispatch.report_seat(conn, SEAT, {"turn": "idle"}, SEAT)
    held = row(conn, SEAT)["item"]
    conn.execute("UPDATE seats SET updated_at = ? WHERE name = ?", (ago(hours=25), SEAT))
    reconciling(db)
    board = client.get("/v1/status", headers=keys[JOSUI]).json()
    stale = [need for need in board["needs_you"] if need["why"] == "stale"]
    assert [need["work"] for need in stale] == [held] and f"wrap {held}" in stale[0]["text"]
    assert (store.get_item(conn, held) or {})["state"] == "working", "only Scott closes his work"


@pytest.mark.parametrize(("up", "ended"), [(timedelta(minutes=2), 0), (timedelta(minutes=30), 1)])
def test_silence_counts_only_from_the_desks_start(
    conn: sqlite3.Connection, db: Path, up: timedelta, ended: int
) -> None:
    seat(conn, "quiet", ago(minutes=40), turn="active")
    conn.execute("UPDATE seats SET turn_started_at = ? WHERE name = 'quiet'", (ago(hours=4),))
    started = datetime.now(UTC) - up
    assert reconcile.run(str(db), reconcile.Settings(), started)["turns"] == ended


def test_the_app_reconciles_at_startup_and_spares_turns_an_outage_silenced(
    db: Path, conn: sqlite3.Connection
) -> None:
    seat(conn, "live", ago(minutes=30), turn="active")
    conn.execute("UPDATE seats SET turn_started_at = ? WHERE name = 'live'", (ago(hours=4),))
    seat(conn, "idle", ago(hours=25), task="old first prompt")
    app = create_app(db)
    app.state.reconciler = reconcile.Reconciler(str(db), reconcile.Settings(every=3600))
    with TestClient(app):
        for _ in range(100):
            if row(conn, "idle")["task"] is None:
                break
            time.sleep(0.05)
    assert row(conn, "idle")["task"] is None
    assert row(conn, "live")["turn"] == "active", "the Desk was down, not the seat"


def test_serve_reconciles_with_its_turn_limit(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    assert desk_main.reconciler(str(db)).settings.turn_max == reconcile.TURN_MAX
    assert desk_main.reconciler(str(db)).settings.silent == reconcile.SILENT
    monkeypatch.setenv("ASHIGARU_SILENT_AFTER", "60")
    assert desk_main.reconciler(str(db)).settings.silent.total_seconds() == ONE_MINUTE
    monkeypatch.setenv("ASHIGARU_TURN_MAX", "7200")
    assert desk_main.reconciler(str(db)).settings.turn_max.total_seconds() == TWO_HOURS


def test_a_busy_seat_quiet_past_silent_is_flagged_without_touching_updated_at(
    conn: sqlite3.Connection, db: Path
) -> None:
    seat(conn, "quiet", ago(minutes=20), status="busy", item="w1")
    seat(conn, "heard", ago(minutes=5), status="busy", item="w2")
    assert reconciling(db)["silent"] == 1
    assert row(conn, "quiet")["silent_since"] and row(conn, "heard")["silent_since"] is None
    assert row(conn, "quiet")["updated_at"] == ago(minutes=20)
    assert reconciling(db)["silent"] == 0, "flagged once"


def test_a_free_seat_is_never_flagged_silent(conn: sqlite3.Connection, db: Path) -> None:
    seat(conn, "free", ago(hours=2))
    assert reconciling(db)["silent"] == 0 and row(conn, "free")["silent_since"] is None


@pytest.mark.parametrize(("up", "flagged"), [(timedelta(minutes=5), 0), (timedelta(minutes=30), 1)])
def test_silence_is_flagged_only_once_the_desk_has_been_up_for_silent(
    conn: sqlite3.Connection, db: Path, up: timedelta, flagged: int
) -> None:
    seat(conn, "quiet", ago(minutes=40), status="busy", item="w1")
    started = datetime.now(UTC) - up
    assert reconcile.run(str(db), reconcile.Settings(), started)["silent"] == flagged


def test_a_write_clears_silent_since_unless_it_sets_it(conn: sqlite3.Connection, db: Path) -> None:
    seat(conn, "quiet", ago(minutes=20), status="busy", item="w1")
    reconciling(db)
    assert row(conn, "quiet")["silent_since"]
    with store.transaction(conn):
        assert store.write_seat(conn, "quiet", {"context_used": 5})["silent_since"] is None
        store.write_seat(conn, "quiet", {"silent_since": ago(minutes=1)})
    assert row(conn, "quiet")["silent_since"] == ago(minutes=1)


def test_a_silent_busy_seat_needs_scott_once(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    with store.transaction(conn):
        work = store.insert_item(
            conn, {"task": "Build it", "mode": "auto", "state": "working"}, JOSUI, source="test"
        )
    seat(conn, SEAT, ago(minutes=20), status="busy", item=work)
    reconciling(db)
    board = client.get("/v1/status", headers=keys[JOSUI]).json()
    silent = [need for need in board["needs_you"] if need["why"] == "silent"]
    assert [(need["seat"], need["work"], need["mode"]) for need in silent] == [(SEAT, work, "auto")]
    assert (
        silent[0]["text"].endswith(f"check the seat or wrap {work}") and "silent since" in silent[0]["text"]
    )
    assert not [need for need in board["needs_you"] if need["why"] == "stale"]
