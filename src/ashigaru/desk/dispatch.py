"""Dispatch on top of the store: assign, claim and wrap, usage and spend, the queue, seats, outcomes
and work views.

Assign and claim run in one BEGIN IMMEDIATE transaction each, so two callers
racing for one item or one seat get one winner and one Conflict.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from . import outcomes as served
from . import store, subjects, sweep
from .model import CLOSED, SEAT_ROW_FIELDS, STALE_SEAT, Billing, Lane, Role, SeatStatus, State, Turn, Wrap

PICK_SEAT = """
    SELECT name FROM seats
    WHERE lane = :lane AND status = 'free' AND owner IS NULL
    ORDER BY since, name
    LIMIT 1
"""

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
CATCH_UP_DEFAULT = timedelta(days=1)

NEEDS_YOU = f"""
    SELECT * FROM items
    WHERE {store.OPEN} AND seat IS NULL AND state = 'queued' AND json_extract(extra, '$.trusted') = 0
       OR {store.OPEN} AND mode = 'manual' AND state = 'blocked'
    ORDER BY state = 'blocked' DESC, updated_at DESC, id DESC
    LIMIT :limit
"""
"""Scott's blocked work, then the subjects intake holds for a maintainer's approval."""

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


def _free(seat: dict[str, Any] | None, name: str) -> dict[str, Any]:
    if seat is None:
        raise store.Conflict(f"seat {name!r} has not reported")
    if seat["owner"]:
        raise store.Conflict(f"seat {name!r} is held by {seat['owner']}")
    if seat["status"] != "free":
        raise store.Conflict(f"seat {name!r} is busy with {seat['item'] or 'something'}")
    return seat


def _take(conn: sqlite3.Connection, item: dict[str, Any], seat: dict[str, Any], actor: str) -> None:
    fields = {"seat": seat["name"], "state": State.ASSIGNED}
    if item["lane"] is None and seat["lane"] is not None:
        fields["lane"] = seat["lane"]
    store.update_item(conn, item["id"], fields, actor)
    store.write_seat(conn, seat["name"], {"status": "busy", "item": item["id"]})


def assign(conn: sqlite3.Connection, item_id: str, seat: str | None, lane: str | None, actor: str) -> str:
    """Give the item to the named seat, or to the longest-free unheld seat in the lane. Returns the seat."""
    with store.transaction(conn):
        item = store.get_item(conn, item_id)
        if item is None:
            raise KeyError(item_id)
        _ready(conn, item)
        lane = lane or item["lane"]
        if seat is None:
            if lane is None:
                raise store.Conflict(f"{item_id} has no lane to pick a seat by; name a seat or a lane")
            picked = conn.execute(PICK_SEAT, {"lane": lane}).fetchone()
            if picked is None:
                raise store.Conflict(f"no free seat in lane {lane}")
            seat = str(picked[0])
        row = _free(store.get_seat(conn, seat), seat)
        if lane is not None and item["lane"] is None:
            store.update_item(conn, item_id, {"lane": lane}, actor)
            item["lane"] = lane
        _take(conn, item, row, actor)
    return seat


def claim(conn: sqlite3.Connection, item_id: str, seat: str) -> None:
    """desk_claim: a free seat takes a queued, unowned, auto item in its lane. Fails closed: a seat
    with no lane claims nothing, and an item with no lane is claimed by no seat; Josui sets its lane
    or assigns it."""
    with store.transaction(conn):
        item = store.get_item(conn, item_id)
        if item is None:
            raise KeyError(item_id)
        _ready(conn, item)
        if item["state"] != State.QUEUED or item["seat"] is not None:
            raise store.Conflict(
                f"{item_id} is {item['state']} with {item['seat'] or 'no seat'}; not claimable"
            )
        if item["mode"] != "auto":
            raise store.Conflict(f"{item_id} is manual: Scott's to hand out")
        row = _free(store.get_seat(conn, seat), seat)
        if row["lane"] is None:
            raise store.Conflict(f"seat {seat!r} has no lane; Josui sets it before it can claim")
        if item["lane"] is None:
            raise store.Conflict(f"{item_id} has no lane; Josui sets it or assigns the item")
        if item["lane"] != row["lane"]:
            raise store.Conflict(f"{item_id} is in lane {item['lane']}, seat {seat!r} in {row['lane']}")
        _take(conn, item, row, seat)


def report_seat(conn: sqlite3.Connection, name: str, fields: dict[str, Any], actor: str) -> dict[str, Any]:
    """PATCH /seats/{name}, and how the Desk sees Scott's hand on a seat, which no seat can claim or drop.

    A seat's report of its session's first typed prompt (turn active with a task) while it holds no
    item makes it busy and manual, holding a new manual item with that task, the way /agents ledgered
    untracked manual work. A /clear (task back to null) clears that item, which frees the seat; a wrap
    does the same. A seat holding an item stays on it. Turn changes without a task (Claude Code's own
    turns: an assignment arriving, a background task's result) never start a hold.
    """
    with store.transaction(conn):
        before = store.get_seat(conn, name)
        seat = store.write_seat(conn, name, fields)
        if not set(fields) <= SEAT_ROW_FIELDS:
            return seat
        held = before["item"] if before and before["owner"] else None
        if held and "task" in fields and fields["task"] is None:
            store.update_item(conn, held, {"wrap": Wrap.CLEARED}, actor, source="clear")
        elif fields.get("turn") == Turn.ACTIVE and fields.get("task") and seat["status"] == SeatStatus.FREE:
            work = {"task": fields["task"], "mode": "manual", "state": State.WORKING, "seat": name}
            item_id = store.insert_item(conn, {**work, "lane": seat["lane"]}, actor, source="prompt")
            store.write_seat(
                conn, name, {"status": SeatStatus.BUSY, "owner": store.MANUAL_OWNER, "item": item_id}
            )
        return store.get_seat(conn, name) or seat


def report_usage(
    conn: sqlite3.Connection, seat: str, report: dict[str, Any], lane_first: bool = False
) -> dict[str, Any]:
    """Record a seat's usage and book whatever of the session's cost is not yet in spend. Billing is
    the report's, else the seat's lane's, else what the row had; with `lane_first` (a seat reporting
    for itself) the lane's comes before the report's. Until one is known the cost waits."""
    with store.transaction(conn):
        current = store.get_seat(conn, seat) or {**store.SEAT_DEFAULTS, "session_id": None}
        cost = report.pop("cost_usd", None)
        fields = {key: value for key, value in report.items() if value is not None}
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


def wrap(conn: sqlite3.Connection, item_id: str, request: dict[str, Any], actor: str) -> None:
    """`wrap`, for the CLI and MCP alike. `wrapping` records that /cc seat was sent. `cleared` refuses
    (Refused) until the item is memorized or a Learned line comes with the request, and while its
    seat's turn is active, unless forced; then it clears the wrap and closes an open item, which
    frees the seat."""
    with store.transaction(conn):
        item = store.get_item(conn, item_id)
        if item is None:
            raise KeyError(item_id)
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
    missing when it never did, or its row has not changed in STALE_SEAT."""
    stale = (datetime.now(UTC) - STALE_SEAT).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{**seat, "missing": seat["updated_at"] < stale} for seat in store.list_seats(conn)]
    unseen = conn.execute(UNSEEN_SEATS, {"role": Role.SEAT})
    return rows + [{**store.SEAT_DEFAULTS, "name": row[0], "missing": True} for row in unseen]


