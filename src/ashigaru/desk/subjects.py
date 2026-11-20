"""The forge's own state of each subject, and what the Desk does with it on its own.

A webhook delivery records its subject here (open, closed or merged; head,
draft, review, title, labels) in the same transaction as intake, then settles
every item whose subject_key or pr names it. A merged change never goes back to
open or closed, and an older delivery (by the forge's `updated_at`) never
overwrites a newer one.
A subject intake did not queue because a seat may have opened it carries `unlinked_at`
(intake.unlinked lists those no item names); recording never touches it.

Settling never touches what a person holds. A seat's item whose subject or PR
merged is marked `landed_at` and waits for its wrap. A seatless item nobody has
started (auto, or manual still queued or proposed) is closed: done when its
subject merged (or, for an issue, closed), wontdo when its PR closed unmerged.
One settling closed (by the event history, not by `verified_by`, which others
may write) is queued again when its own subject reopens and nothing else it
names is closed or merged. A manual item Scott has started is his and is only
ever marked landed. A PR named in `pr` only lands or closes an item on merge; a
PR closed unmerged says nothing about the story it carried.
"""

import json
import sqlite3
from typing import Any

from . import store
from .forges import IntakeItem
from .model import Kind, Mode, State, SubjectState

ACTOR = "desk"
SOURCE = "subject"

UPSERT = """
    INSERT INTO subjects (
        subject_key, repo, kind, state, head_sha, draft, review, title, author, labels, source,
        source_updated_at, seen_at
    ) VALUES (
        :subject_key, :repo, :kind, :state, :head_sha, :draft, :review, :title, :author, :labels, :source,
        :source_updated_at, :seen_at
    )
    ON CONFLICT (subject_key) DO UPDATE SET
        repo = excluded.repo, kind = excluded.kind, state = excluded.state, head_sha = excluded.head_sha,
        draft = excluded.draft, review = excluded.review, title = excluded.title, author = excluded.author,
        labels = excluded.labels, source = excluded.source, source_updated_at = excluded.source_updated_at,
        seen_at = excluded.seen_at
"""

OPEN = "coalesce(i.wrap, '') != 'cleared' AND i.state NOT IN ('done', 'wontdo', 'abandoned')"
PARKED = "i.seat IS NULL AND (i.mode = 'auto' OR i.state IN ('proposed', 'queued'))"
"""Nobody holds the item: settling closes it, as nobody would. A manual one waiting for Scott counts;
one he has started (assigned, working, blocked, review) is his, and is only marked landed."""
UNHELD = (State.PROPOSED, State.QUEUED)
CLOSED_HERE = """(
    SELECT e.actor || ' ' || e.source FROM events e
    WHERE e.item_id = i.id AND json_type(e.patch, '$.state') IS NOT NULL
    ORDER BY e.seq DESC LIMIT 1
) = :actor || ' ' || :source"""
"""The item's state was last set by settling, not by anyone who can also write `verified_by`."""
DUE = f"""
    SELECT i.*, s.subject_key AS due_subject, s.kind AS due_kind, s.state AS due_state,
           s.head_sha AS due_head
    FROM subjects s JOIN items i ON s.subject_key IN (i.subject_key, i.pr)
    WHERE (:key IS NULL OR s.subject_key = :key)
      AND (
          {OPEN} AND s.state = 'merged' AND NOT ({PARKED}) AND i.landed_at IS NULL
          OR {OPEN} AND {PARKED}
              AND (s.state = 'merged' OR s.state = 'closed' AND s.subject_key = i.subject_key)
          OR i.state IN ('done', 'wontdo') AND coalesce(i.wrap, '') != 'cleared'
              AND s.state = 'open' AND s.subject_key = i.subject_key AND {CLOSED_HERE}
              AND NOT EXISTS (
                  SELECT 1 FROM subjects o WHERE o.subject_key IN (i.subject_key, i.pr) AND o.state != 'open'
              )
      )
    ORDER BY i.id, s.state = 'merged' DESC, s.subject_key
"""
"""Exactly the items a subject's state moves, so a read that finds none writes nothing: a seat's or
Scott's open item whose subject or PR merged and is not yet marked landed; a PARKED item whose
subject or PR merged, or whose own subject closed; and one settling closed whose own subject is open
again while nothing else it names is closed or merged. An item with two subjects due comes once per
subject, its merged one first, and settles by that one."""


def get(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM subjects WHERE subject_key = ?", (key,)).fetchone()
    if row is None:
        return None
    found = dict(row)
    found["labels"] = json.loads(found["labels"])
    found["draft"] = None if found["draft"] is None else bool(found["draft"])
    return found


def record(conn: sqlite3.Connection, item: IntakeItem, source: str) -> bool:
    """Store what a delivery says of its subject; the caller holds the transaction. False when the
    stored row is newer by the forge's clock, so nothing changed."""
    known = get(conn, item.subject_key) or {}
    stored_at = known.get("source_updated_at")
    if item.updated_at and stored_at and item.updated_at < stored_at:
        return False
    if item.merged or known.get("state") == SubjectState.MERGED:
        state = SubjectState.MERGED
    else:
        state = SubjectState.CLOSED if item.closed else SubjectState.OPEN
    draft = item.draft if item.draft is not None else known.get("draft")
    conn.execute(
        UPSERT,
        {
            "subject_key": item.subject_key,
            "repo": item.repo,
            "kind": item.kind,
            "state": state,
            "head_sha": item.head or known.get("head_sha"),
            "draft": None if draft is None else int(draft),
            "review": item.review or known.get("review"),
            "title": item.title,
            "author": item.author,
            "labels": json.dumps(list(item.labels)),
            "source": source,
            "source_updated_at": item.updated_at or stored_at,
            "seen_at": store.now(),
        },
    )
    return True


def transition(due: dict[str, Any]) -> dict[str, Any]:
    """The patch a DUE row makes to its item: see the module's rules."""
    if due["state"] in (State.DONE, State.WONTDO):
        return {"state": State.QUEUED, "result": None, "verified_by": None, "closed_at": None}
    if due["seat"] is not None or due["mode"] != Mode.AUTO and due["state"] not in UNHELD:
        return {"landed_at": store.now()}
    if due["due_state"] == SubjectState.MERGED:
        state, why = State.DONE, f"merged at {(due['due_head'] or '')[:12]}".strip()
    elif due["due_kind"] == Kind.OUTCOME:
        state, why = State.DONE, "closed at the source"
    else:
        state, why = State.WONTDO, "closed unmerged at the source"
    return {"state": state, "result": due["result"] or why, "verified_by": ACTOR}


def settle(conn: sqlite3.Connection, key: str | None = None) -> list[str]:
    """Apply each due transition, for one subject or all, once per item; the caller holds the
    transaction. Returns the ids that changed."""
    moved: dict[str, None] = {}
    for row in conn.execute(DUE, {"key": key, "actor": ACTOR, "source": SOURCE}).fetchall():
        if row["id"] not in moved:
            store.update_item(conn, row["id"], transition(store.decode(row)), ACTOR, SOURCE)
            moved[row["id"]] = None
    return list(moved)


def hygiene(conn: sqlite3.Connection) -> list[str]:
    """Settle every subject, from a read, as `expire_proposed` does: one lookup, then a transaction
    only when something is due. It catches a transition a webhook could not make (a rule added later,
    or an item linked to its PR after the merge)."""
    if conn.execute(DUE, {"key": None, "actor": ACTOR, "source": SOURCE}).fetchone() is None:
        return []
    with store.transaction(conn):
        return settle(conn)
