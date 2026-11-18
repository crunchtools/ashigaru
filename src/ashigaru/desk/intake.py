"""Intake: a normalized IntakeItem from any adapter, upserted into the queue on its subject_key.

A new subject becomes a queued item whose task names the subject and never
quotes it: the source's title is data under `extra`, not a brief a seat is told
to follow. An item is claimable (mode auto) only when its author may write to
the repo, with the lane hint as its lane, or once a maintainer approves it: an
event that adds a lane (the adapter says which) from a sender who may write.
Anything else, including an edit, reopen or comment by a writer, leaves it
held, no lane and mode manual, for Josui to assign; who approved, and when, is
recorded under `extra`. Otherwise later deliveries never touch what Josui owns
(task, lane, mode, state, seat): they refresh the ref, and the source's title,
labels and open or closed state under `extra`. A subject first seen closed is
not queued.
"""

import sqlite3
from typing import Any

from . import store
from .forges import IntakeItem
from .model import Kind, Lane, Mode, State

ACTOR = "intake"
SOURCE = "webhook"


def held(existing: dict[str, Any]) -> bool:
    """An intake item still waiting for a maintainer's approval."""
    return existing["extra"].get("trusted") is False


def lane(hint: str | None) -> str | None:
    """The hint when it names one of the Desk's lanes, else None: a source cannot invent a lane."""
    return hint if hint in {known.value for known in Lane} else None


def task_for(item: IntakeItem) -> str:
    """The item's task: what to do with the subject, in the Desk's words, never the source's."""
    verb = "Triage" if item.kind == Kind.OUTCOME else "Review"
    return f"{verb} {item.subject_key} (from {item.source}; read it at the ref)"


def _seen(item: IntakeItem) -> dict[str, Any]:
    return {
        "source": item.source,
        "title": item.title,
        "labels": list(item.labels),
        "source_state": "closed" if item.closed else "open",
    }


def _approval(item: IntakeItem) -> dict[str, Any]:
    return {"approved_by": item.sender, "approved_at": store.now(), "approved_lane": lane(item.approves_lane)}


def upsert(conn: sqlite3.Connection, item: IntakeItem) -> str | None:
    """The id of the item the subject maps to, or None when it was closed on first sight. The caller
    holds the transaction; an item is written only when something changed."""
    existing = store.item_by_subject(conn, item.subject_key)
    if existing is None:
        if item.closed:
            return None
        return store.insert_item(
            conn,
            {
                "kind": item.kind,
                "subject_key": item.subject_key,
                "task": task_for(item),
                "ref": item.ref,
                "repo": item.repo,
                "lane": lane(item.lane_hint)
                if item.author_writes
                else lane(item.approves_lane)
                if item.approved
                else None,
                "mode": Mode.AUTO if item.author_writes or item.approved else Mode.MANUAL,
                "extra": {
                    **_seen(item),
                    "author": item.author,
                    "trusted": item.author_writes or item.approved,
                    **(_approval(item) if item.approved and not item.author_writes else {}),
                },
            },
            ACTOR,
            SOURCE,
        )
    patch: dict[str, Any] = {}
    extra = {**existing["extra"], **_seen(item)}
    if item.approved and held(existing):
        extra.update(trusted=True, **_approval(item))
        if existing["state"] == State.QUEUED and existing["seat"] is None:
            patch.update(lane=lane(item.approves_lane), mode=Mode.AUTO)
    if extra != existing["extra"]:
        patch["extra"] = extra
    if item.ref != existing["ref"]:
        patch["ref"] = item.ref
    if patch:
        store.update_item(conn, existing["id"], patch, ACTOR, SOURCE)
    return str(existing["id"])
