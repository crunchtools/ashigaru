"""Dispatch on top of the store: assign, claim and wrap, usage and spend, the queue, seats, outcomes
and work views.

Assign and claim run in one BEGIN IMMEDIATE transaction each, so two callers
racing for one item or one seat get one winner and one Conflict.
"""

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from . import intake, store, subjects, sweep
from . import outcomes as served
from .model import (
    CLOSED,
    LEASE,
    LEASE_ACTIVE,
    SEAT_ROW_FIELDS,
    Billing,
    Lane,
    Mode,
    Role,
    SeatStatus,
    State,
    Turn,
    Wrap,
)

PICK_SEAT = """
    SELECT name FROM seats
    WHERE lane = :lane AND status = 'free' AND owner IS NULL AND turn = 'idle' AND lease_until >= :now
    ORDER BY since, name
    LIMIT 1
"""
"""The seat an assign picks: free, unheld, idle and up (its lease live), free longest."""

WAITING_ON = """
    SELECT value FROM json_each(:after)
    WHERE value NOT IN (SELECT id FROM items WHERE state = 'done')
"""

OUTCOMES = """
    SELECT o.id, o.task, o.ref, o.state,
           count(s.id) AS total,
           coalesce(sum(s.state = 'done'), 0) AS done,
           coalesce(sum(s.state = 'blocked'), 0) AS blocked,
           coalesce(sum(s.state NOT IN ('done', 'blocked', 'wontdo', 'abandoned')), 0) AS open
    FROM items o LEFT JOIN items s ON s.parent = o.id
    WHERE o.kind = 'outcome'
    GROUP BY o.id
    ORDER BY o.updated_at DESC, o.id DESC
"""

QUEUE_LIMIT = 1000
"""Items per list in the queue view, GET /v1/items's own cap; a longer queue is paged there."""

CAUGHT = """
    SELECT * FROM (
        SELECT e.seq AS event_seq, (SELECT min(seq) FROM events f WHERE f.item_id = e.item_id) AS first_seq,
               json_extract(e.patch, '$.extra.source_state') AS event_state,
               i.*
        FROM events e JOIN items i ON i.id = e.item_id
        WHERE e.seq > :after AND e.seq <= :until AND e.source = 'webhook'
          AND json_extract(i.extra, '$.trusted') IS NOT NULL
    ) c
    WHERE event_seq = first_seq
       OR CASE WHEN event_state = 'open' AND state IN ('done', 'wontdo', 'abandoned') THEN (
              SELECT json_extract(p.patch, '$.extra.source_state') FROM events p
              WHERE p.item_id = c.id AND p.seq < c.event_seq AND p.source = 'webhook'
                AND json_extract(p.patch, '$.extra.source_state') IS NOT NULL
              ORDER BY p.seq DESC LIMIT 1
          ) = 'closed' ELSE 0 END
    ORDER BY event_seq
    LIMIT :limit
"""
"""Intake's events after the cursor that the catch-up reports: an item's first (it is new), or the one
that flipped its source state from closed to open while the item is closed (it was reopened), so a
later delivery on an item closed here and open there is not reported again. `trusted` is set only by
intake, so an item the API created and a webhook later touched is not one. The history is read back
only for an open-at-the-source event on a closed item, so most events cost no lookup."""
CAUGHT_ONLY = ("event_seq", "first_seq", "event_state")
FROM_TIME = """
    SELECT coalesce(
        (SELECT min(seq) FROM events WHERE at >= :since) - 1, (SELECT coalesce(max(seq), 0) FROM events)
    )
"""
"""The cursor for a time: just before the first event stamped at or after it, so an event stamped
late but committed early is read again rather than skipped."""
SWEEP = "sweep"
AUTODISPATCH = "autodispatch"
"""The mark that is the dispatch switch: `on` lets free seats claim queued auto work; off by default."""
NEXT_READY = f"""
    SELECT * FROM items
    WHERE {store.OPEN} AND state = 'queued' AND seat IS NULL AND mode = 'auto' AND lane = :lane
      AND NOT EXISTS (
          SELECT 1 FROM json_each(items.after)
          WHERE value NOT IN (SELECT id FROM items WHERE state = 'done')
      )
    ORDER BY created_at, id
    LIMIT 1
"""
"""What a free seat in the lane claims: the oldest queued auto item whose `after` items are done."""
CATCH_UP_DEFAULT = timedelta(days=1)

NEEDS_YOU = f"""
    SELECT * FROM items
    WHERE {store.OPEN} AND seat IS NULL AND state = 'queued' AND json_extract(extra, '$.trusted') = 0
       OR {store.OPEN} AND state = 'blocked' AND (mode = 'manual' OR seat IS NULL)
    ORDER BY state = 'blocked' DESC, updated_at DESC, id DESC
    LIMIT :limit
"""
"""Scott's blocked work and blocked work no seat holds (one cleared before its wrap), then the subjects
intake holds for a maintainer's approval."""

UNSEEN_SEATS = """
    SELECT name FROM identities
    WHERE role = :role AND disabled_at IS NULL AND name NOT IN (SELECT name FROM seats)
    ORDER BY name
"""

