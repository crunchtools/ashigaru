"""Fixtures for the Desk: a fresh database, a client, and one key per role."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashigaru.desk import auth, store
from ashigaru.desk.api import create_app

LEDGER = Path(__file__).parent / "fixtures" / "agents-ledger.jsonl"
"""Every line of a real ledger with its keys, ids, states and times; the free text is placeholder."""


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
