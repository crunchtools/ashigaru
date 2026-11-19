"""Human tables for the views and actions. `--json` bypasses all of this."""

import re
import shutil
import textwrap
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

Column = tuple[str, Callable[[dict[str, Any]], Any]]
TEXT_WIDTH = 60
# The check table has three free-text columns side by side.
NARROW = 40
NARROWER = 30
HOURS_A_DAY = 24
WIDTH = 120
"""A table's width when there is no terminal to measure."""
NO_TERMINAL = (WIDTH, 24)
"""shutil's fallback size, columns and lines, when stdout is no terminal and COLUMNS is unset."""
WRAP_MIN = 24
"""The narrowest a wrapped column gets, however wide the rest."""
CLIP_MIN = 12
"""The narrowest a clipped column gets to make room for a wrapped one."""


SEAT_NAME = re.compile(r"^(.*\S) (\d+) - (.+)$")
"""A seat's name, `Dolza 2 - Vertex`: host, number, lane."""


class Whole(str):
    """A cell table() never clips: a task is read in full."""


class Tail(str):
    """A cell table() keeps on one line and clips only when its row overflows: NEXT."""


def clip(value: Any, width: int = TEXT_WIDTH) -> str:
    text = "" if value is None else " ".join(str(value).split())
    return text if isinstance(value, Whole | Tail) or len(text) <= width else text[: width - 1] + "…"


def age(stamp: str | None, now: datetime | None = None) -> str:
    """How long ago a Desk time (`YYYY-MM-DDTHH:MM:SSZ`) was, as 45s, 12m, 3h05m or 2d04h."""
    if not stamp:
        return ""
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return stamp
    return duration((now or datetime.now(UTC)) - then)


def duration(span: Any) -> str:
    seconds = int(span if isinstance(span, int | float) else span.total_seconds())
    minutes, _ = divmod(max(seconds, 0), 60)
    hours, minute = divmod(minutes, 60)
    days, hour = divmod(hours, HOURS_A_DAY)
    if not minutes:
        return f"{max(seconds, 0)}s"
    if not hours:
        return f"{minutes}m"
    return f"{days}d{hour:02d}h" if days else f"{hours}h{minute:02d}m"


def seen(stamp: str | None) -> str:
    """How long ago a check-in was: `now` inside a minute, else `12m ago`; `never` without one."""
    if not stamp:
        return "never"
    ago = age(stamp)
    return "now" if ago.endswith("s") and ago[:-1].isdigit() else f"{ago} ago"


def seat_names(names: Sequence[str]) -> str:
    """Seats by name, those on one host in one lane run together: `Dolza 1/2/4 - Max`."""
    groups: dict[tuple[str, str], list[str]] = {}
    for name in names:
        parts = SEAT_NAME.match(name)
        host, number, lane = parts.groups() if parts else (name, "", "")
        groups.setdefault((host, lane), []).append(number)
    return ", ".join(
        f"{host} {'/'.join(numbers)} - {lane}" if lane else host for (host, lane), numbers in groups.items()
    )