# Who pays for each lane's seats; a lane not here (prime, rule) bills nothing until a report names it.
LANE_BILLING = {Lane.VERTEX: Billing.VERTEX, Lane.DOLZA_MAX: Billing.MAX, Lane.EXEDORE_MAX: Billing.MAX}

ADD_SPEND = """
    INSERT INTO spend (billing, day, cost_usd) VALUES (:billing, :day, :cost)
    ON CONFLICT (billing, day) DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd
"""

SPEND = """
    SELECT b.billing,
           coalesce((SELECT cost_usd FROM spend WHERE billing = b.billing AND day = :day), 0) AS today_usd,
           coalesce((SELECT sum(cost_usd) FROM spend WHERE billing = b.billing AND day LIKE :month), 0)
               AS month_usd,
           l.daily_usd AS daily_limit_usd, l.monthly_usd AS monthly_limit_usd
    FROM (SELECT billing FROM spend UNION SELECT billing FROM billing_limits) b
    LEFT JOIN billing_limits l ON l.billing = b.billing
    ORDER BY b.billing
"""

SET_LIMITS = """
    INSERT INTO billing_limits (billing, daily_usd, monthly_usd, updated_at)
    VALUES (:billing, :daily_usd, :monthly_usd, :at)
    ON CONFLICT (billing) DO UPDATE SET
        daily_usd = excluded.daily_usd, monthly_usd = excluded.monthly_usd, updated_at = excluded.updated_at
"""


