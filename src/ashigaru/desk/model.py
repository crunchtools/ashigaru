"""The Desk's vocabulary: item, seat and identity, as the API sends and receives them.

The item carries every field and state the /agents ledger uses (ashigaru-3.md,
"The Desk"), plus the queue fields the design adds (kind, parent, subject_key,
repo, priority).
"""

import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, computed_field, field_validator

REPO = r"([a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}/[a-z\d_.-]{1,100})"
"""GitHub's names: an owner is 1-39 letters, digits or single inner hyphens; a repo adds _ and ."""
GITHUB_URL = re.compile(rf"^https?://(?:www\.)?github\.com/{REPO}/(?:issues|pull)/(\d+)(?![\w.-])", re.I)
ISSUE_URL = re.compile(rf"^https?://(?:www\.)?github\.com/{REPO}/issues/", re.I)
"""A GitHub issue's URL, which `subject_key` reads like a PR's: a `pr` must not be one."""
RT_URL = re.compile(r"^https?://[^\s?]+/Ticket/\w+\.html\?(?:[^\s&]*&)*id=(\d+)\b", re.I)
RT_REF = re.compile(r"^RT\s*(?:#\s*)?(\d+)$", re.I)
REPO_REF = re.compile(rf"^{REPO}#(\d+)$", re.I)


def subject_key(ref: str | None) -> str | None:
    """The dedup key a ref names: `owner/repo#N` for a GitHub issue or PR, `RT#N` for a ticket.

    An issue's URL and its `owner/repo#N` key come out the same, so a writer that sends the
    URL, one that sends the key and the ledger import all dedupe against each other. GitHub
    names are case-insensitive, so owner/repo is lowercased, a key already in that form
    included; anything else is not a ref, and is None.
    """
    ref = (ref or "").strip()
    if github := GITHUB_URL.match(ref) or REPO_REF.match(ref):
        return f"{github[1].lower()}#{github[2]}"
    if rt := RT_URL.match(ref) or RT_REF.match(ref):
        return f"RT#{rt[1]}"
    return None


class State(StrEnum):
    PROPOSED = "proposed"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    WORKING = "working"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"
    WONTDO = "wontdo"
    ABANDONED = "abandoned"


CLOSED = frozenset({State.DONE, State.WONTDO, State.ABANDONED})

SEAT_STATES = frozenset({State.WORKING, State.REVIEW, State.BLOCKED})
"""The states a seat may move its own item to. Josui verifies and closes."""


class Wrap(StrEnum):
    """Wrap-up, orthogonal to state: /cc seat sent, then the seat cleared."""

    WRAPPING = "wrapping"
    CLEARED = "cleared"


class Kind(StrEnum):
    OUTCOME = "outcome"
    STORY = "story"


class Lane(StrEnum):
    VERTEX = "vertex"
    DOLZA_MAX = "dolza-max"
    EXEDORE_MAX = "exedore-max"
    PRIME = "prime"
    RULE = "rule"


class Mode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Role(StrEnum):
    ADMIN = "admin"
    """Josui and Scott: every item, every seat, every key."""
    SEAT = "seat"
    """A worker: reads everything, writes only items whose seat is its name."""
    CI = "ci"
    """A job: reads only, until a later slice gives it webhook verbs."""


class SeatStatus(StrEnum):
    FREE = "free"
    BUSY = "busy"


