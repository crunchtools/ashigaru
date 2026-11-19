"""What each piece of work serves: its outcome, the OUTCOME column of `work` and `status`.

An outcome is an item of kind outcome: an RT ticket or a forge issue (its
subject_key names it), or Scott's one-off instruction with no ticket (a short
name as its task, the instruction as its note). A story names it as `parent`.
Work recorded for a story's PR, such as its review or a fix-up, has no parent of
its own and inherits the story's: an item whose subject_key is another's `pr`
serves that item's outcome, as does a child filed under a story.

The work view groups its rows by outcome: groups holding something only Scott
can move first, then the rest, newest first, then the work that serves none,
shown as `-` so the gap is seen.
"""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import store
from .model import CLOSED, Kind, Mode, State, Verdict, subject_key

LABEL = 40
"""The longest one-off outcome name drawn from its instruction."""

OUTCOMES = """
    SELECT id, subject_key, task FROM items
    WHERE kind = 'outcome' AND id IN (SELECT value FROM json_each(:ids))
"""
OUTCOME_BY_KEY = """
    SELECT id, state FROM items
    WHERE kind = 'outcome' AND (subject_key = :key OR (subject_key IS NULL AND ref = :key))
    ORDER BY state IN ('done', 'wontdo', 'abandoned'), updated_at DESC LIMIT 1
"""
"""The outcome item a ref names: one recorded with that subject_key, or one minted here that carries
it as its ref; an open one first."""
IS_PR = "SELECT 1 FROM items WHERE pr = ? LIMIT 1"
FINISHED = """
    SELECT o.id FROM items o
    WHERE o.kind = 'outcome' AND o.mode = 'auto' AND o.seat IS NULL
      AND coalesce(o.wrap, '') != 'cleared' AND o.state NOT IN ('done', 'wontdo', 'abandoned')
      AND EXISTS (SELECT 1 FROM items c WHERE c.parent = o.id)
      AND NOT EXISTS (
          SELECT 1 FROM items c WHERE c.parent = o.id
          AND coalesce(c.wrap, '') != 'cleared' AND c.state NOT IN ('done', 'wontdo', 'abandoned')
      )
"""
"""Open auto outcomes no seat holds whose stories, of which there is one at least, are all closed."""
ACTOR = "desk"
STORIES = """
    SELECT pr, parent FROM items
    WHERE parent IS NOT NULL AND pr IN (SELECT value FROM json_each(:keys)) ORDER BY updated_at
"""
FILED = "SELECT id, parent FROM items WHERE parent IS NOT NULL AND id IN (SELECT value FROM json_each(:ids))"
RED = f"""
    SELECT s.subject_key FROM subjects s
    JOIN ci_checks c ON c.repo = lower(s.repo) AND c.sha = lower(s.head_sha) AND c.received_at >= :since
    WHERE s.subject_key IN (SELECT value FROM json_each(:prs))
    GROUP BY s.subject_key
    HAVING sum(c.verdict = '{Verdict.PENDING}') = 0
       AND sum(c.verdict IN ('{Verdict.FAILURE}', '{Verdict.CANCELLED}')) > 0
"""
"""The PRs among :prs whose head's CI ended failed, as ci.status_of says: nothing pending, one failed."""


class Unknown(ValueError):
    """An outcome named neither by an id, a ref, nor with the text a new one-off outcome needs."""


def label(outcome: dict[str, Any]) -> str:
    """How the OUTCOME column names an outcome: its RT#N or owner/repo#N, else its short name."""
    return str(outcome["subject_key"] or outcome["task"][:LABEL])


def _is_pr(conn: sqlite3.Connection, name: str, key: str) -> bool:
    """A PR is a story's `pr`, never an outcome: its URL says so, or an item has it as its pr or
    records it from a PR's URL."""
    found = store.item_by_subject(conn, key) or {}
    return (
        "/pull/" in name
        or "/pull/" in (found.get("ref") or "")
        or bool(conn.execute(IS_PR, (key,)).fetchone())
    )


def _reopened(conn: sqlite3.Connection, outcome: str, state: str, actor: str) -> str:
    """The outcome's id, reopened first if it was done: new work serves it again."""
    if state == State.DONE:
        store.update_item(conn, outcome, {"state": State.WORKING, "result": None}, actor, "outcome")
    return outcome


