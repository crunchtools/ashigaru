"""Dispatch on top of the store: assign and claim, usage and spend, the outcomes rollup.

Assign and claim run in one BEGIN IMMEDIATE transaction each, so two callers
racing for one item or one seat get one winner and one Conflict.
"""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import store
from .model import CLOSED, SEAT_ROW_FIELDS, SeatStatus, State, Turn, Wrap

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


def report_usage(conn: sqlite3.Connection, seat: str, report: dict[str, Any]) -> dict[str, Any]:
    """Record a seat's usage and book the session cost it added since its last report."""
    with store.transaction(conn):
        current = store.get_seat(conn, seat) or {**store.SEAT_DEFAULTS, "session_id": None}
        cost = report.pop("cost_usd", None)
        fields = {key: value for key, value in report.items() if value is not None}
        if cost is not None:
            same = current["session_id"] == report["session_id"]
            delta = max(cost - (current["session_cost_usd"] if same else 0.0), 0.0)
            fields["session_cost_usd"] = cost
            billing = fields.get("billing") or current["billing"]
            if billing and delta:
                conn.execute(ADD_SPEND, {"billing": billing, "day": store.now()[:10], "cost": delta})
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