def _ready(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    """Raise Conflict unless the item is open and everything it waits on is done."""
    if item["state"] in CLOSED or item["wrap"] == Wrap.CLEARED:
        raise store.Conflict(f"{item['id']} is closed")
    waiting = [row[0] for row in conn.execute(WAITING_ON, {"after": json.dumps(item["after"])})]
    if waiting:
        raise store.Conflict(f"{item['id']} waits on {', '.join(waiting)}")


@dataclasses.dataclass(frozen=True)
class Own:
    """A request from the seat itself, in the session that sent `epoch` (None from a 4.x client)."""

    epoch: int | None = None


def live(seat: dict[str, Any]) -> bool:
    """The seat is up: a request from it renewed its lease, which has not run out."""
    return bool(seat.get("lease_until")) and seat["lease_until"] >= store.now()


def lease(turn: str | None) -> dict[str, Any]:
    """The lease a request from the seat renews: LEASE while idle, LEASE_ACTIVE mid-turn."""
    span = LEASE_ACTIVE if turn == Turn.ACTIVE else LEASE
    return {"lease_until": stamp(datetime.now(UTC) + span)}


def fence(seat: dict[str, Any] | None, name: str, epoch: int | None) -> None:
    """Raise Conflict (stale-session) when a request names an epoch that is not the seat's current
    one: the session that sent it has ended. A request naming none (a 4.x client) passes."""
    if epoch is not None and (seat or {}).get("epoch", 0) != epoch:
        current = (seat or {}).get("epoch", 0)
        raise store.Conflict(
            f"seat {name!r} is at epoch {current}; this session's ({epoch}) has ended: /clear to rejoin",
            "stale-session",
        )


def unmoved(item: dict[str, Any], version: int | None) -> None:
    """Raise Conflict (version) when the caller read the item at another version than it now has."""
    if version is not None and item["version"] != version:
        raise store.Conflict(
            f"{item['id']} moved: it is at version {item['version']}, not {version}; read it again", "version"
        )


def _free(seat: dict[str, Any] | None, name: str) -> dict[str, Any]:
    if seat is None:
        raise store.Conflict(f"seat {name!r} has not reported", "unseen")
    if seat["owner"]:
        raise store.Conflict(f"seat {name!r} is held by {seat['owner']}", "held")
    if seat["status"] != "free":
        raise store.Conflict(f"seat {name!r} is busy with {seat['item'] or 'something'}", "busy")
    if not live(seat):
        raise store.Conflict(f"seat {name!r} is missing: no word since {seat['updated_at']}", "missing")
    return seat


def _take(
    conn: sqlite3.Connection, item: dict[str, Any], seat: dict[str, Any], actor: str, source: str = "api"
) -> None:
    """The seat takes the item, in its current session's epoch; `source` says how (`next` for a claim
    the seat's next turn must acknowledge)."""
    fields = {"seat": seat["name"], "state": State.ASSIGNED, "epoch": seat["epoch"]}
    if item["lane"] is None and seat["lane"] is not None:
        fields["lane"] = seat["lane"]
    store.update_item(conn, item["id"], fields, actor, source)


def assign(
    conn: sqlite3.Connection, item_id: str, target: dict[str, Any], actor: str, version: int | None = None
) -> str:
    """Give the item to the target's seat, or to the longest-free unheld seat that is up in its lane
    (default the item's). Returns the seat."""
    seat, lane = target.get("seat"), target.get("lane")
    with store.transaction(conn):
        item = store.get_item(conn, item_id)
        if item is None:
            raise KeyError(item_id)
        unmoved(item, version)
        _ready(conn, item)
        lane = lane or item["lane"]
        if seat is None:
            if lane is None:
                raise store.Conflict(f"{item_id} has no lane to pick a seat by; name a seat or a lane")
            picked = conn.execute(PICK_SEAT, {"lane": lane, "now": store.now()}).fetchone()
            if picked is None:
                raise store.Conflict(f"no free seat in lane {lane}")
            seat = str(picked[0])
        row = _free(store.get_seat(conn, seat), seat)
        if lane is not None and item["lane"] is None:
            store.update_item(conn, item_id, {"lane": lane}, actor)
            item["lane"] = lane
        _take(conn, item, row, actor)
    return seat


def claim(
    conn: sqlite3.Connection, item_id: str, seat: str, epoch: int | None = None, version: int | None = None
) -> None:
    """desk_claim: a free seat takes a queued, unowned, auto item in its lane. Fails closed: a seat
    with no lane claims nothing, and an item with no lane is claimed by no seat; Josui sets its lane
    or assigns it. A session the Desk has ended claims nothing (`fence`)."""
    with store.transaction(conn):
        item = store.get_item(conn, item_id)
        if item is None:
            raise KeyError(item_id)
        fence(store.get_seat(conn, seat), seat, epoch)
        unmoved(item, version)
        _ready(conn, item)
        if item["state"] != State.QUEUED or item["seat"] is not None:
            raise store.Conflict(
                f"{item_id} is {item['state']} with {item['seat'] or 'no seat'}; not claimable"
            )
        if item["mode"] != "auto":
            raise store.Conflict(f"{item_id} is manual: Scott's to hand out")
        row = _free(store.get_seat(conn, seat), seat)
        if row["lane"] is None:
            raise store.Conflict(f"seat {seat!r} has no lane; Josui sets it before it can claim", "no-lane")
        if item["lane"] is None:
            raise store.Conflict(f"{item_id} has no lane; Josui sets it or assigns the item")
        if item["lane"] != row["lane"]:
            raise store.Conflict(f"{item_id} is in lane {item['lane']}, seat {seat!r} in {row['lane']}")
        _take(conn, item, row, seat, "claim")


def autodispatch(conn: sqlite3.Connection) -> bool:
    """The AUTODISPATCH switch: on hands queued auto items to free seats; anything else, nothing."""
    mark = store.get_mark(conn, AUTODISPATCH)
    return mark is not None and mark["value"] == "on"


def _waiting_seat(conn: sqlite3.Connection, seat: str, epoch: int | None = None) -> sqlite3.Row | None:
    """The item a free seat would claim now, or None to keep waiting: the switch is off, the seat is
    mid-turn (a brief it is reading arrived another way), its item is still wrapping, or nothing in
    its lane is ready. Raises Conflict when the seat cannot take work at all (busy, held, unseen or
    laneless, each with its code), or stale-session for a session the Desk has ended. /clear comes
    before `wrap` closes a wrapping item, so the hook /clear starts waits; a wrapping seat still
    mid-turn is not that race and is busy."""
    current = store.get_seat(conn, seat)
    fence(current, seat, epoch)
    item_id = current["item"] if current and not current["owner"] else None
    wrapping = item_id is not None and (store.get_item(conn, item_id) or {}).get("wrap") == Wrap.WRAPPING
    row = current if current and wrapping and current["turn"] == Turn.IDLE else _free(current, seat)
    if row["lane"] is None:
        raise store.Conflict(f"seat {seat!r} has no lane; Josui sets it before it can claim", "no-lane")
    if wrapping or row["turn"] != Turn.IDLE or not autodispatch(conn):
        return None
    found: sqlite3.Row | None = conn.execute(NEXT_READY, {"lane": row["lane"]}).fetchone()
    return found


def next_item(conn: sqlite3.Connection, seat: str, epoch: int | None = None) -> dict[str, Any] | None:
    """`POST /seats/{name}/next`: the free, idle seat claims the oldest queued auto item in its lane
    whose `after` items are done, or None to keep waiting. Raises Conflict when the seat cannot take
    work at all, so its hook stops asking. Reads first and takes the write lock only to claim, so a
    waiting seat's looks cost no lock while the switch is off or the lane is empty. The claim waits
    for the seat's next turn to acknowledge it (ACK_AFTER)."""
    if _waiting_seat(conn, seat, epoch) is None:
        return None
    with store.transaction(conn):
        found = _waiting_seat(conn, seat, epoch)
        if found is None:
            return None
        item = store.decode(found)
        _ready(conn, item)
        _take(conn, item, _free(store.get_seat(conn, seat), seat), seat, "next")
        return store.get_item(conn, item["id"])


def unclaim(conn: sqlite3.Connection, item_id: str, seat: str) -> None:
    """Undo a `next` claim whose client went away before the answer: the item goes back to the queue
    and the seat to free, if neither has moved since."""
    with store.transaction(conn):
        item = store.get_item(conn, item_id)
        row = store.get_seat(conn, seat)
        if item is None or row is None or item["seat"] != seat or item["state"] != State.ASSIGNED:
            return
        store.update_item(conn, item_id, {"seat": None, "state": State.QUEUED}, seat)


BRIEF_FIELDS = (("done_when", "Done when"), ("ref", "Ref"), ("repo", "Repo"), ("note", "Note"))
"""The item's fields a brief carries after its task, each only when set."""


BRIEF_LIMITS = """\
Don't merge, tag, release or deploy; Josui Prime does that. Don't message other seats.
Branch as `{id}/<short-slug>` and end each PR body with the line `Ashigaru-Item: {id}`, so the Desk \
links the PR to {id} instead of queuing it again.
Fan out with Sonnet subagents (Agent tool, model sonnet) wherever the work splits; you stay the integrator.
Report with `ashigaru report {id} <verb>`, and SendMessage to Josui Prime "DONE {id}: <result, links>" \
or "BLOCKED {id}: <what you need>", then "Filed: <urls or none>"; end with "Learned: <gotcha>" \
or "Learned: none". If you stop partway, report what is finished and what is left.
When Prime asks, run /cc seat and reply CC-DONE {id}.
Gatehouse: answer each finding with a reply whose first line is "Fixed in <sha>", \
"Not a bug: <20+ chars>" or "Deferred: <open issue URL>"; anything else leaves triage red, \
and a reply posted just before a push waits for the next retriage."""
"""The standing limits every brief ends with, as Josui Prime's hand briefs do; {id} is the item's id."""


def brief(item: dict[str, Any]) -> str:
    """What a woken seat reads: the item, its task, what else it says (BRIEF_FIELDS), and the limits
    and report contract every brief carries (BRIEF_LIMITS)."""
    said = "".join(f"{label}: {item[key]}\n" for key, label in BRIEF_FIELDS if item.get(key))
    said = f"{said}\n" if said else ""
    return (
        f"Desk assignment {item['id']} (lane {item['lane']}): the Ashigaru Desk claimed it for this seat "
        "through the seat's own `ashigaru hook next`. This is your work now, a brief from Josui Prime.\n\n"
        f"{item['task']}\n\n"
        f"{said}"
        f"{BRIEF_LIMITS.format(id=item['id'])}"
    )


def check_in(conn: sqlite3.Connection, name: str, epoch: int | None, turn: str | None = None) -> None:
    """A request from the seat itself: refused when its session has ended (`fence`), else proof the
    seat is up, which renews its lease. A seat's first request creates its row."""
    with store.transaction(conn):
        current = store.get_seat(conn, name)
        fence(current, name, epoch)
        store.write_seat(conn, name, lease(turn or (current or {}).get("turn")))


def _cleared(
    conn: sqlite3.Connection, held: dict[str, Any] | None, name: str, actor: str, source: str
) -> None:
    """The session holding `held` is gone (a /clear, or a new session started): nothing in the seat
    remembers the work. Scott's hold ends with a /clear, as it always has; a new session leaves his
    item open and seatless. Josui's item, unless it is wrapping (`wrap` settles that one), leaves the
    seat: blocked when it was under way, so NEEDS YOU shows it, else as it stands."""
    if held is None:
        return
    gone = f"seat {name} was {'cleared' if source == 'clear' else 'restarted'} before the wrap"
    if held["mode"] == Mode.MANUAL:
        fields: dict[str, Any] = {"wrap": Wrap.CLEARED} if source == "clear" else {"seat": None, "note": gone}
    elif held["wrap"] is None and held["state"] in (State.ASSIGNED, State.WORKING):
        fields = {"seat": None, "state": State.BLOCKED, "blocked_reason": f"{gone}: its context is gone"}
    elif held["wrap"] is None:
        fields = {"seat": None, "note": gone}
    else:
        return
    store.update_item(conn, held["id"], fields, actor, source=source)


def _prompted(
    conn: sqlite3.Connection, held: dict[str, Any] | None, seat: dict[str, Any], task: str | None, actor: str
) -> None:
    """A turn starting on the seat. Josui's item still `assigned` was not started: Scott's typed prompt
    (a task) hands it back to the queue; any other turn (the brief arriving) acknowledges it, so it is
    working. Scott's typed prompt on a seat then holding nothing opens his manual item, holding it."""
    if held and held["mode"] == Mode.AUTO and held["state"] == State.ASSIGNED:
        if task:
            back = {
                "seat": None,
                "state": State.QUEUED,
                "note": f"{seat['name']}: Scott typed before it started",
            }
            store.update_item(conn, held["id"], back, actor, source="prompt")
            held = None
        else:
            store.update_item(conn, held["id"], {"state": State.WORKING}, actor, source="ack")
    if task and held is None:
        work = {"task": task, "mode": Mode.MANUAL, "state": State.WORKING, "seat": seat["name"]}
        store.insert_item(
            conn, {**work, "lane": seat["lane"], "epoch": seat["epoch"]}, actor, source="prompt"
        )


def _report_seat(
    conn: sqlite3.Connection, name: str, fields: dict[str, Any], actor: str, own: Own | None
) -> dict[str, Any]:
    before = store.get_seat(conn, name)
    if own:
        fence(before, name, own.epoch)
        fields = {**fields, **lease(fields.get("turn") or (before or {}).get("turn"))}
    seat = store.write_seat(conn, name, fields)
    if not set(fields) - {"lease_until"} <= SEAT_ROW_FIELDS:
        return seat
    held = store.get_item(conn, before["item"]) if before and before["item"] else None
    if "task" in fields and fields["task"] is None:
        _cleared(conn, held, name, actor, "clear")
    elif fields.get("turn") == Turn.ACTIVE:
        _prompted(conn, held, seat, fields.get("task"), actor)
    return store.get_seat(conn, name) or seat


def report_seat(
    conn: sqlite3.Connection, name: str, fields: dict[str, Any], actor: str, own: Own | None = None
) -> dict[str, Any]:
    """PATCH /seats/{name}, and how the Desk sees what happens in a seat, which no seat can claim or drop.

    `own` is the seat reporting for itself: refused for an ended session (`fence`), and renewing its
    lease. A seat's turn starting acknowledges Josui's item it has not started, unless the turn is
    Scott's first typed prompt (turn active with a task): that hands the item back and makes the seat
    his, holding a new manual item with that task, the way /agents ledgered untracked manual work. A
    /clear (task back to null) ends what the session held (`_cleared`). Turn changes without a task
    (Claude Code's own turns: an assignment arriving, a background task's result) never start a hold.
    """
    with store.transaction(conn):
        return _report_seat(conn, name, fields, actor, own)


def end_session(conn: sqlite3.Connection, name: str, actor: str, why: str) -> list[str]:
    """End the seat's session, the caller holding the transaction: the next epoch, which no session
    holds, and no lease, so the seat is missing until a new session starts. Josui's work leaves the
    seat: queued again when it was assigned or working, else as it stands, each with `why` as its
    note. Scott's hold and a wrapping item stay for a person. Returns the items moved."""
    conn.execute("UPDATE seats SET epoch = epoch + 1, lease_until = NULL WHERE name = ?", (name,))
    moved = []
    for item in store.list_items(conn, seat=name, mode=Mode.AUTO):
        if item["state"] in CLOSED or item["wrap"] is not None:
            continue
        fields: dict[str, Any] = {"seat": None, "note": why}
        if item["state"] in (State.ASSIGNED, State.WORKING):
            fields["state"] = State.QUEUED
        store.update_item(conn, item["id"], fields, actor, source="session")
        moved.append(item["id"])
    return moved


def start_session(
    conn: sqlite3.Connection, name: str, session_id: str, source: str, actor: str
) -> tuple[int, dict[str, Any]]:
    """POST /seats/{name}/session: the seat's SessionStart. A new session (startup, clear) gets the
    next epoch, which ends the one before it (`_cleared`: its context is gone). A resume keeps the
    epoch when it is the seat's current session and its lease is live; else it is refused, Conflict
    (stale-session): what it remembers is no longer its own. A resume of the current session whose
    lease ran out ends it first (`end_session`), as reconcile would have. Returns the epoch and the
    seat."""
    with store.transaction(conn):
        current = store.get_seat(conn, name)
        resumed = source == "resume" and current is not None and current["epoch"] > 0
        if current and resumed and current["session_id"] == session_id and live(current):
            seat = _report_seat(conn, name, {"turn": Turn.IDLE}, actor, Own(current["epoch"]))
            return current["epoch"], seat
        if current and resumed and current["session_id"] == session_id:
            end_session(conn, name, actor, f"seat {name}'s session {session_id} lapsed")
        if not resumed:
            epoch = (current["epoch"] if current else 0) + 1
            same = current is not None and current["session_id"] == session_id
            fresh = {} if same else {"session_cost_usd": 0.0, "session_booked_usd": 0.0}
            store.write_seat(conn, name, {"epoch": epoch, "session_id": session_id, **fresh})
            held = store.get_item(conn, current["item"]) if current and current["item"] else None
            _cleared(conn, held, name, actor, "clear" if source == "clear" else "startup")
            fields = {"turn": Turn.IDLE, "task": None} if source == "clear" else {"turn": Turn.IDLE}
            return epoch, _report_seat(conn, name, fields, actor, Own(epoch))
    raise store.Conflict(
        f"seat {name!r}: session {session_id} has ended (the seat is at epoch "
        f"{(store.get_seat(conn, name) or {}).get('epoch')}); /clear to rejoin",
        "stale-session",
    )


def report_usage(
    conn: sqlite3.Connection,
    seat: str,
    report: dict[str, Any],
    lane_first: bool = False,
    epoch: int | None = None,
) -> dict[str, Any]:
    """Record a seat's usage and book whatever of the session's cost is not yet in spend. Billing is
    the report's, else the seat's lane's, else what the row had; with `lane_first` (a seat reporting
    for itself) the lane's comes before the report's, the session must be current (`fence`), and the
    report renews the seat's lease. Until a billing is known the cost waits."""
    with store.transaction(conn):
        found = store.get_seat(conn, seat)
        if lane_first:
            fence(found, seat, epoch)
        current = found or {**store.SEAT_DEFAULTS, "session_id": None}
        cost = report.pop("cost_usd", None)
        fields = {key: value for key, value in report.items() if value is not None}
        if lane_first:
            fields.update(lease(current["turn"]))
        lane = LANE_BILLING.get(current["lane"])
        reported = fields.get("billing")
        billing = ((lane or reported) if lane_first else (reported or lane)) or current["billing"]
        if billing:
            fields["billing"] = billing
        if cost is not None:
            same = current["session_id"] == report["session_id"]
            booked = current["session_booked_usd"] if same else 0.0
            fields["session_cost_usd"] = cost
            if billing:
                if cost > booked:
                    conn.execute(
                        ADD_SPEND, {"billing": billing, "day": store.now()[:10], "cost": cost - booked}
                    )
                fields["session_booked_usd"] = max(cost, booked)
            elif not same:
                fields["session_booked_usd"] = 0.0
        return store.write_seat(conn, seat, fields)


def _turn_seconds(seat: dict[str, Any], at: datetime) -> int | None:
    if seat["turn"] != "active" or not seat["turn_started_at"]:
        return None
    started = datetime.strptime(seat["turn_started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return int((at - started).total_seconds())


def usage(conn: sqlite3.Connection) -> dict[str, Any]:
    """`ashigaru usage`: one row per seat, and spend by billing source today and this month."""
    at = datetime.now(UTC)
    day = at.strftime("%Y-%m-%d")
    seats = [{**seat, "turn_seconds": _turn_seconds(seat, at)} for seat in store.list_seats(conn)]
    spend = [dict(row) for row in conn.execute(SPEND, {"day": day, "month": day[:7] + "-%"})]
    return {"seats": seats, "spend": spend}


def set_limits(conn: sqlite3.Connection, billing: str, limits: dict[str, Any]) -> None:
    conn.execute(SET_LIMITS, {"billing": billing, **limits, "at": store.now()})


def outcomes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """`ashigaru outcomes`: each outcome with its stories' done, blocked and open counts."""
    return [dict(row) for row in conn.execute(OUTCOMES)]


class Refused(Exception):
    """An action the item's state does not allow yet; nothing changed."""


def wrap(
    conn: sqlite3.Connection, item_id: str, request: dict[str, Any], actor: str, version: int | None = None
) -> None:
    """`wrap`, for the CLI and MCP alike. `wrapping` records that /cc seat was sent. `cleared` refuses
    (Refused) until the item is memorized or a Learned line comes with the request, and while its
    seat's turn is active, unless forced; then it clears the wrap and closes an open item, which
    frees the seat."""
    with store.transaction(conn):
        item = store.get_item(conn, item_id)
        if item is None:
            raise KeyError(item_id)
        unmoved(item, version)
        fields: dict[str, Any] = {"wrap": request["stage"]}
        if request.get("learned"):
            fields["learned"] = request["learned"]
        if request["stage"] == Wrap.CLEARED:
            if not request.get("force"):
                if not (item["memorized"] or request.get("learned")):
                    raise Refused(f"{item_id}: no CC-DONE yet; send /cc seat first, or force")
                seat = store.get_seat(conn, item["seat"]) if item["seat"] else None
                if item["seat"] and seat is None:
                    raise Refused(f"{item['seat']} never reported, so it cannot be seen idle; force to clear")
                if seat and seat["turn"] == Turn.ACTIVE:
                    raise Refused(f"{item['seat']} is mid-turn; wrap it once it is idle")
            if item["state"] not in CLOSED:
                fields["state"] = request["state"]
        store.update_item(conn, item_id, fields, actor, source="wrap")


def queue(conn: sqlite3.Connection) -> dict[str, Any]:
    """`ashigaru queue`: the newest QUEUE_LIMIT queued items, each with the state of every item it
    waits on (`unknown` for an id with no item), then as many proposed children with their expiry;
    `truncated` when either list was cut."""
    store.expire_proposed(conn)
    queued = store.list_items(conn, limit=QUEUE_LIMIT + 1, state=State.QUEUED)
    proposed = store.list_items(conn, limit=QUEUE_LIMIT + 1, state=State.PROPOSED)
    truncated = len(queued) > QUEUE_LIMIT or len(proposed) > QUEUE_LIMIT
    queued, proposed = queued[:QUEUE_LIMIT], proposed[:QUEUE_LIMIT]
    wanted = sorted({after for item in queued for after in item["after"]})
    states = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT id, state FROM items WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(wanted),)
        )
    }
    for item in queued:
        item["waits_on"] = [{"id": after, "state": states.get(after, "unknown")} for after in item["after"]]
    return {"queued": queued, "proposed": proposed, "truncated": truncated}