def money(value: float | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def table(rows: Sequence[dict[str, Any]], columns: Sequence[Column], width: int | None = None) -> str:
    """Rows under their headers, each column as wide as its widest cell. When the table is wider than
    `width` (the terminal's, else 120), a Whole column word-wraps, never cut, in the room the others
    leave (WRAP_MIN at least); then, while the row still overflows, a Tail column and then the widest
    others are clipped, none below CLIP_MIN and none widened. Every cell but a Whole one stays on its
    row's first line, so the columns line up."""
    raw = [[get(row) for _, get in columns] for row in rows]
    whole, tail = (
        {i for i in range(len(columns)) if any(isinstance(line[i], kind) for line in raw)}
        for kind in (Whole, Tail)
    )
    cells = [[h for h, _ in columns]] + [[clip(value) for value in line] for line in raw]
    widths = [max(len(line[i]) for line in cells) for i in range(len(columns))]
    room = (width or shutil.get_terminal_size(NO_TERMINAL).columns) - 2 * (len(columns) - 1)
    if whole and sum(widths) > room:
        free = room - sum(w for i, w in enumerate(widths) if i not in whole)
        for i in whole:
            widths[i] = min(widths[i], max(WRAP_MIN, free // len(whole)))
    others = sorted(
        (i for i in range(len(columns)) if i not in whole), key=lambda i: (i not in tail, -widths[i])
    )
    for i in others:
        if (over := sum(widths) - room) <= 0:
            break
        widths[i] = max(min(CLIP_MIN, widths[i]), widths[i] - over)
    lines: list[str] = []
    for line in cells:
        parts = [
            textwrap.wrap(cell, widths[i]) or [""] if i in whole else [clip(cell, widths[i])]
            for i, cell in enumerate(line)
        ]
        for depth in range(max(len(part) for part in parts)):
            row = [part[depth] if depth < len(part) else "" for part in parts]
            lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)


def _context(seat: dict[str, Any]) -> str:
    used, window = seat.get("context_used"), seat.get("context_window")
    if used is None:
        return ""
    return f"{used:,}" + (f" ({100 * used // window}%)" if window else "")


def usage(report: dict[str, Any]) -> str:
    return f"{table(report['seats'], USAGE)}\n\n{table(report['spend'], SPEND)}"


def tally(summary: dict[str, Any]) -> str:
    return f"{summary['up']} of {summary['seats']} up, {summary['open']} open"


def agents(roster: list[dict[str, Any]]) -> str:
    """The seats, then how many are up."""
    up = sum(not seat.get("missing") for seat in roster)
    return f"{table(roster, AGENTS)}\n\n{up} of {len(roster)} up"


def _rows(rows: Sequence[dict[str, Any]], columns: Sequence[Column]) -> str:
    """A table without its header line."""
    return table(rows, columns).partition("\n")[2]


def swept(run: dict[str, Any] | None) -> str:
    """The last sweep in a line: `swept 4m ago (31 subjects, 2 new, 2 closed)`, or its error, or
    `sweep skipped 1m ago: run 41 holds the lease, …` when another run's lease kept it from running."""
    if not run:
        return "no sweep yet"
    if run.get("skipped"):
        return f"sweep skipped {seen(run['finished_at'])}: {run['skipped']}"
    if not run.get("finished_at"):
        return f"sweep running for {age(run['started_at'])}"
    said = f"swept {seen(run['finished_at'])}"
    counts = f"({run['subjects']} subjects, {run['new']} new, {run['closed']} closed)"
    return f"{said}: {run['error']} {counts}" if run.get("error") else f"{said} {counts}"


def sweep(state: dict[str, Any]) -> str:
    """Whether a timer runs, how often, and the last run."""
    if not state["configured"]:
        return "no sweep runs here: the Desk has no forge token and org"
    every = duration(state["every"])
    asked = "run requested; " if state.get("requested") else ""
    running = "running now; " if state.get("running") else ""
    return f"{asked}{running}every {every}; {swept(state.get('last'))}"


def status(board: dict[str, Any]) -> str:
    """The busy seats, one line for the free and missing ones and the tally, then NEEDS YOU."""
    seats = [f"{len(board['free'])} free" + (f": {seat_names(board['free'])}" if board["free"] else "")]
    if board["missing"]:
        seats.append(f"{len(board['missing'])} missing: {seat_names(board['missing'])}")
    seats += [tally(board["summary"]), swept(board.get("sweep"))]
    parts = [table(board["busy"], BOARD) if board["busy"] else "no seat is busy", "   ".join(seats)]
    if board["needs_you"]:
        parts.append(f"NEEDS YOU\n{_rows(board['needs_you'], NEEDS)}")
    if (found := board.get("changes")) and found["items"]:
        when = seen(found["seen_at"]) if found.get("seen_at") else "a first look: the last day"
        parts.append(f"CHANGED SINCE YOU LOOKED ({when})\n{_rows(found['items'], CHANGES)}")
    return "\n\n".join(parts)


def looked(found: dict[str, Any]) -> str:
    """Where a look began: `since you looked 3h05m ago`, `since <time>`, or the default day."""
    if found.get("since"):
        return f"since {found['since']}"
    return f"since you looked {seen(found['seen_at'])}" if found.get("seen_at") else "in the last day"


def changes(found: dict[str, Any]) -> str:
    """What others changed, last first, then the window and whether the mark moved."""
    window = f"{len(found['items'])} changed {looked(found)}; events {found['after']} to {found['until']}"
    if found.get("truncated"):
        window += ", cut at 1000: the next --ack reads on"
    if found.get("acked"):
        window += " (seen mark moved)"
    return f"{table(found['items'], CHANGES)}\n{window}" if found["items"] else window


def queue(lists: dict[str, Any]) -> str:
    return f"{table(lists['queued'], QUEUED)}\n\n{table(lists['proposed'], PROPOSED)}"


def state(row: dict[str, Any]) -> str:
    """STATE: `review`, `manual · blocked`; auto, the default, shows bare; a wrap stage follows."""
    shown = row["state"] + (f"/{row['wrap']}" if row.get("wrap") else "")
    return f"manual · {shown}" if row.get("mode") == "manual" else shown


def work(round_: dict[str, Any]) -> str:
    """The check table, the seats and their tally, the queue, then what the forge brought in."""
    caught = round_["catch_up"]
    window = f"events {caught['after']} to {caught['until']}"
    if caught.get("truncated"):
        window += ", cut at 1000: the next --advance reads on"
    if caught.get("advanced"):
        window += " (sweep mark moved)"
    cut = "\n(the first 1000; ashigaru list pages the rest)" if round_.get("truncated") else ""
    parts = [
        table(round_["items"], WORK) + cut,
        table(round_["seats"], AGENTS),
        tally(round_["summary"]),
        queue(round_["queue"]),
        f"{table(caught['new'], NEW)}\n{window}",
    ]
    if caught["reopened"]:
        parts.append(table(caught["reopened"], REOPENED))
    return "\n\n".join(parts)


def minted(result: dict[str, Any]) -> str:
    """The URL alone, so `$(ashigaru mcp mint ...)` captures it; or where --out wrote it."""
    if "url" in result:
        return str(result["url"])
    return f"wrote the MCP URL for {result['identity']} (code {result['code_id']}) to {result['out']}"


def _row(item: dict[str, Any]) -> dict[str, Any]:
    """An item as a check-table row."""
    return {**item, "result_or_blocker": item.get("blocked_reason") or item.get("result")}


def waited(result: dict[str, Any]) -> str:
    """A settled item's row, or a commit's CI: its status, what it still waits on, then each check."""
    if "status" not in result:
        return table([_row(result)], WORK)
    head = f"{result['repo']}@{result['sha'][:12]}  {result['status']}"
    if result.get("pending"):
        head += f"\npending: {', '.join(result['pending'])}"
    if result.get("unverified"):
        head += f"\n{result['unverified']}"
    return f"{head}\n\n{table(result['checks'], CHECKS)}" if result.get("checks") else head


def view(command: str, result: Any) -> str:
    """The table for a command's result: a view's own, or one work row for an action's item."""
    if command in VIEWS:
        return VIEWS[command](result)
    if command in LISTS:
        return table(result, LISTS[command])
    return table([_row(result)], WORK)


USAGE: list[Column] = [
    ("SEAT", lambda s: s["name"]),
    ("MODEL", lambda s: s.get("model")),
    ("CONTEXT", _context),
    ("TOKENS", lambda s: "" if s.get("session_tokens") is None else f"{s['session_tokens']:,}"),
    ("COST", lambda s: money(s.get("session_cost_usd"))),
    ("TURN", lambda s: s.get("turn")),
    ("IN TURN", lambda s: "" if s.get("turn_seconds") is None else duration(s["turn_seconds"])),
]
SPEND: list[Column] = [
    ("BILLING", lambda b: b["billing"]),
    ("TODAY", lambda b: money(b["today_usd"])),
    ("DAILY LIMIT", lambda b: money(b.get("daily_limit_usd"))),
    ("MONTH", lambda b: money(b["month_usd"])),
    ("MONTHLY LIMIT", lambda b: money(b.get("monthly_limit_usd"))),
]
AGENTS: list[Column] = [
    ("SEAT", lambda s: s["name"]),
    ("STATUS", lambda s: "missing" if s.get("missing") else s.get("hold") or s["status"]),
    ("WORK", lambda s: s.get("item")),
    ("TIME", lambda s: age(s.get("since"))),
    ("TURN", lambda s: s["turn"]),
    ("SEEN", lambda s: age(s.get("updated_at")) or "never"),
    ("TASK", lambda s: Whole(s.get("task") or "")),
]
BOARD: list[Column] = [
    ("OUTCOME", lambda r: r.get("outcome_label") or "-"),
    ("MODE", lambda r: r["mode"]),
    ("SEAT", lambda r: r["seat"]),
    ("WORK", lambda r: r.get("work")),
    ("TIME", lambda r: age(r.get("since"))),
    ("ACTIVE", lambda r: "missing" if r.get("missing") else seen(r.get("active"))),
    ("TASK", lambda r: Whole(r.get("task") or "")),
]
NEEDS: list[Column] = [
    ("WORK", lambda n: n.get("work")),
    ("MODE", lambda n: n.get("mode")),
    ("WHY", lambda n: n["why"] if n["why"] != "blocked" else n["state"]),
    ("TEXT", lambda n: Whole(f"{n['seat']}: {n['text']}" if n.get("seat") else n.get("text") or "")),
]
CHANGES: list[Column] = [
    ("WORK", lambda r: r["id"]),
    ("WHEN", lambda r: seen(r["at"])),
    ("STATE", lambda r: state(r)),
    ("BY", lambda r: ", ".join(r["by"])),
    ("CHANGED", lambda r: "new" if r["new"] else ", ".join(r["fields"])),
    ("TASK", lambda r: Whole(r["task"])),
    ("NEXT", lambda r: Tail(r.get("step") or "")),
]
WORK: list[Column] = [
    ("WORK", lambda r: r["id"]),
    ("TIME", lambda r: age(r.get("since") or r.get("updated_at"))),
    ("SEAT", lambda r: r.get("seat")),
    ("STATE", lambda r: state(r)),
    ("OUTCOME", lambda r: r.get("outcome_label") or "-"),
    ("TASK", lambda r: Whole(r["task"])),
    (
        "NEXT",
        lambda r: Tail(
            " ".join(str(r.get("step") or r.get("next") or r.get("result_or_blocker") or "").split())
        ),
    ),
]
"""Scott's `work` columns: the mode shows inside STATE, and NEXT is the one next step."""
QUEUED: list[Column] = [
    ("QUEUED", lambda i: i["id"]),
    ("LANE", lambda i: i.get("lane")),
    ("MODE", lambda i: i["mode"]),
    ("TASK", lambda i: Whole(i["task"])),
    ("WAITS ON", lambda i: ", ".join(f"{w['id']} {w['state']}" for w in i["waits_on"])),
]
PROPOSED: list[Column] = [
    ("PROPOSED", lambda i: i["id"]),
    ("UNDER", lambda i: i.get("parent")),
    ("FILED BY", lambda i: i.get("filed_by")),
    ("TASK", lambda i: Whole(i["task"])),
    ("EXPIRES", lambda i: i.get("expires_at")),
]
OUTCOMES: list[Column] = [
    ("ID", lambda o: o["id"]),
    ("STATE", lambda o: o["state"]),
    ("REF", lambda o: o.get("ref")),
    ("TASK", lambda o: clip(o["task"], NARROW)),
    ("DONE", lambda o: f"{o['done']}/{o['total']}"),
    ("BLOCKED", lambda o: o["blocked"]),
    ("OPEN", lambda o: o["open"]),
]
NEW: list[Column] = [
    ("NEW", lambda c: c["id"]),
    ("REF", lambda c: c.get("ref")),
    ("TITLE", lambda c: clip(c.get("title"), NARROW)),
    ("LANE", lambda c: "held" if c["held"] else c.get("lane") or "no lane"),
    ("FILED BY", lambda c: c.get("filed_by")),
]
REOPENED: list[Column] = [
    ("REOPENED", lambda c: c["id"]),
    ("REF", lambda c: c.get("ref")),
    ("TITLE", lambda c: clip(c.get("title"), NARROW)),
    ("STATE", lambda c: c["state"]),
]
CODES: list[Column] = [
    ("ID", lambda c: c["code_id"]),
    ("IDENTITY", lambda c: c["identity"]),
    ("LABEL", lambda c: c.get("label")),
    ("CREATED", lambda c: c["created_at"]),
    ("LAST USED", lambda c: c.get("last_used_at")),
    ("REVOKED", lambda c: c.get("revoked_at")),
]
CHECKS: list[Column] = [
    ("CHECK", lambda c: c["name"]),
    ("VERDICT", lambda c: c["verdict"]),
    ("URL", lambda c: c.get("url")),
]
MARK: list[Column] = [
    ("MARK", lambda m: m["name"]),
    ("VALUE", lambda m: m["value"]),
    ("UPDATED", lambda m: m["updated_at"]),
    ("BY", lambda m: m["updated_by"]),
]
LISTS = {"outcomes": OUTCOMES, "mcp_list": CODES}
VIEWS: dict[str, Callable[[Any], str]] = {
    "list": lambda items: table([_row(item) for item in items], WORK),
    "wait": waited,
    "mark": lambda m: table([m], MARK),
    "usage": usage,
    "agents": agents,
    "status": status,
    "changes": changes,
    "sweep": sweep,
    "queue": queue,
    "work": work,
    "mcp_mint": minted,
    "mcp_revoke": lambda code: table([code], CODES),
}
