"""Intake: a normalized IntakeItem from any adapter, upserted into the queue on its subject_key.

A new subject becomes a queued item whose task names the subject and never
quotes it: the source's title is data under `extra`, not a brief a seat is told
to follow. An item is claimable (mode auto) only when its author may write to
the repo, with the lane hint as its lane, or once a maintainer approves it: an
event that adds a lane (the adapter says which) from a sender who may write.
Anything else, including an edit, reopen or comment by a writer, leaves it
held, no lane and mode manual, for Josui to assign; who approved, when, and the
head it was approved at are recorded under `extra`. While held, the author is
asked about again, so a check that failed is retried. An approval is withdrawn
(held again) when a writer removes the approved lane, or when anyone who may not
write moves the head it was approved at; a writer moving it moves the approval.
Approval, retry and withdrawal change lane and mode only while the item is
queued and seatless and nobody but intake has set them: a hold Josui chose
wins. Otherwise later deliveries never touch what Josui owns (task, lane, mode,
state, seat): they refresh the ref, and the source's title, labels and open or
closed state under `extra`. A subject first seen closed is not queued, nor is one of the record issues
the sweep keeps (RECORDS): the inbox and the red issue carry strangers' text, and their author, the
app's bot, would make them trusted and claimable. One queued before that is held on its next delivery,
for good: nothing it carries can trust it again.
"""

import dataclasses
import sqlite3
from typing import Any

from . import store
from .forges import IntakeItem
from .model import Kind, Lane, Mode, State

ACTOR = "intake"
SOURCE = "webhook"
RECORDS = frozenset({"ashigaru-inbox", "ashigaru-releases", "ashigaru-red", "ashigaru-observe"})
"""The labels of the issues the sweep keeps as records (scripts/common.py), which are never work."""


def held(existing: dict[str, Any]) -> bool:
    """An intake item still waiting for a maintainer's approval."""
    return existing["extra"].get("trusted") is False


def trusted(existing: dict[str, Any]) -> bool:
    """An intake item that is claimable on its own merits: its author writes, or an approval stands."""
    return existing["extra"].get("trusted") is True


def withdrawn(existing: dict[str, Any]) -> bool:
    """An intake item whose trust was withdrawn: only a maintainer's approval restores it."""
    return "held_for" in existing["extra"]


def was_approved(existing: dict[str, Any]) -> bool:
    """An intake item a maintainer has approved, whether or not the approval still stands."""
    return "approved_by" in existing["extra"]


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
    return {
        "approved_by": item.sender,
        "approved_at": store.now(),
        "approved_lane": lane(item.approves_lane),
        "approved_head": item.head,
    }


def _authored(item: IntakeItem) -> dict[str, Any]:
    """A writer's own subject is tied to its head like an approval, so a push by anyone else holds it."""
    return {"approved_head": item.head} if item.head is not None else {}


def _held(item: IntakeItem) -> dict[str, Any]:
    """A held subject is tied to its head too: the author check is retried only at that head."""
    return {"held_head": item.head} if item.head is not None else {}


def _withdrawal(item: IntakeItem, why: str) -> dict[str, Any]:
    return {"trusted": False, "held_by": item.sender, "held_at": store.now(), "held_for": why}


def _ours(conn: sqlite3.Connection, existing: dict[str, Any]) -> bool:
    """Lane and mode are intake's to set: the item is queued and seatless, and nobody else has set
    either (Josui's hold, or Josui's lane, wins over a label)."""
    if existing["state"] != State.QUEUED or existing["seat"] is not None:
        return False
    return not store.steered_by_other(conn, existing["id"], ACTOR)


def _trust(
    conn: sqlite3.Connection, existing: dict[str, Any], item: IntakeItem, extra: dict[str, Any]
) -> dict[str, Any]:
    """Approve, retry or withdraw the existing item's trust in `extra`; the lane and mode that follow."""
    if held(existing) and (item.author_writes or item.approved):
        extra.update(trusted=True, **(_authored(item) if item.author_writes else _approval(item)))
        hint = item.lane_hint if item.author_writes else item.approves_lane
        return {"lane": lane(hint), "mode": Mode.AUTO} if _ours(conn, existing) else {}
    if not trusted(existing):
        if item.pushed_by_writer:
            extra.update(_held(item))
        return {}
    if item.revoked:
        why = "lane removed"
    elif item.head is not None and item.head != extra.get("approved_head") and not item.pushed_by_writer:
        why = "head moved"
    else:
        if item.head is not None:
            extra["approved_head"] = item.head
        return {}
    extra.update(_withdrawal(item, why))
    return {"lane": None, "mode": Mode.MANUAL} if _ours(conn, existing) else {}


def _unrecord(
    conn: sqlite3.Connection, existing: dict[str, Any], item: IntakeItem, extra: dict[str, Any]
) -> dict[str, Any]:
    """A record issue queued before RECORDS: held as withdrawn, so no author check or label trusts it."""
    if withdrawn(existing) and not trusted(existing):
        return {}
    extra.update(_withdrawal(item, "record issue"))
    return {"lane": None, "mode": Mode.MANUAL} if _ours(conn, existing) else {}


def _unless_moved(conn: sqlite3.Connection, existing: dict[str, Any], item: IntakeItem) -> IntakeItem:
    """The item, stripped of what its judging granted when the row has changed since it was judged."""
    if item.judged_at == store.last_event(conn, existing["id"]):
        return item
    return dataclasses.replace(item, author_writes=False, approved=False, pushed_by_writer=False)


def upsert(conn: sqlite3.Connection, item: IntakeItem) -> str | None:
    """The id of the item the subject maps to, or None when it was closed or a record on first sight.
    The caller holds the transaction; an item is written only when something changed."""
    existing = store.item_by_subject(conn, item.subject_key)
    if existing is None:
        if item.closed or RECORDS & set(item.labels):
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
                    **(
                        _authored(item)
                        if item.author_writes
                        else _approval(item)
                        if item.approved
                        else _held(item)
                    ),
                },
            },
            ACTOR,
            SOURCE,
        )
    item = _unless_moved(conn, existing, item)
    patch: dict[str, Any] = {}
    extra = {**existing["extra"], **_seen(item)}
    if RECORDS & set(item.labels):
        patch.update(_unrecord(conn, existing, item, extra))
    else:
        patch.update(_trust(conn, existing, item, extra))
    if extra != existing["extra"]:
        patch["extra"] = extra
    if item.ref != existing["ref"]:
        patch["ref"] = item.ref
    if patch:
        store.update_item(conn, existing["id"], patch, ACTOR, SOURCE)
    return str(existing["id"])