def seats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """`ashigaru agents`: every seat row, then every seat identity that never checked in. A seat is
    missing when it never did, or its lease has run out (`live`)."""
    rows = [{**seat, "missing": not live(seat)} for seat in store.list_seats(conn)]
    unseen = conn.execute(UNSEEN_SEATS, {"role": Role.SEAT})
    return rows + [{**store.SEAT_DEFAULTS, "name": row[0], "missing": True} for row in unseen]


def seat(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """One seat row, `missing` when its lease has run out."""
    found = store.get_seat(conn, name)
    return {**found, "missing": not live(found)} if found else None


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(value: str) -> str:
    """A time as the Desk stores it, so it compares as text, from one without seconds (`T14:00Z`) or
    with an offset. Raises ValueError for anything that is not a time."""
    parsed = datetime.fromisoformat(value)
    return stamp(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))


def _caught(item: dict[str, Any]) -> dict[str, Any]:
    extra = item["extra"]
    return {
        **{key: item[key] for key in ("id", "ref", "subject_key", "lane", "mode", "state")},
        "title": extra.get("title"),
        "filed_by": extra.get("author"),
        "held": extra.get("trusted") is False,
    }


def cursor(conn: sqlite3.Connection, since: str | None, name: str = SWEEP) -> tuple[int, str | None]:
    """Where a look at the events starts: just before the first event at `since`; else the mark
    `name` (the sweep mark unless a reader's own), an event seq an advance wrote or a time someone
    set; else a day back. Returns the seq and the time it was read from, if any. Raises ValueError
    for a time that is not one."""
    if since is None:
        mark = store.get_mark(conn, name)
        if mark and mark["value"].isdigit():
            return int(mark["value"]), None
        since = mark["value"] if mark else stamp(datetime.now(UTC) - CATCH_UP_DEFAULT)
    since = instant(since)
    return int(conn.execute(FROM_TIME, {"since": since}).fetchone()[0]), since


