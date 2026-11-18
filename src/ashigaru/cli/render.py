"""Human tables for the views and actions. `--json` bypasses all of this."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

Column = tuple[str, Callable[[dict[str, Any]], Any]]
TEXT_WIDTH = 60
# The check table has three free-text columns side by side.
NARROW = 40
NARROWER = 30
HOURS_A_DAY = 24


def clip(value: Any, width: int = TEXT_WIDTH) -> str:
    text = "" if value is None else " ".join(str(value).split())
    return text if len(text) <= width else text[: width - 1] + "…"


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


def money(value: float | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def table(rows: Sequence[dict[str, Any]], columns: Sequence[Column]) -> str:
    cells = [[h for h, _ in columns]] + [[clip(get(row)) for _, get in columns] for row in rows]
    widths = [max(len(line[i]) for line in cells) for i in range(len(columns))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip() for line in cells
    ]
    return "\n".join(lines)


def _context(seat: dict[str, Any]) -> str:
    used, window = seat.get("context_used"), seat.get("context_window")
    if used is None:
        return ""
    return f"{used:,}" + (f" ({100 * used // window}%)" if window else "")


def usage(report: dict[str, Any]) -> str:
    return f"{table(report['seats'], USAGE)}\n\n{table(report['spend'], SPEND)}"


def queue(lists: dict[str, Any]) -> str:
    return f"{table(lists['queued'], QUEUED)}\n\n{table(lists['proposed'], PROPOSED)}"


def work(round_: dict[str, Any]) -> str:
    """The check table, the seats and their tally, the queue, then what the forge brought in."""
    tally = round_["summary"]
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
        f"{tally['up']} of {tally['seats']} up, {tally['open']} items open",
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
    """A settled item's row, or a commit's CI: its status, then each check."""
    if "status" not in result:
        return table([_row(result)], WORK)
    head = f"{result['repo']}@{result['sha'][:12]}  {result['status']}"
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
    ("ITEM", lambda s: s.get("item")),
    ("FOR", lambda s: age(s.get("since"))),
    ("TURN", lambda s: s["turn"]),
    ("TASK", lambda s: s.get("task")),
    ("SEEN", lambda s: age(s.get("updated_at")) or "never"),
]
WORK: list[Column] = [
    ("ID", lambda r: r["id"]),
    ("SEAT", lambda r: r.get("seat")),
    ("MODE", lambda r: r["mode"]),
    ("STATE", lambda r: r["state"] + (f"/{r['wrap']}" if r.get("wrap") else "")),
    ("TASK", lambda r: clip(r["task"], NARROW)),
    ("RESULT OR BLOCKER", lambda r: clip(r.get("result_or_blocker"), NARROW)),
    ("NEXT", lambda r: clip(r.get("next"), NARROWER)),
]
QUEUED: list[Column] = [
    ("QUEUED", lambda i: i["id"]),
    ("LANE", lambda i: i.get("lane")),
    ("MODE", lambda i: i["mode"]),
    ("TASK", lambda i: i["task"]),
    ("WAITS ON", lambda i: ", ".join(f"{w['id']} {w['state']}" for w in i["waits_on"])),
]
PROPOSED: list[Column] = [
    ("PROPOSED", lambda i: i["id"]),
    ("UNDER", lambda i: i.get("parent")),
    ("FILED BY", lambda i: i.get("filed_by")),
    ("TASK", lambda i: i["task"]),
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
LISTS = {"agents": AGENTS, "outcomes": OUTCOMES, "mcp_list": CODES}
VIEWS: dict[str, Callable[[Any], str]] = {
    "list": lambda items: table([_row(item) for item in items], WORK),
    "wait": waited,
    "mark": lambda m: table([m], MARK),
    "usage": usage,
    "queue": queue,
    "work": work,
    "mcp_mint": minted,
    "mcp_revoke": lambda code: table([code], CODES),
}
