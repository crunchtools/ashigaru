"""Fixtures for the Desk: a fresh database, a client, and one key per role."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashigaru.desk import auth, store
from ashigaru.desk.api import create_app
from ashigaru.desk.forges import github
from ashigaru.desk.model import Verdict

LEDGER = Path(__file__).parent / "fixtures" / "agents-ledger.jsonl"
"""Every line of a real ledger with its keys, ids, states and times; the free text is placeholder."""


class RequiredChecks:
    """The forge's required-check lookup in GitHub's place: what each branch requires, and what each
    check has reported; a check with no report is pending. `None` for a branch makes it unreadable."""

    def __init__(self) -> None:
        self.rules: dict[str, tuple[str, ...] | None] = {}
        self.reported: dict[str, Verdict] = {}
        self.asked: list[tuple[str, str, str | None]] = []
        self.refusal: OSError = OSError("unreadable")

    def __call__(self, repo: str, sha: str, branch: str | None, token: str) -> tuple[str, dict[str, Verdict]]:
        self.asked.append((repo, sha, branch))
        branch = branch or "main"
        names = self.rules.get(branch, ())
        if names is None:
            raise self.refusal
        return branch, {name: self.reported.get(name, Verdict.PENDING) for name in names}


@pytest.fixture(autouse=True)
def required_checks(monkeypatch: pytest.MonkeyPatch) -> RequiredChecks:
    """No test reaches GitHub for required checks; by default a branch requires none."""
    fake = RequiredChecks()
    monkeypatch.setattr(github.FORGE, "required", fake)
    return fake


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "desk.db"


@pytest.fixture
def conn(db: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(db)
    store.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(db: Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    """The app over the same file `conn` writes, so a test can arrange rows directly."""
    with TestClient(create_app(db)) as test_client:
        yield test_client


class Keys(dict[str, dict[str, str]]):
    """Authorization headers by identity name."""


@pytest.fixture
def keys(conn: sqlite3.Connection) -> Keys:
    out = Keys()
    for name, role in (
        ("Josui Prime", "admin"),
        ("Exedore 1 - Max", "seat"),
        ("Exedore 2 - Max", "seat"),
        ("ci", "ci"),
    ):
        store.create_identity(conn, name, role)
        _, token = auth.mint(conn, name)
        out[name] = {"Authorization": f"Bearer {token}"}
    return out