def window(
    conn: sqlite3.Connection, query: str, after: int, **params: Any
) -> tuple[list[sqlite3.Row], bool, int]:
    """`query`'s rows for the events after `after`, at most QUEUE_LIMIT, each with its `event_seq`;
    whether the limit cut them; and `until`, the last event read when it did, else the last committed
    when the read began, so an event that commits mid-read is the next look's."""
    last = int(conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])
    rows = conn.execute(query, {"after": after, "until": last, "limit": QUEUE_LIMIT + 1, **params}).fetchall()
    if len(rows) > QUEUE_LIMIT:
        return rows[:QUEUE_LIMIT], True, rows[QUEUE_LIMIT - 1]["event_seq"]
    return rows, False, max(last, after)


def catch_up(conn: sqlite3.Connection, since: str | None = None) -> dict[str, Any]:
    """The org sweep from the Desk's own data: intake's events after the cursor, as subjects first seen
    (`new`) and subjects reopened at the source while their item is closed (`reopened`), each item once.

    Events are numbered in commit order (one writer at a time), so `until`, the cursor `advance`
    stores, loses and repeats nothing: it is the last event read when QUEUE_LIMIT events cut the
    round short (`truncated`), else the last committed when the round began, so a delivery that
    commits mid-round is the next round's."""
    after, read_from = cursor(conn, since)
    rows, truncated, until = window(conn, CAUGHT, after)
    found: dict[str, dict[str, dict[str, Any]]] = {"new": {}, "reopened": {}}
    for row in rows:
        item = {key: value for key, value in store.decode(row).items() if key not in CAUGHT_ONLY}
        if row["event_seq"] == row["first_seq"]:
            found["new"].setdefault(item["id"], _caught(item))
        elif item["id"] not in found["new"]:
            found["reopened"].setdefault(item["id"], _caught(item))
    return {
        "since": read_from,
        "after": after,
        "until": until,
        **{name: list(items.values()) for name, items in found.items()},
        "truncated": truncated,
    }


