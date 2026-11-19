"""The sweep: the Desk reads the forge back on a timer, records what it says, and settles items."""

import dataclasses
import http.client
import sqlite3
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.client import CONFLICT, CREATED, FORBIDDEN, OK
from pathlib import Path
from typing import Any

import pytest
from conftest import Keys
from fastapi.testclient import TestClient

from ashigaru.desk import __main__ as desk_main
from ashigaru.desk import dispatch, store, sweep
from ashigaru.desk.api import create_app
from ashigaru.desk.forges import Found, IntakeItem, Search
from ashigaru.desk.model import Kind

JOSUI, SEAT = "Josui Prime", "Exedore 1 - Max"
ORG = "crunchtools"
HOUR = 3600.0
LONG_AGO = "2020-01-01T00:00:00Z"
TWO_MINUTES = 120.0
LAPSED_AT = 10
"""The tick at which the test lets the stale lease lapse."""
WRITERS = 2
"""Of the four authors the search found, two are queued: the member and the app's bot."""


def subject(number: int, kind: Kind = Kind.STORY, **fields: Any) -> IntakeItem:
    defaults = {"author": "fatherlinux", "updated_at": "2026-11-19T10:00:00Z"}
    return IntakeItem(
        subject_key=f"crunchtools/x#{number}",
        title=f"title {number}",
        ref=f"https://github.com/crunchtools/x/pull/{number}",
        source="github",
        repo="crunchtools/x",
        kind=kind,
        **(defaults | fields),
    )


@dataclasses.dataclass
class FakeForge:
    """What GitHub would answer: each subject by number, and the search's hits."""

    subjects: dict[int, IntakeItem] = dataclasses.field(default_factory=dict)
    hits: list[Found] = dataclasses.field(default_factory=list)
    searched: list[str] = dataclasses.field(default_factory=list)
    refuse: dict[int, Exception] = dataclasses.field(default_factory=dict)
    complete: bool = True
    writers: frozenset[str] = frozenset({"fatherlinux"})

    def subject(self, repo: str, number: int, token: str) -> IntakeItem:
        if number in self.refuse:
            raise self.refuse[number]
        return self.subjects[number]

    def updated(self, org: str, since: str, token: str) -> Search:
        self.searched.append(since)
        return Search(self.hits, self.complete)

    def may_write(self, repo: str, account: str, token: str) -> bool:
        return account in self.writers


def sweeping(db: Path, forge: FakeForge, authors: frozenset[str] = frozenset()) -> dict[str, Any]:
    run = sweep.run(str(db), sweep.Settings(forge, "token", ORG, authors), "request")
    assert run["skipped"] is None, "another run held the lease"
    return run


def create(client: TestClient, keys: Keys, **body: Any) -> str:
    made = client.post("/v1/items", headers=keys[JOSUI], json={"task": "t"} | body)
    assert made.status_code == CREATED, made.text
    return str(made.json()["id"])


def item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    found = store.get_item(conn, item_id)
    assert found
    return found


def test_subjects_are_read_in_bounded_batches_and_all_are_read(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "READ_BATCH", 2)
    numbers = range(1, 6)
    for number in numbers:
        create(client, keys, subject_key=f"crunchtools/x#{number}")
    run = sweeping(db, FakeForge(subjects={number: subject(number) for number in numbers}))
    assert run["subjects"] == len(numbers) and run["error"] is None


def test_asking_who_may_write_looks_the_hits_up_in_one_query(conn: sqlite3.Connection) -> None:
    hits = [Found(subject(number, author=f"member{number}"), "MEMBER", "") for number in range(1, 6)]
    queries: list[str] = []
    conn.set_trace_callback(queries.append)
    counts = sweep.Counts()
    sweep._ask(conn, sweep.Settings(FakeForge(), "token", ORG), hits, counts)
    conn.set_trace_callback(None)
    assert len([query for query in queries if "FROM items" in query]) == 1
    assert len(counts.writers) == len(hits)


