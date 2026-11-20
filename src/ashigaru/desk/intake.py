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
closed state under `extra`. A subject first seen closed is not queued, nor is a bot's that is not one of
the trusted authors (dependabot's PRs are its own to land), nor is one of the record issues
the sweep keeps (RECORDS): the inbox and the red issue carry strangers' text, and their author, the
app's bot, would make them trusted and claimable. One queued before that is held on its next delivery,
for good: nothing it carries can trust it again.

A subject an item already names in `pr` is never queued again, and a writer's PR is linked, not queued,
when it names an open item of its repo that has no `pr` (`link`): by a body line `Ashigaru-Item: aN`,
else its head branch `aN/...` or `aN-...`, else `(aN)` in its title or body; the item gets the PR as its
`pr`. A stranger's PR links nothing. A writer's PR that names nothing, in a repo where a seat holds an
open item with no `pr`, is not queued either (a bot's still is): a seat opened it as the writer, and
Josui links it (`unlinked` lists them, until an item names one). The same rules serve a webhook and the
sweep's search. A delivery older than the last one seen by the forge's `updated_at` leaves the source's
title, labels, state and ref as they were.
"""

import dataclasses
import re
import sqlite3
from typing import Any

from . import store, subjects
from .forges import IntakeItem
from .model import CLOSED, Kind, Lane, Mode, State, Wrap

ACTOR = "intake"
SOURCE = "webhook"
RECORDS = frozenset({"ashigaru-inbox", "ashigaru-releases", "ashigaru-red", "ashigaru-observe"})
"""The labels of the issues the sweep keeps as records (scripts/common.py), which are never work."""
TRAILER = re.compile(r"^Ashigaru-Item:[ \t]*(a\d+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
BRANCH = re.compile(r"^(a\d+)[/-]", re.IGNORECASE)
PARENTHESIZED = re.compile(r"\((a\d+)\)", re.IGNORECASE)
REPO_OF = """lower(coalesce(repo, CASE WHEN instr(subject_key, '#') > 1
    THEN substr(subject_key, 1, instr(subject_key, '#') - 1) END))"""
AWAITING = f"""
    SELECT 1 FROM items
    WHERE seat IS NOT NULL AND pr IS NULL AND {store.OPEN} AND {REPO_OF} = lower(:repo) LIMIT 1
"""
"""A seat holds an open item in the repo that has no PR yet: a writer's PR there may be the seat's."""
MARK_UNLINKED = "UPDATE subjects SET unlinked_at = coalesce(unlinked_at, :at) WHERE subject_key = :key"
UNLINKED = """
    SELECT s.subject_key, s.repo, s.title, s.author, s.unlinked_at AS since FROM subjects s
    WHERE s.state = 'open' AND s.unlinked_at IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM items i WHERE s.subject_key IN (i.subject_key, i.pr)
    )
    ORDER BY s.unlinked_at, s.subject_key
"""
"""Open subjects a writer's seat may have opened that no item names: NEEDS YOU's to link."""


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
        **({"source_updated_at": item.updated_at} if item.updated_at else {}),
    }


def _older(existing: dict[str, Any], item: IntakeItem) -> bool:
    """The delivery is older, by the forge's clock, than the last one the item took."""
    stored = existing["extra"].get("source_updated_at")
    return bool(item.updated_at and stored and item.updated_at < stored)


def by_pr(conn: sqlite3.Connection, key: str) -> str | None:
    """The id of an item, open or closed, whose `pr` is the subject."""
    row = conn.execute("SELECT id FROM items WHERE pr = ? LIMIT 1", (key,)).fetchone()
    return str(row[0]) if row else None


def _repo(found: dict[str, Any]) -> str:
    """The item's repo: its column, else the repo part of its subject_key."""
    owner_repo = (found["subject_key"] or "").partition("#")[0]
    return (found["repo"] or (owner_repo if "/" in owner_repo else "")).lower()


def _named(item: IntakeItem) -> list[str]:
    """The item ids a PR names, in the order they count: trailer lines, head branch, then `(aN)`."""
    branch = BRANCH.match(item.head_ref or "")
    found = [
        *TRAILER.findall(item.body),
        *([branch[1]] if branch else []),
        *PARENTHESIZED.findall(f"{item.title}\n{item.body}"),
    ]
    return [name.lower() for name in found]


def link(conn: sqlite3.Connection, item: IntakeItem) -> str | None:
    """The id of the item the subject belongs to: the one whose `pr` it already is, else (for a PR
    whose author the Desk judged a writer, never a stranger's) the first item `_named` that is open, has
    no `pr` and is in the PR's repo."""
    known = by_pr(conn, item.subject_key)
    if known or item.kind != Kind.STORY or not item.author_writes:
        return known
    for name in _named(item):
        found = store.get_item(conn, name)
        if (
            found
            and _repo(found) == (item.repo or "").lower()
            and not found["pr"]
            and found["state"] not in CLOSED
            and found["wrap"] != Wrap.CLEARED
        ):
            return name
    return None


def unlinked(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The open subjects a seat may have opened that no item names, oldest first."""
    return [dict(row) for row in conn.execute(UNLINKED)]


def _awaiting(conn: sqlite3.Connection, item: IntakeItem) -> bool:
    """A writer's open PR, in a repo where a seat holds an open item with no `pr`, that names none. A
    bot's (the app's own fixes) is never a seat's: seats push as a person."""
    person = not (item.author or "").endswith("[bot]")
    mine = item.kind == Kind.STORY and item.author_writes and person and not item.closed and item.repo
    return bool(mine and conn.execute(AWAITING, {"repo": item.repo}).fetchone())


def _mark_unlinked(conn: sqlite3.Connection, item: IntakeItem) -> None:
    """Record the subject, which the delivery's own record may refuse as older, and flag it."""
    subjects.record(conn, item, SOURCE)
    conn.execute(MARK_UNLINKED, {"at": store.now(), "key": item.subject_key})


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


def _untrusted_bot(item: IntakeItem) -> bool:
    """A bot's subject whose author is not trusted: the app's own bot is, by ASHIGARU_FORGE_AUTHORS."""
    return bool(item.author and item.author.endswith("[bot]")) and not item.author_writes


def upsert(conn: sqlite3.Connection, item: IntakeItem) -> str | None:
    """The id of the item the subject maps to, or None when it was closed or a record on first sight.
    The caller holds the transaction; an item is written only when something changed."""
    existing = store.item_by_subject(conn, item.subject_key)
    if existing is None:
        if (linked := link(conn, item)) is not None:
            if not (store.get_item(conn, linked) or {}).get("pr"):
                store.update_item(conn, linked, {"pr": item.subject_key}, ACTOR, SOURCE)
            return linked
        if item.closed or RECORDS & set(item.labels) or _untrusted_bot(item):
            return None
        if _awaiting(conn, item):
            _mark_unlinked(conn, item)
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
    older = _older(existing, item)
    extra = {**existing["extra"], **({} if older else _seen(item))}
    if RECORDS & set(item.labels):
        patch.update(_unrecord(conn, existing, item, extra))
    else:
        patch.update(_trust(conn, existing, item, extra))
    if extra != existing["extra"]:
        patch["extra"] = extra
    if item.ref != existing["ref"] and not older:
        patch["ref"] = item.ref
    if patch:
        store.update_item(conn, existing["id"], patch, ACTOR, SOURCE)
    return str(existing["id"])