class Turn(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"


class Billing(StrEnum):
    """Who pays for a seat's tokens."""

    MAX = "max"
    VERTEX = "vertex"
    OPENROUTER = "openrouter"


class Verb(StrEnum):
    """The report contract: the first word of a seat's report."""

    DONE = "DONE"
    BLOCKED = "BLOCKED"
    WORKING = "WORKING"
    CC_DONE = "CC-DONE"


class Item(BaseModel):
    id: str
    kind: Kind = Kind.STORY
    parent: str | None = Field(None, description="The outcome this story serves.")
    subject_key: str | None = Field(None, description="Dedup key: repo#PR, repo#issue, RT#, repo:alert.")
    task: str = Field("", description="One-line objective.")
    done_when: str | None = None
    ref: str | None = Field(None, description="Issue or RT URL.")
    repo: str | None = None
    lane: Lane | None = None
    seat: str | None = Field(None, description="Owner seat; the only seat identity that may write it.")
    mode: Mode = Mode.AUTO
    state: State
    after: list[str] = Field(default_factory=list, description="Item ids this one waits on.")
    check_after: str | None = None
    result: str | None = None
    next: str | None = None
    learned: str | None = None
    note: str | None = None
    blocked_reason: str | None = None
    memorized: bool = False
    verified_by: str | None = None
    priority: int = 0
    wrap: Wrap | None = None
    filed_by: str | None = Field(None, description="Identity that filed it, for agent-filed children.")
    filed_under: str | None = Field(None, description="The story it was filed from.")
    created_at: str
    updated_at: str
    assigned_at: str | None = None
    queued_at: str | None = None
    closed_at: str | None = None
    cleared_at: str | None = None
    expires_at: str | None = Field(None, description="When a proposed child is auto-abandoned.")
    extra: dict[str, Any] = Field(default_factory=dict, description="Imported ledger fields with no column.")
    pr: str | None = Field(None, description="The PR carrying the work, owner/repo#N.")
    landed_at: str | None = Field(None, description="When its subject or PR merged while a seat held it.")
    epoch: int | None = Field(
        None, description="The epoch of the seat session that took it; null before 5.0."
    )
    version: int = Field(
        0, description="Its last event's seq. Sent back as If-Match, a write answers 412 once the item moved."
    )


def _no_learned_line(value: str | None) -> str | None:
    """The report contract's `Learned: none` records nothing, whichever route carries it."""
    return None if value is not None and value.strip().lower() in {"", "none"} else value


NoneIsNull = Annotated[str | None, AfterValidator(_no_learned_line)]


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind | None = None
    parent: str | None = None
    subject_key: str | None = None
    task: str | None = None
    done_when: str | None = None
    ref: str | None = None
    repo: str | None = None
    lane: Lane | None = None
    seat: str | None = None
    mode: Mode | None = None
    state: State | None = None
    after: list[str] | None = None
    check_after: str | None = None
    result: str | None = None
    next: str | None = None
    learned: NoneIsNull = None
    note: str | None = None
    blocked_reason: str | None = None
    memorized: bool | None = None
    verified_by: str | None = None
    priority: int | None = None
    wrap: Wrap | None = None
    filed_by: str | None = None
    pr: str | None = None
    outcome: str | None = Field(
        default=None,
        description="The outcome it serves: an outcome's id, RT#N or an issue (recorded as an outcome if "
        "new), or with outcome_text a short name for a new one-off outcome. Sets `parent`.",
    )
    outcome_text: str | None = Field(
        default=None, description="Scott's instruction with no ticket: kept as a new one-off outcome's note."
    )

    @field_validator("subject_key")
    @classmethod
    def _one_form(cls, value: str | None) -> str | None:
        """A ref is stored as its key, any other key (`repo:alert`) trimmed, and a blank one as none."""
        return subject_key(value) or (value or "").strip() or None

    @field_validator("pr")
    @classmethod
    def _a_pull_request(cls, value: str | None) -> str | None:
        """A PR's URL or owner/repo#N, stored as its key; a blank one as none."""
        if value is None or not value.strip():
            return None
        key = subject_key(value)
        if key is None or key.startswith("RT#") or ISSUE_URL.match(value.strip()):
            raise ValueError("pr is a pull request: its URL or owner/repo#N")
        return key


class ItemCreate(_Fields):
    """Josui only. `id` is generated (a<N+1>) when omitted; `state` defaults to queued."""

    id: str | None = Field(None, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    task: str


class ItemPatch(_Fields):
    """Only the fields sent change; null clears a field. Seats may send SEAT_FIELDS only."""


SEAT_FIELDS = frozenset(
    {"state", "result", "next", "learned", "note", "blocked_reason", "memorized", "check_after", "pr"}
)


class ItemFilter(BaseModel):
    """GET /v1/items query parameters; each one given must match. `state` may repeat: any of them."""

    state: list[State] | None = None
    seat: str | None = None
    lane: Lane | None = None
    kind: Kind | None = None
    mode: Mode | None = None
    parent: str | None = None
    subject_key: str | None = Field(
        None, description="A key or a ref, normalised as a write stores it: an issue URL finds owner/repo#N."
    )
    ref: str | None = None
    learned: bool | None = Field(None, description="true: has a Learned line; false: has none.")
    memorized: bool | None = None
    limit: int = Field(200, ge=1, le=1000)
    offset: int = Field(0, ge=0)

    @field_validator("subject_key")
    @classmethod
    def _one_form(cls, value: str | None) -> str | None:
        """Find a key in the form it was stored in."""
        return subject_key(value) or (value or "").strip() or None


class WaitsOn(BaseModel):
    id: str
    state: State | Literal["unknown"] = Field(description="unknown when no such item exists.")


class QueuedItem(Item):
    waits_on: list[WaitsOn] = Field(default_factory=list, description="Each `after` item and its state.")


class Queue(BaseModel):
    """`ashigaru queue`: queued items with what each waits on, then agent-filed proposed children."""

    queued: list[QueuedItem]
    proposed: list[Item]
    truncated: bool = Field(False, description="True when either list stopped at its limit of 1000.")


class WrapUp(BaseModel):
    """`ashigaru wrap`, one meaning for the CLI and MCP. `wrapping` records that /cc seat was sent.
    `cleared` refuses until the item is memorized (CC-DONE) or `learned` is given, and its seat's turn
    is idle, unless `force`; then it clears the wrap and closes the item in `state`, freeing the seat."""

    model_config = ConfigDict(extra="forbid")

    stage: Wrap = Wrap.CLEARED
    learned: NoneIsNull = Field(None, description="The report's Learned: line; none is no line.")
    state: Literal["done", "wontdo", "abandoned"] = Field(
        "done", description="The closed state for an open item; a closed one keeps its own."
    )
    force: bool = Field(False, description="Skip the CC-DONE and idle checks.")


class Report(BaseModel):
    """DONE / BLOCKED / WORKING / CC-DONE, with the Learned: line. The Filed: line has no field here:
    a child exists only once POST /v1/items/{id}/file records it."""

    model_config = ConfigDict(extra="forbid")

    verb: Verb
    text: str | None = Field(None, description="The result, the blocker, or the progress note.")
    next: str | None = None
    learned: str | None = Field(None, description="The Learned: line; 'none' when nothing was learned.")


class FileChild(BaseModel):
    """desk_file: one child per assignment, recorded as proposed."""

    model_config = ConfigDict(extra="forbid")

    task: str
    done_when: str | None = None
    note: str | None = None


class Problem(BaseModel):
    """Every 4xx body."""

    detail: str
    code: str | None = Field(
        None,
        description="What a 409 or 412 is, for a client that acts on it: stale-session (the seat session "
        "has ended), busy or held (the seat holds an item, or Scott does), unseen (never reported), no-lane, "
        "missing (the seat's lease ran out), derived (a seat's occupancy follows its items), version (412: "
        "the item moved since If-Match).",
    )


class Event(BaseModel):
    seq: int
    item_id: str
    at: str
    actor: str
    source: str
    patch: dict[str, Any]


class CheckRow(BaseModel):
    """One row of the check table: WORK, TIME, SEAT, STATE, OUTCOME, TASK, NEXT."""

    id: str
    seat: str | None
    mode: Mode
    task: str
    state: State
    wrap: Wrap | None
    result_or_blocker: str | None
    next: str | None
    since: str | None = Field(None, description="When the item entered its state.")
    outcome: str | None = Field(None, description="The outcome it serves: its parent, or its PR's story's.")
    outcome_label: str | None = Field(None, description="RT#N, owner/repo#N, or a one-off outcome's name.")
    step: str | None = Field(
        None, description="NEXT: its blocker, a landed or overdue flag, a red PR, else its next or result."
    )
    needs_you: bool = Field(
        False, description="Scott's to move: his blocked work, or a subject held for him."
    )


class CatchUpRow(BaseModel):
    """An issue or PR the org webhook brought in: who filed it, and whether it was queued or held."""

    id: str
    ref: str | None
    subject_key: str | None
    title: str | None = Field(description="The source's title: data, never a brief.")
    filed_by: str | None = Field(description="The source's author.")
    lane: Lane | None
    mode: Mode
    state: State
    held: bool = Field(description="Waiting for a maintainer's approval: no lane, mode manual.")


class CatchUp(BaseModel):
    """What the org webhook brought in after the sweep mark: the org sweep done Desk side."""

    since: str | None = Field(description="The time the cursor was read from: `since`, or a time mark.")
    after: int = Field(
        description="The event seq it starts after: the sweep mark, or the one before `since`."
    )
    until: int = Field(description="The last event it covers; `advance` stores it as the sweep mark.")
    new: list[CatchUpRow] = Field(description="Subjects first seen in the window.")
    reopened: list[CatchUpRow] = Field(
        description="Subjects open again at the source whose item is closed, changed in the window."
    )
    truncated: bool = Field(
        False, description="True when the window stopped at 1000 events; `until` is where it stopped."
    )
    advanced: bool = Field(False, description="True when this round moved the sweep mark to `until`.")


class ChangeRow(BaseModel):
    """An item someone else changed since the reader looked: what moved, who moved it, its NEXT now."""

    id: str
    kind: Kind
    task: str
    state: State
    mode: Mode
    seat: str | None
    outcome_label: str | None = Field(None, description="The outcome the work serves.")
    new: bool = Field(description="Created in the window.")
    fields: list[str] = Field(description="The fields its writes in the window set.")
    by: list[str] = Field(description="Who wrote them, first writer first.")
    writes: int = Field(description="How many writes in the window.")
    at: str = Field(description="When it last changed.")
    step: str | None = Field(
        None, description="NEXT: its blocker, a landed or overdue flag, else its next or result."
    )


class Changes(BaseModel):
    """`ashigaru changes`: what others changed after the reader's own seen mark, each item once."""

    since: str | None = Field(description="The time the window was read from, when `since` was given.")
    seen_at: str | None = Field(description="When the reader last acked: the mark this window starts at.")
    after: int = Field(description="The event seq it starts after: the seen mark, or the one before `since`.")
    until: int = Field(description="The last event it covers; an ack stores it as the seen mark.")
    items: list[ChangeRow] = Field(description="Items changed in the window, last changed first.")
    truncated: bool = Field(False, description="True when the window stopped at 1000 events.")
    acked: bool = Field(False, description="True when this look moved the reader's seen mark to `until`.")


class Seat(BaseModel):
    name: str
    lane: Lane | None = None
    status: SeatStatus = SeatStatus.FREE
    owner: str | None = Field(None, description="Who holds a manual seat (Scott), else null.")
    turn: Turn = Turn.IDLE
    task: str | None = Field(None, description="The session's first prompt.")
    item: str | None = Field(None, description="The item the seat holds.")
    since: str | None = Field(None, description="When status last changed.")
    updated_at: str | None = Field(None, description="Its last check-in; null if it never checked in.")
    model: str | None = None
    billing: Billing | None = None
    context_used: int | None = Field(None, description="Tokens in the context window now.")
    context_window: int | None = None
    session_id: str | None = None
    session_tokens: int | None = Field(None, description="Tokens used this session.")
    session_cost_usd: float = 0.0
    turn_started_at: str | None = Field(None, description="When the turn last went active.")
    turn_reconciled_at: str | None = Field(
        None,
        description="When seat reconcile ended a turn no hook did (active past ASHIGARU_TURN_MAX with no "
        "write for 10 minutes); null once a turn starts.",
    )
    silent_since: str | None = Field(
        None,
        description="When seat reconcile found a busy seat silent (no write for ASHIGARU_SILENT_AFTER); "
        "null once the seat writes again.",
    )
    waiting_on: str | None = Field(
        None, description="The seat's last message before it went idle, trimmed; null once a turn starts."
    )
    epoch: int = Field(0, description="The current session's epoch; a write with another is refused.")
    lease_until: str | None = Field(
        None, description="Until when the seat counts as up without another request."
    )
    missing: bool = Field(
        False,
        description="In `GET /seats`: a seat identity that never checked in, or whose row has not "
        "changed in STALE_SEAT.",
    )

    @computed_field(description="free, busy-auto (Josui's item) or busy-manual (Scott's hold).")
    def hold(self) -> Literal["free", "busy-auto", "busy-manual"]:
        busy: Literal["busy-auto", "busy-manual"] = "busy-manual" if self.owner else "busy-auto"
        return busy if self.status == SeatStatus.BUSY else "free"


WAITING_LENGTH = 2000
"""The longest `waiting_on` a seat may send; its stop hook trims well under it."""


class ChangesAck(BaseModel):
    """POST /v1/changes: how far the caller read, so the ack never passes what it was shown."""

    model_config = ConfigDict(extra="forbid")

    until: int | None = Field(None, ge=0, description="The `until` of the look the caller was shown.")


class SeatPatch(BaseModel):
    """A seat may send SEAT_ROW_FIELDS for its own row. The rest are Josui's: lane, status, the owner
    that is Scott's manual hold, and the item it holds. `since` is server-set."""

    model_config = ConfigDict(extra="forbid")

    lane: Lane | None = None
    status: SeatStatus | None = None
    owner: str | None = None
    turn: Turn | None = None
    task: str | None = None
    item: str | None = None
    waiting_on: str | None = Field(None, max_length=WAITING_LENGTH)


STALE_SEAT = timedelta(hours=24)
"""A seat whose row has not changed for this long is idle long enough for reconcile to drop its task."""
LEASE = timedelta(minutes=10)
"""How long an idle seat counts as up after its last request: its `next` hook asks every 90 seconds."""
LEASE_ACTIVE = timedelta(minutes=60)
"""How long a seat mid-turn counts as up after its last request: a long tool call posts nothing."""
ACK_AFTER = timedelta(minutes=10)
"""How long an item `next` handed a seat may wait for the seat's next turn before it is queued again."""

SEAT_ROW_FIELDS = frozenset({"turn", "task", "waiting_on"})
"""`since` is in neither: the Desk stamps it when status changes, so no one can jump the pick queue."""


class SessionStart(BaseModel):
    """POST /v1/seats/{name}/session: the seat's SessionStart hook registering its Claude Code session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    source: Literal["startup", "resume", "clear"]


class SessionStarted(BaseModel):
    """The epoch the session sends as X-Ashigaru-Epoch from now on, and the seat as it stands."""

    epoch: int
    seat: "Seat"


class UsageReport(BaseModel):
    """What a seat's hooks and status line send. `cost_usd` is the session's running total, as Claude
    Code reports it; the Desk books the difference from the last report against today's spend."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    model: str | None = None
    billing: Billing | None = None
    context_used: int | None = Field(None, ge=0)
    context_window: int | None = Field(None, ge=0)
    session_tokens: int | None = Field(None, ge=0)
    cost_usd: float | None = Field(None, ge=0)


class SeatUsage(BaseModel):
    """One row of `ashigaru usage`."""

    name: str
    model: str | None
    billing: Billing | None
    context_used: int | None
    context_window: int | None
    session_tokens: int | None
    session_cost_usd: float
    turn: Turn
    turn_seconds: int | None = Field(description="Time in the current turn; null when idle.")


class Spend(BaseModel):
    billing: Billing
    today_usd: float
    month_usd: float
    daily_limit_usd: float | None
    monthly_limit_usd: float | None


class SeatSummary(BaseModel):
    up: int = Field(description="Seats not missing.")
    seats: int
    open: int = Field(description="Open items: the rows of `items`.")


class Round(BaseModel):
    """`ashigaru work`: everything the check round reads, in one call."""

    items: list[CheckRow] = Field(description="The check table: open items, blocked first, then newest.")
    truncated: bool = Field(False, description="True when `items` stopped at its limit of 1000.")
    seats: list[Seat]
    summary: SeatSummary
    queue: Queue
    catch_up: CatchUp


class BoardRow(BaseModel):
    """One busy seat on the board: who holds it, the work, how long, its last word, the whole task."""

    seat: str
    mode: Mode = Field(description="manual for Scott's hold, else the work's mode.")
    work: str | None = Field(description="The id of the work item the seat holds.")
    state: State | None = Field(description="The work's state.")
    since: str | None = Field(description="When the seat went busy.")
    active: str | None = Field(description="The seat's last check-in: a hook, or the status line.")
    turn: Turn
    task: str | None = Field(description="The work's task, else the session's first prompt; never clipped.")
    missing: bool = False
    outcome_label: str | None = Field(None, description="The outcome the work serves.")
    pr: str | None = Field(None, description="The PR carrying the work, owner/repo#N.")
    done_when: str | None = Field(None, description="The work's done-when.")
    check_after: str | None = Field(None, description="When Prime looks at the work next.")
    step: str | None = Field(None, description="NEXT: the one next step.")


class NeedsYou(BaseModel):
    """Something only Scott can move: his blocked work, a subject held for approval, a manual seat
    idle on a question, a hold of his on a seat silent for a day, a busy seat gone silent (`silent`),
    an outsider's subject the sweep found and did not store, or a writer's PR intake did not queue
    because it names no item while a seat works in its repo (`unlinked`: link it with `update --pr`)."""

    why: Literal["blocked", "held", "waiting", "outsider", "stale", "silent", "unlinked"]
    work: str | None
    seat: str | None = None
    mode: Mode | None
    state: State | None
    text: str | None = Field(description="The blocker, the approval asked for, or the seat's last word.")
    task: str | None = Field(None, description="The work's task.")
    outcome_label: str | None = Field(None, description="The outcome the work serves.")
    since: str | None = Field(None, description="When the work last changed.")
    pr: str | None = Field(None, description="The PR carrying the work, owner/repo#N.")
    done_when: str | None = Field(None, description="The work's done-when.")
    check_after: str | None = Field(None, description="When Prime looks at the work next.")
    step: str | None = Field(None, description="NEXT: the one next step.")


class Outsider(BaseModel):
    """A subject the sweep found by someone who is not a member or a trusted author: never stored as
    an item, only listed for Scott."""

    subject_key: str
    ref: str | None = None
    author: str | None = None


class SweepRun(BaseModel):
    """One run of the sweep: what started it, when, what it read, queued and closed, and why it
    stopped short, if it did."""

    id: int
    trigger: Literal["startup", "timer", "request"]
    started_at: str
    finished_at: str | None = Field(description="null while it runs.")
    subjects: int = Field(description="Subjects read from the forge.")
    new: int = Field(description="Subjects queued (or held) that had no item.")
    closed: int = Field(description="Items it settled: closed, requeued or marked landed.")
    error: str | None = Field(description="The first thing it could not read, e.g. `search: HTTP 403`.")
    outsiders: list[Outsider] = Field(default_factory=list, description="New subjects by outsiders: listed.")
    skipped: str | None = Field(
        default=None,
        description="Why it did not run: another run's live lease, e.g. `run 41 holds the lease`.",
    )


class Sweep(BaseModel):
    """`ashigaru sweep`: whether the timer runs here, how often, if a run is under way, and the last."""

    configured: bool = Field(description="False without the forge's token and org: no timer runs.")
    every: float | None = Field(description="Seconds between runs.")
    running: bool
    requested: bool = Field(False, description="True when this call asked for a run now.")
    last: SweepRun | None


class Board(BaseModel):
    """`ashigaru status`: the busy seats in full, the free ones, the tally, and what needs Scott."""

    busy: list[BoardRow]
    free: list[str] = Field(description="Free seats that are up, by name.")
    missing: list[str] = Field(description="Free seats that never checked in or went quiet for a day.")
    summary: SeatSummary
    needs_you: list[NeedsYou]
    sweep: SweepRun | None = Field(None, description="The last sweep, so a person sees the fallback alive.")
    changes: Changes | None = Field(None, description="CHANGED SINCE YOU LOOKED, without moving the mark.")


class Usage(BaseModel):
    seats: list[SeatUsage]
    spend: list[Spend]


class BillingLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_usd: float | None = Field(None, ge=0)
    monthly_usd: float | None = Field(None, ge=0)


class OutcomeRow(BaseModel):
    """One row of `ashigaru outcomes`: an outcome and the state of its stories."""

    id: str
    task: str
    ref: str | None
    state: State
    total: int
    done: int
    blocked: int
    open: int


class Assign(BaseModel):
    """Name a seat, or let the Desk pick the longest-free unheld seat in the lane."""

    model_config = ConfigDict(extra="forbid")

    seat: str | None = None
    lane: Lane | None = Field(None, description="Defaults to the item's lane.")


class NewItem(BaseModel):
    """What `ashigaru assign --task` records before it assigns: the item, in the assign's lane."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    done_when: str | None = None
    ref: str | None = None
    repo: str | None = None
    after: list[str] = Field(default_factory=list)
    mode: Mode | None = None
    subject_key: str | None = None
    outcome: str | None = Field(None, description="As ItemCreate's: the outcome it serves.")
    outcome_text: str | None = Field(None, description="As ItemCreate's: Scott's instruction, no ticket.")


class Identity(BaseModel):
    name: str
    role: Role
    created_at: str
    disabled_at: str | None = None


class IdentityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    role: Role


class ApiKey(BaseModel):
    key_id: str
    identity: str
    label: str | None
    created_at: str
    revoked_at: str | None
    last_used_at: str | None


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None


class MintedKey(ApiKey):
    token: str = Field(description="Shown once. Send it as `Authorization: Bearer <token>`.")


class Verdict(StrEnum):
    """One check's verdict, whatever forge ran it. A forge adapter maps its own conclusions onto these."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    NEUTRAL = "neutral"


class CIStatus(StrEnum):
    """A commit's CI as a whole: pending while any check is (or none has reported, or a check the
    target branch requires has not), else failure when any check failed or was cancelled, else success."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


class Check(BaseModel):
    name: str
    verdict: Verdict
    url: str | None = None
    updated_at: str = Field(description="When the forge last changed it.")


class CIResult(BaseModel):
    """The normalized CI result for one commit: what `desk_wait` and `ashigaru wait` read."""

    repo: str = Field(description="owner/name, lower case.")
    sha: str
    status: CIStatus
    checks: list[Check] = Field(default_factory=list)
    failing: list[str] = Field(
        default_factory=list, description="Names of the checks that failed or were cancelled."
    )
    pending: list[str] = Field(
        default_factory=list,
        description="Names of the checks with no conclusion yet: the commit's own, and each required check "
        "that has not reported (one that never started counts).",
    )
    branch: str | None = Field(default=None, description="The branch whose required checks were read.")
    required: list[str] | None = Field(
        default=None,
        description="The checks that branch requires; null when they were not, or could not be, read.",
    )
    unverified: str | None = Field(
        default=None,
        description="Why the required checks could not be read; the status is then never success.",
    )


BRANCH_MAX = 255
"""The longest branch name GitHub takes (a ref's file name)."""

MAX_WAIT = 90
"""Seconds one wait may block. Cloudflare drops a proxied request at 100s, so a client waits again."""


class NextWait(BaseModel):
    """POST /v1/seats/{name}/next query parameters."""

    wait: int = Field(MAX_WAIT, ge=0, le=MAX_WAIT, description="Seconds to wait for work; 0 asks once.")


class NextItem(BaseModel):
    """What a free seat claimed: the item, and the brief its hook hands Claude Code."""

    item: Item
    brief: str


class CIWait(BaseModel):
    """GET /v1/ci/wait query parameters."""

    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$", description="owner/name")
    sha: str = Field(pattern=r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$", description="The full commit id.")
    timeout: int = Field(MAX_WAIT, ge=0, le=MAX_WAIT, description="Seconds to wait; 0 reads now.")
    branch: str | None = Field(
        None,
        max_length=BRANCH_MAX,
        pattern=r"^[^\s~^:?*\[\\]+$",
        description="The branch the commit targets, whose required checks it must pass; default the repo's.",
    )


class SubjectState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class Subject(BaseModel):
    """The forge's own state of an issue or PR, as its webhook (and later the sweep) last said."""

    subject_key: str
    repo: str | None
    kind: Kind
    state: SubjectState
    head_sha: str | None = None
    draft: bool | None = None
    review: str | None = Field(None, description="approved, changes_requested or dismissed; null for none.")
    title: str | None = Field(None, description="The source's title: data, never a brief.")
    author: str | None = None
    labels: list[str] = Field(default_factory=list)
    source: str = Field(description="What wrote it last: webhook, or sweep.")
    source_updated_at: str | None = Field(None, description="When the forge last changed it.")
    seen_at: str


class WebhookAck(BaseModel):
    """The whole answer to a verified delivery; nothing from the delivery is echoed."""

    duplicate: bool = Field(
        description="True when the same signed delivery (forge, event and body) was already processed."
    )
    handled: bool = Field(description="True when an adapter mapped the event to CI or intake.")


MARK_NAME = r"^[a-z][a-z0-9-]{0,63}$"
"""A watermark's name, the same for REST and MCP."""


class Mark(BaseModel):
    """A named watermark Josui keeps on the Desk, such as where the last sweep reached."""

    name: str
    value: str
    updated_at: str
    updated_by: str


class MarkSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, description="Usually a time, YYYY-MM-DDTHH:MM:SSZ.")