def resolve(conn: sqlite3.Connection, name: str | None, text: str | None, actor: str) -> str:
    """The id of the outcome `name` means, recording it if new; the caller holds the transaction.
    `name` is an outcome's id, or a ref (RT#N, an issue's URL or owner/repo#N) for the outcome item
    of kind outcome that names it, recorded if there is none: a story or intake row holding that key
    is never taken for it. With `text`, anything else is the short name of a new one-off outcome, and
    with `text` alone the name is its start. Raises Unknown for a PR, blank text, or an unknown name
    without text."""
    name = (name or "").strip()
    if text is not None and not text.strip():
        raise Unknown("an outcome's instruction text cannot be blank")
    key = subject_key(name)
    match store.get_item(conn, name) if name else None, key:
        case {"kind": Kind.OUTCOME, "id": str() as found, "state": str() as state}, _:
            return _reopened(conn, found, state, actor)
        case _, str() if _is_pr(conn, name, key):
            raise Unknown(f"{key} is a PR: set it as the story's pr, and name the issue or ticket it serves")
        case _, str():
            known = conn.execute(OUTCOME_BY_KEY, {"key": key}).fetchone()
            # The key may be held by a story or an intake row, so the outcome carries it as task and ref.
            minted = {"kind": Kind.OUTCOME, "task": key, "ref": key}
        case _, None if text:
            known = None
            minted = {
                "kind": Kind.OUTCOME,
                "task": name or " ".join(text.split())[:LABEL],
                "note": text.strip(),
            }
        case _:
            raise Unknown(f"no outcome {name!r}: name an outcome's id or ref, or give its instruction text")
    if known is not None:
        return _reopened(conn, known["id"], known["state"], actor)
    return store.insert_item(
        conn, {**minted, "mode": Mode.AUTO, "state": State.WORKING}, actor, source="outcome"
    )


def close_finished(conn: sqlite3.Connection) -> list[str]:
    """Close each open auto outcome no seat holds once it has stories and all of them are closed, from
    a read, as subjects.hygiene settles: one lookup, then a transaction only when one is due."""
    due = [row[0] for row in conn.execute(FINISHED)]
    if due:
        with store.transaction(conn):
            for outcome in due:
                store.update_item(
                    conn, outcome, {"state": State.DONE, "result": "all its stories closed"}, ACTOR, "outcome"
                )
    return due


def attach(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Give each row its `outcome` and `outcome_label`: its parent, else the outcome of the story whose
    PR it is, else that of the story it was filed under. Three queries, each over only what the rows
    name: the stories whose pr is a row's subject, the stories rows were filed under, their outcomes."""
    keys = json.dumps(sorted({row["subject_key"] for row in rows if row.get("subject_key")}))
    by_pr = {row["pr"]: row["parent"] for row in conn.execute(STORIES, {"keys": keys})}
    under = json.dumps(sorted({row["filed_under"] for row in rows if row.get("filed_under")}))
    filed = {row["id"]: row["parent"] for row in conn.execute(FILED, {"ids": under})}
    for row in rows:
        row["outcome"] = (
            row.get("parent") or by_pr.get(row.get("subject_key")) or filed.get(row.get("filed_under"))
        )
    wanted = json.dumps(sorted({row["outcome"] for row in rows if row["outcome"]}))
    names = {row["id"]: label(dict(row)) for row in conn.execute(OUTCOMES, {"ids": wanted})}
    for row in rows:
        row["outcome_label"] = names.get(row["outcome"])


def _clock(stamp: str) -> str:
    """HH:MM of a Desk time."""
    return stamp[11:16]


def step(row: dict[str, Any], now: str, red: frozenset[str] = frozenset()) -> str | None:
    """NEXT: the one next step. A blocker, else a landed PR to wrap, else an overdue check, else a PR
    in `red` (its CI failed), else the item's own next, else its result."""
    if row["state"] == State.BLOCKED:
        return f"blocked: {row['blocked_reason']}" if row.get("blocked_reason") else "blocked"
    if row.get("landed_at") and row["state"] not in CLOSED:
        return "landed: wrap it"
    after = row.get("check_after")
    if after and after < now and row["state"] in (State.ASSIGNED, State.WORKING):
        return f"overdue since {_clock(after)}"
    if row.get("pr") in red:
        return f"{row['pr']} CI red"
    return row.get("next") or row.get("result")


def needs_you(row: dict[str, Any]) -> bool:
    """Only Scott can move it: his blocked work, or a subject held for his approval."""
    blocked = row["mode"] == Mode.MANUAL and row["state"] == State.BLOCKED
    held = (
        row["state"] == State.QUEUED and not row.get("seat") and row.get("extra", {}).get("trusted") is False
    )
    return blocked or held


def arrange(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows with their outcome, NEXT and needs-you flag, grouped by outcome: groups holding
    needs-you work first, then by their newest row; work serving no outcome last. Within a group,
    needs-you rows, then blocked, then newest, as the check table came."""
    attach(conn, rows)
    now = datetime.now(UTC)
    prs = json.dumps(sorted({row["pr"] for row in rows if row.get("pr")}))
    since = (now - store.CI_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
    red = frozenset(row[0] for row in conn.execute(RED, {"prs": prs, "since": since}))
    for row in rows:
        row["step"] = step(row, now.strftime("%Y-%m-%dT%H:%M:%SZ"), red)
        row["needs_you"] = needs_you(row)
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["outcome"], []).append(row)
    for members in groups.values():
        members.sort(key=lambda row: not row["needs_you"])

    newest = sorted(groups, key=lambda key: max(row["updated_at"] for row in groups[key]), reverse=True)
    rank = {key: place for place, key in enumerate(newest)}
    order = sorted(
        groups, key=lambda key: (key is None, not any(row["needs_you"] for row in groups[key]), rank[key])
    )
    return [row for key in order for row in groups[key]]