def seat(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """One seat row, `missing` when it has not changed in STALE_SEAT."""
    found = store.get_seat(conn, name)
    stale = (datetime.now(UTC) - STALE_SEAT).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {**found, "missing": found["updated_at"] < stale} if found else None


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


def cursor(conn: sqlite3.Connection, since: str | None) -> tuple[int, str | None]:
    """Where a catch-up starts: just before the first event at `since`; else the sweep mark, an event
    seq that `advance` wrote or a time someone set; else a day back. Returns the seq and the
    time it was read from, if any. Raises ValueError for a time that is not one."""
    if since is None:
        mark = store.get_mark(conn, SWEEP)
        if mark and mark["value"].isdigit():
            return int(mark["value"]), None
        since = mark["value"] if mark else stamp(datetime.now(UTC) - CATCH_UP_DEFAULT)
    since = instant(since)
    return int(conn.execute(FROM_TIME, {"since": since}).fetchone()[0]), since


def catch_up(conn: sqlite3.Connection, since: str | None = None) -> dict[str, Any]:
    """The org sweep from the Desk's own data: intake's events after the cursor, as subjects first seen
    (`new`) and subjects reopened at the source while their item is closed (`reopened`), each item once.

    Events are numbered in commit order (one writer at a time), so `until`, the cursor `advance`
    stores, loses and repeats nothing: it is the last event read when QUEUE_LIMIT events cut the
    round short (`truncated`), else the last committed when the round began, so a delivery that
    commits mid-round is the next round's."""
    after, read_from = cursor(conn, since)
    last = int(conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])
    rows = conn.execute(CAUGHT, {"after": after, "until": last, "limit": QUEUE_LIMIT + 1}).fetchall()
    truncated = len(rows) > QUEUE_LIMIT
    rows = rows[:QUEUE_LIMIT]
    found: dict[str, dict[str, dict[str, Any]]] = {"new": {}, "reopened": {}}
    for row in rows:
        item = {key: value for key, value in store.decode(row).items() if key not in CAUGHT_ONLY}
        if row["event_seq"] == row["first_seq"]:
            found["new"].setdefault(item["id"], _caught(item))
        elif item["id"] not in found["new"]:
            found["reopened"].setdefault(item["id"], _caught(item))
    until = rows[-1]["event_seq"] if truncated else max(last, after)
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
    return {"why": why, "work": item["id"], "mode": item["mode"], "state": item["state"], "text": text}


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


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """`ashigaru status`, the board a person reads: each busy seat with its work in full, the free and
    missing seats by name, the tally, and NEEDS YOU: each manual seat idle on what it last said, then
    Scott's blocked work, then subjects held for his approval."""
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
    held = {row["work"]: row["state"] for row in busy}
    needs = [
        _held_seat(row, held.get(row["item"]))
        for row in roster
        if row["owner"] and row["turn"] == Turn.IDLE and (row["waiting_on"] or row["missing"])
    ]
    needs += [_needs(store.decode(item)) for item in conn.execute(NEEDS_YOU, {"limit": QUEUE_LIMIT})]
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
