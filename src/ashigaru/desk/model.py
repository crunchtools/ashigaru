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
RT_URL = re.compile(r"^https?://\S+/Ticket/\w+\.html\?(?:\S*&)?id=(\d+)\b", re.I)
RT_REF = re.compile(r"^RT\s*#?\s*(\d+)$", re.I)
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

    @field_validator("subject_key")
    @classmethod
    def _one_form(cls, value: str | None) -> str | None:
        """A ref is stored as its key, any other key (`repo:alert`) trimmed, and a blank one as none."""
        return subject_key(value) or (value or "").strip() or None


class ItemCreate(_Fields):
    """Josui only. `id` is generated (a<N+1>) when omitted; `state` defaults to queued."""

    id: str | None = Field(None, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    task: str


class ItemPatch(_Fields):
    """Only the fields sent change; null clears a field. Seats may send SEAT_FIELDS only."""


SEAT_FIELDS = frozenset(
    {"state", "result", "next", "learned", "note", "blocked_reason", "memorized", "check_after"}
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


class Event(BaseModel):
    seq: int
    item_id: str
    at: str
    actor: str
    source: str
    patch: dict[str, Any]


class CheckRow(BaseModel):
    """One row of the /agents check table."""

    id: str
    seat: str | None
    mode: Mode
    task: str
    state: State
    wrap: Wrap | None
    result_or_blocker: str | None
    next: str | None


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
    missing: bool = Field(
        False,
        description="In `GET /seats`: a seat identity that never checked in, or whose row has not "
        "changed in STALE_SEAT.",
    )

    @computed_field(description="free, busy-auto (Josui's item) or busy-manual (Scott's hold).")
    def hold(self) -> Literal["free", "busy-auto", "busy-manual"]:
        busy: Literal["busy-auto", "busy-manual"] = "busy-manual" if self.owner else "busy-auto"
        return busy if self.status == SeatStatus.BUSY else "free"


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


STALE_SEAT = timedelta(hours=24)
"""A seat whose row has not changed for this long is shown as missing."""

SEAT_ROW_FIELDS = frozenset({"turn", "task"})
"""`since` is in neither: the Desk stamps it when status changes, so no one can jump the pick queue."""


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
    """A commit's CI as a whole: pending while any check is (or none has reported), else failure when
    any check failed or was cancelled, else success."""

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


MAX_WAIT = 90
"""Seconds one wait may block. Cloudflare drops a proxied request at 100s, so a client waits again."""


class CIWait(BaseModel):
    """GET /v1/ci/wait query parameters."""

    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$", description="owner/name")
    sha: str = Field(pattern=r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$", description="The full commit id.")
    timeout: int = Field(MAX_WAIT, ge=0, le=MAX_WAIT, description="Seconds to wait; 0 reads now.")


class WebhookAck(BaseModel):
    """The whole answer to a verified delivery; nothing from the delivery is echoed."""

    duplicate: bool = Field(
        description="True when the same signed delivery (forge, event and body) was already processed."
    )
    handled: bool = Field(description="True when an adapter mapped the event to CI or intake.")


class Mark(BaseModel):
    """A named watermark Josui keeps on the Desk, such as where the last sweep reached."""

    name: str
    value: str
    updated_at: str
    updated_by: str


class MarkSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, description="Usually a time, YYYY-MM-DDTHH:MM:SSZ.")