def test_a_missed_merge_is_caught_and_settles_the_item(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    review = create(client, keys, subject_key="crunchtools/x#5")
    forge = FakeForge(subjects={5: subject(5, closed=True, merged=True, head="ab" * 20)})
    run = sweeping(db, forge)
    assert (run["subjects"], run["closed"], run["error"], run["trigger"]) == (1, 1, None, "request")
    assert (item(conn, review)["state"], item(conn, review)["result"]) == ("done", "merged at abababababab")
    assert (store.get_mark(conn, sweep.MARK) or {})["value"] == run["started_at"]


def test_the_search_queues_writers_and_only_lists_everyone_else(conn: sqlite3.Connection, db: Path) -> None:
    hits = [
        Found(subject(1, lane_hint="vertex"), "MEMBER", ""),
        Found(subject(2, author="outsider"), "CONTRIBUTOR", ""),
        Found(subject(3, author="dependabot[bot]"), "NONE", ""),
        Found(subject(4, author="ashigaru-crunchtools[bot]"), "NONE", ""),
    ]
    run = sweeping(db, FakeForge(hits=hits), frozenset({"ashigaru-crunchtools[bot]"}))
    assert run["new"] == WRITERS
    rows = {row["subject_key"]: row for row in store.list_items(conn)}
    assert set(rows) == {"crunchtools/x#1", "crunchtools/x#4"}
    member = rows["crunchtools/x#1"]
    assert (member["mode"], member["lane"], member["extra"]["trusted"]) == ("auto", "vertex", True)
    assert [found["author"] for found in run["outsiders"]] == ["outsider", "dependabot[bot]"]
    stored = {row[0] for row in conn.execute("SELECT subject_key FROM subjects")}
    assert stored == {"crunchtools/x#1", "crunchtools/x#4"}
    listed = [need["text"] for need in dispatch.status(conn)["needs_you"] if need["why"] == "outsider"]
    assert listed == [
        "https://github.com/crunchtools/x/pull/2 by outsider: not queued",
        "https://github.com/crunchtools/x/pull/3 by dependabot[bot]: not queued",
    ]


def test_a_writers_pr_naming_an_item_becomes_its_pr(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    story = create(client, keys)
    other = create(client, keys)
    hits = [
        Found(subject(9, closed=True, merged=True), "MEMBER", f"Does the thing ({story})"),
        Found(subject(10), "CONTRIBUTOR", f"Hijack ({other})"),
    ]
    sweeping(db, FakeForge(hits=hits))
    assert item(conn, story)["pr"] == "crunchtools/x#9" and item(conn, story)["state"] == "done"
    assert item(conn, other)["pr"] is None


def test_a_subject_it_cannot_read_is_reported_and_the_rest_still_settle(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    gone = create(client, keys, subject_key="crunchtools/x#6")
    merged = create(client, keys, subject_key="crunchtools/x#7")
    refused = urllib.error.HTTPError("u", 404, "Not Found", Message(), None)
    forge = FakeForge(subjects={7: subject(7, closed=True, merged=True)}, refuse={6: refused})
    run = sweeping(db, forge)
    assert run["error"] == "crunchtools/x#6: HTTP 404"
    assert (item(conn, gone)["state"], item(conn, merged)["state"]) == ("queued", "done")


def test_a_search_that_fails_leaves_the_mark_for_the_next_run(conn: sqlite3.Connection, db: Path) -> None:
    store.set_mark(conn, sweep.MARK, "2026-11-19T09:00:00Z", JOSUI)

    class Down(FakeForge):
        def updated(self, org: str, since: str, token: str) -> Search:
            raise urllib.error.HTTPError("u", 403, "Forbidden", Message(), None)

    run = sweeping(db, Down())
    assert run["error"] == "search: HTTP 403" and run["finished_at"]
    assert (store.get_mark(conn, sweep.MARK) or {})["value"] == "2026-11-19T09:00:00Z"
    forge = FakeForge()
    sweeping(db, forge)
    assert forge.searched == ["2026-11-19T08:50:00Z"], "the search reaches back OVERLAP past the mark"


def test_sweep_routes_say_whether_a_timer_runs_and_only_an_admin_starts_one(
    client: TestClient, keys: Keys
) -> None:
    off = client.get("/v1/sweep", headers=keys[SEAT]).json()
    assert (off["configured"], off["last"]) == (False, None)
    assert client.post("/v1/sweep", headers=keys[JOSUI]).status_code == CONFLICT


def test_the_timer_runs_at_startup_and_on_request(db: Path, keys: Keys, conn: sqlite3.Connection) -> None:
    app = create_app(db)
    app.state.sweeper = sweep.Sweeper(str(db), sweep.Settings(FakeForge(), "token", ORG, every=HOUR))
    with TestClient(app) as running:
        assert running.post("/v1/sweep", headers=keys[SEAT]).status_code == FORBIDDEN
        started = running.post("/v1/sweep", headers=keys[JOSUI])
        assert started.status_code == OK and started.json()["requested"] is True
        for _ in range(100):
            runs = [row[0] for row in conn.execute("SELECT trigger FROM sweeps ORDER BY id")]
            if runs == ["startup", "request"]:
                break
            time.sleep(0.05)
        assert runs == ["startup", "request"]
        state = running.get("/v1/sweep", headers=keys[SEAT]).json()
        assert state["configured"] and state["every"] == HOUR and state["last"]["trigger"] == "request"
        assert running.get("/v1/status", headers=keys[SEAT]).json()["sweep"]["trigger"] == "request"


def test_a_live_lease_skips_a_run_with_a_row_and_a_killed_runs_lapses_in_a_lease(
    conn: sqlite3.Connection, db: Path
) -> None:
    settings = sweep.Settings(FakeForge(), "token", ORG, every=HOUR)
    store.set_mark(conn, sweep.LOCK, f"{sweep._beat(datetime.now(UTC))} 41", JOSUI)
    skipped = sweep.run(str(db), settings, "startup")
    assert skipped["skipped"].startswith("run 41 holds the lease") and skipped["finished_at"]
    assert sweep.last(conn) == skipped
    killed = datetime.now(UTC) - timedelta(seconds=sweep.LEASE + 1)
    store.set_mark(conn, sweep.LOCK, f"{sweep._beat(killed)} 41", JOSUI)
    ran = sweep.run(str(db), settings, "startup")
    assert ran["skipped"] is None and ran["error"] is None
    assert store.get_mark(conn, sweep.LOCK) is None


def test_a_run_renews_its_lease_while_it_works(conn: sqlite3.Connection, db: Path) -> None:
    settings = sweep.Settings(FakeForge(), "token", ORG, every=HOUR, lease=0.2)
    others: list[dict[str, Any]] = []

    class Slow(FakeForge):
        def updated(self, org: str, since: str, token: str) -> Search:
            time.sleep(0.5)
            others.append(sweep.run(str(db), settings, "request"))
            return Search([], True)

    ran = sweep.run(str(db), dataclasses.replace(settings, forge=Slow()), "timer")
    assert others[0]["skipped"].startswith(f"run {ran['id']} holds the lease"), "renewed past one lease"
    assert store.get_mark(conn, sweep.LOCK) is None, "released at the end"


def test_the_timer_retries_a_skipped_startup_once_the_lease_could_lapse(
    db: Path, conn: sqlite3.Connection
) -> None:
    store.set_mark(conn, sweep.LOCK, f"{sweep._beat(datetime.now(UTC))} 41", JOSUI)
    app = create_app(db)
    app.state.sweeper = sweep.Sweeper(
        str(db), sweep.Settings(FakeForge(), "token", ORG, every=HOUR, lease=0.05)
    )
    with TestClient(app):
        for tick in range(100):
            runs = [
                tuple(row) for row in conn.execute("SELECT trigger, skipped IS NULL FROM sweeps ORDER BY id")
            ]
            if len(runs) > 1:
                break
            if tick == LAPSED_AT:
                store.set_mark(conn, sweep.LOCK, f"{LONG_AGO} 41", JOSUI)
            time.sleep(0.05)
    assert runs[:2] == [("startup", 0), ("timer", 1)]


def test_a_permission_check_cut_short_is_that_subjects_error_and_the_search_goes_on(
    conn: sqlite3.Connection, db: Path
) -> None:
    class Cut(FakeForge):
        def may_write(self, repo: str, account: str, token: str) -> bool:
            if account == "reader":
                raise http.client.IncompleteRead(b"")
            return super().may_write(repo, account, token)

    hits = [
        Found(subject(1, author="reader"), "MEMBER", ""),
        Found(subject(2, author="fatherlinux"), "MEMBER", ""),
    ]
    run = sweeping(db, Cut(hits=hits))
    assert run["error"] == "crunchtools/x#1: may reader write? IncompleteRead"
    keys = {row["subject_key"] for row in store.list_items(conn)}
    assert keys == {"crunchtools/x#2"}, "the unjudged subject waits for the next run"


def test_a_body_cut_short_is_one_subjects_error_and_the_run_still_finishes(
    client: TestClient, keys: Keys, conn: sqlite3.Connection, db: Path
) -> None:
    create(client, keys, subject_key="crunchtools/x#6")
    merged = create(client, keys, subject_key="crunchtools/x#7")
    forge = FakeForge(
        subjects={7: subject(7, closed=True, merged=True)}, refuse={6: http.client.IncompleteRead(b"")}
    )
    run = sweeping(db, forge)
    assert run["finished_at"] and run["error"] == "crunchtools/x#6: IncompleteRead"
    assert item(conn, merged)["state"] == "done"


def test_an_unexpected_failure_still_finishes_the_row_and_frees_the_lock(
    conn: sqlite3.Connection, db: Path
) -> None:
    class Broken(FakeForge):
        def updated(self, org: str, since: str, token: str) -> Search:
            raise TypeError("a bug")

    with pytest.raises(TypeError):
        sweep.run(str(db), sweep.Settings(Broken(), "token", ORG), "timer")
    row = sweep.last(conn) or {}
    assert row["finished_at"] and row["error"] == "stopped: TypeError"
    assert store.get_mark(conn, sweep.LOCK) is None


def test_a_search_cut_at_the_cap_moves_the_mark_only_to_its_last_hit(
    conn: sqlite3.Connection, db: Path
) -> None:
    hits = [Found(subject(1, updated_at="2026-11-19T07:00:00Z"), "MEMBER", "")]
    run = sweeping(db, FakeForge(hits=hits, complete=False))
    assert run["error"].startswith("search: cut at the forge's cap")
    assert (store.get_mark(conn, sweep.MARK) or {})["value"] == "2026-11-19T07:00:00Z"


def test_a_member_who_cannot_write_to_the_repo_is_recorded_but_held(
    conn: sqlite3.Connection, db: Path
) -> None:
    hits = [
        Found(subject(1, author="reader"), "MEMBER", "Names (a1)"),
        Found(subject(2, author="fatherlinux"), "MEMBER", ""),
    ]
    run = sweeping(db, FakeForge(hits=hits))
    rows = {row["subject_key"]: row for row in store.list_items(conn)}
    held, writer = rows["crunchtools/x#1"], rows["crunchtools/x#2"]
    assert run["new"] == WRITERS and run["outsiders"] == []
    assert (held["mode"], held["lane"], held["extra"]["trusted"]) == ("manual", None, False)
    assert (writer["mode"], writer["extra"]["trusted"]) == ("auto", True)
    assert held["pr"] is None, "a PR from someone who cannot write links nothing"


def test_old_runs_are_pruned(conn: sqlite3.Connection, db: Path) -> None:
    conn.execute(
        "INSERT INTO sweeps (trigger, started_at, finished_at) VALUES ('timer', ?, ?)", (LONG_AGO, LONG_AGO)
    )
    sweeping(db, FakeForge())
    assert [row[0] for row in conn.execute("SELECT started_at FROM sweeps")] != [LONG_AGO]
    assert conn.execute("SELECT count(*) FROM sweeps WHERE started_at = ?", (LONG_AGO,)).fetchone()[0] == 0


def test_the_timer_runs_again_after_its_interval(db: Path, conn: sqlite3.Connection) -> None:
    app = create_app(db)
    app.state.sweeper = sweep.Sweeper(str(db), sweep.Settings(FakeForge(), "token", ORG, every=0.05))
    with TestClient(app):
        for _ in range(100):
            runs = [row[0] for row in conn.execute("SELECT trigger FROM sweeps ORDER BY id")]
            if "timer" in runs:
                break
            time.sleep(0.05)
    assert runs[0] == "startup" and "timer" in runs


def test_two_runs_racing_for_the_lock_make_one_run(db: Path, conn: sqlite3.Connection) -> None:
    class Slow(FakeForge):
        def updated(self, org: str, since: str, token: str) -> Search:
            time.sleep(0.3)
            return Search([], True)

    settings = sweep.Settings(Slow(), "token", ORG, every=HOUR)
    with ThreadPoolExecutor(2) as pool:
        runs = list(pool.map(lambda trigger: sweep.run(str(db), settings, trigger), ["timer", "request"]))
    assert sorted(bool(run["skipped"]) for run in runs) == [False, True]
    assert conn.execute("SELECT count(*) FROM sweeps WHERE skipped IS NULL").fetchone()[0] == 1


def test_serve_sweeps_only_with_a_token_and_an_org(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    monkeypatch.setenv("ASHIGARU_FORGE_TOKEN_GITHUB", "token")
    assert desk_main.sweeper(str(db)) is None
    monkeypatch.setenv("ASHIGARU_FORGE_ORG_GITHUB", " crunchtools ")
    monkeypatch.setenv("ASHIGARU_SWEEP_EVERY", "120")
    monkeypatch.setenv("ASHIGARU_FORGE_AUTHORS_GITHUB", "ashigaru-crunchtools[bot]")
    made = desk_main.sweeper(str(db))
    assert made is not None
    assert (made.settings.org, made.settings.every, made.settings.token) == (ORG, TWO_MINUTES, "token")
    assert made.settings.authors == frozenset({"ashigaru-crunchtools[bot]"})
    monkeypatch.delenv("ASHIGARU_FORGE_TOKEN_GITHUB")
    assert desk_main.sweeper(str(db)) is None
