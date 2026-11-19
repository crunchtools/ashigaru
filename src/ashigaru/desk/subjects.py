"""The forge's own state of each subject, and what the Desk does with it on its own.

A webhook delivery records its subject here (open, closed or merged; head,
draft, review, title, labels) in the same transaction as intake, then settles
every item whose subject_key or pr names it. A merged change never goes back to
open or closed, and an older delivery (by the forge's `updated_at`) never
overwrites a newer one.

Settling never touches what a person holds. A seat's item whose subject or PR
merged is marked `landed_at` and waits for its wrap. A seatless auto item is
closed: done when its subject merged (or, for an issue, closed), wontdo when its
PR closed unmerged. One the Desk closed is queued again when its subject
reopens. A manual item is Scott's and is only ever marked landed. A PR named in
`pr` only lands or closes an item on merge; a PR closed unmerged says nothing
about the story it carried.
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

DUE = """
    SELECT i.id, s.subject_key AS subject, s.kind AS subject_kind, s.state AS subject_state,
           s.head_sha AS subject_head
    FROM subjects s JOIN items i ON s.subject_key IN (i.subject_key, i.pr)
    WHERE (:key IS NULL OR s.subject_key = :key)
      AND (
          coalesce(i.wrap, '') != 'cleared' AND i.state NOT IN ('done', 'wontdo', 'abandoned')
              AND s.state != 'open' AND (i.landed_at IS NULL OR i.seat IS NULL AND i.mode = 'auto')
          OR i.state IN ('done', 'wontdo') AND i.verified_by = 'desk' AND s.state = 'open'
      )
    ORDER BY i.id
"""
"""Items a subject's state may move: open ones whose subject closed or merged (a seat's, until it is
marked landed), and ones the Desk closed whose subject is open again."""


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


def transition(item: dict[str, Any], due: dict[str, Any]) -> dict[str, Any]:
    """The patch a subject's state makes to one item, or {}: see the module's rules."""
    merged, via_pr = due["subject_state"] == SubjectState.MERGED, due["subject"] != item["subject_key"]
    if item["state"] in (State.DONE, State.WONTDO):
        return (
            {} if via_pr else {"state": State.QUEUED, "result": None, "verified_by": None, "closed_at": None}
        )
    if item["seat"] is not None or item["mode"] != Mode.AUTO:
        return {"landed_at": store.now()} if merged and not item["landed_at"] else {}
    if via_pr and not merged:
        return {}
    if merged:
        state, why = State.DONE, f"merged at {(due['subject_head'] or '')[:12]}".strip()
    elif due["subject_kind"] == Kind.OUTCOME:
        state, why = State.DONE, "closed at the source"
    else:
        state, why = State.WONTDO, "closed unmerged at the source"
    return {"state": state, "result": item["result"] or why, "verified_by": ACTOR}


def settle(conn: sqlite3.Connection, key: str | None = None) -> list[str]:
    """Apply each due transition, for one subject or all; the caller holds the transaction. Returns
    the ids that changed."""
    moved = []
    for due in [dict(row) for row in conn.execute(DUE, {"key": key})]:
        item = store.get_item(conn, due["id"])
        patch = transition(item, due) if item else {}
        if patch:
            store.update_item(conn, due["id"], patch, ACTOR, SOURCE)
            moved.append(due["id"])
    return moved


def hygiene(conn: sqlite3.Connection) -> list[str]:
    """Settle every subject, from a read, as `expire_proposed` does: one lookup, then a transaction
    only when something is due. It catches a transition a webhook could not make (a rule added later,
    or an item linked to its PR after the merge)."""
    if conn.execute(DUE, {"key": None}).fetchone() is None:
        return []
    with store.transaction(conn):
        return settle(conn)