def summary(conn: sqlite3.Connection, roster: list[dict[str, Any]]) -> dict[str, int]:
    """How many seats are up, of how many, and how many items are open."""
    return {
        "up": sum(not seat["missing"] for seat in roster),
        "seats": len(roster),
        "open": store.count_open(conn),
    }


def _needs(item: dict[str, Any]) -> dict[str, Any]:
    if item["state"] == State.BLOCKED:
        why, text = "blocked", item["blocked_reason"]
    else:
        subject = item["subject_key"] or item["ref"] or item["task"]
        author = item["extra"].get("author")
        by = f" by {author}" if author else ""
        why, text = "held", f"{subject}{by}: approve with a lane label or close"
    return {
        "why": why,
        "work": item["id"],
        "seat": item.get("seat"),
        "mode": item["mode"],
        "state": item["state"],
        "text": text,
        "task": item["task"],
        "outcome_label": item.get("outcome_label"),
        "since": item.get("updated_at"),
        **_detail(item),
    }


def _busy(seat: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    """A busy seat as a board row, with its work item (`{}` for none) and that work's outcome: Scott's
    hold is manual, else the work's mode."""
    return {
        "seat": seat["name"],
        "mode": "manual" if seat["owner"] else work.get("mode", "auto"),
        "work": seat["item"],
        "state": work.get("state"),
        "since": seat["since"],
        "active": seat["updated_at"],
        "turn": seat["turn"],
        "task": work.get("task") or seat["task"],
        "missing": seat["missing"],
        "outcome_label": work.get("outcome_label"),
        **_detail(work),
    }


def _detail(work: dict[str, Any]) -> dict[str, Any]:
    """The fields `work --wide` shows and NEXT, which a board row carries beside its own."""
    return {
        "pr": work.get("pr"),
        "done_when": work.get("done_when"),
        "check_after": work.get("check_after"),
        "step": served.step(work, store.now()) if work else None,
    }


def _held_seat(seat: dict[str, Any], state: str | None) -> dict[str, Any]:
    """A NEEDS YOU row for a seat Scott holds that is idle: on a question it asked him (`waiting`), or
    silent for STALE_SEAT (`stale`), a /clear that never fired, which only he can settle."""
    stale = f"held, silent since {seat['updated_at']}: /clear the seat or wrap {seat['item']}"
    return {
        "why": "stale" if seat["missing"] else "waiting",
        "work": seat["item"],
        "seat": seat["name"],
        "mode": "manual",
        "state": state,
        "text": stale if seat["missing"] else seat["waiting_on"],
    }


def _silent_seat(seat: dict[str, Any], busy: dict[str, Any]) -> dict[str, Any]:
    """A NEEDS YOU row for a busy seat that has gone silent: reconcile found no write for SILENT."""
    return {
        "why": "silent",
        "work": seat["item"],
        "seat": seat["name"],
        "mode": busy["mode"],
        "state": busy["state"],
        "text": f"silent since {seat['silent_since']}: check the seat or wrap {seat['item']}",
        "task": busy["task"],
        "outcome_label": busy.get("outcome_label"),
        "since": seat["silent_since"],
    }


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """`ashigaru status`, the board a person reads: each busy seat with its work in full, the free and
    missing seats by name, the tally, and NEEDS YOU: each manual seat idle on what it last said, then
    each busy seat gone silent, then blocked work (Scott's, and any no seat holds), then subjects held
    for his approval, outsiders' subjects, and writers' PRs that name no item (`unlinked`)."""
    subjects.hygiene(conn)
    served.close_finished(conn)
    roster = seats(conn)
    working = [row for row in roster if row["status"] == SeatStatus.BUSY]
    items = {
        row["item"]: found for row in working if row["item"] and (found := store.get_item(conn, row["item"]))
    }
    served.attach(conn, list(items.values()))
    busy = [_busy(row, items.get(row["item"], {})) for row in working]
    idle = [row for row in roster if row["status"] != SeatStatus.BUSY]
    held = {row["work"]: row for row in busy}
    needs = [
        {
            **_held_seat(row, (held.get(row["item"]) or {}).get("state")),
            "task": (held.get(row["item"]) or {}).get("task"),
            "outcome_label": (held.get(row["item"]) or {}).get("outcome_label"),
            "since": row["updated_at"],
        }
        for row in roster
        if row["owner"] and row["turn"] == Turn.IDLE and (row["waiting_on"] or row["missing"])
    ]
    listed = {row["seat"] for row in needs}
    needs += [
        _silent_seat(row, found)
        for row, found in zip(working, busy, strict=True)
        if row["silent_since"] and row["name"] not in listed
    ]
    blocked = [store.decode(item) for item in conn.execute(NEEDS_YOU, {"limit": QUEUE_LIMIT})]
    served.attach(conn, blocked)
    needs += [_needs(item) for item in blocked]
    needs += [
        {
            "why": "outsider",
            "work": None,
            "mode": None,
            "state": None,
            "text": f"{found['ref'] or found['subject_key']} by {found['author']}: not queued",
        }
        for found in sweep.outsiders(conn)
    ]
    needs += [
        {
            "why": "unlinked",
            "work": None,
            "mode": None,
            "state": None,
            "text": f"{found['subject_key']} by {found['author']}: names no item; link it with update --pr",
            "task": found["title"],
            "since": found["since"],
        }
        for found in intake.unlinked(conn, QUEUE_LIMIT)
    ]
    return {
        "busy": busy,
        "free": [row["name"] for row in idle if not row["missing"]],
        "missing": [row["name"] for row in idle if row["missing"]],
        "summary": summary(conn, roster),
        "needs_you": needs,
        "sweep": sweep.last(conn),
    }


def work(conn: sqlite3.Connection, show_all: bool = False, since: str | None = None) -> dict[str, Any]:
    """`ashigaru work`, the whole check round: the check table, the seats with how many are up and how
    many items are open, the queue, and the catch-up. The check table stops at QUEUE_LIMIT rows
    (`truncated`); GET /v1/items pages the rest. Like every view of the queue, it first settles items
    whose subject moved (subjects.hygiene)."""
    subjects.hygiene(conn)
    served.close_finished(conn)
    items = store.check(conn, show_all, limit=QUEUE_LIMIT + 1)
    roster = seats(conn)
    return {
        "items": served.arrange(conn, items[:QUEUE_LIMIT]),
        "truncated": len(items) > QUEUE_LIMIT,
        "seats": roster,
        "summary": summary(conn, roster),
        "queue": queue(conn),
        "catch_up": catch_up(conn, since),
    }
