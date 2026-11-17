"""The Desk's vocabulary: item, seat and identity, as the API sends and receives them.

The item carries every field and state the /agents ledger uses (ashigaru-3.md,
"The Desk"), plus the queue fields the design adds (kind, parent, subject_key,
repo, priority).
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    learned: str | None = None
    note: str | None = None
    blocked_reason: str | None = None
    memorized: bool | None = None
    verified_by: str | None = None
    priority: int | None = None
    wrap: Wrap | None = None


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
    """GET /v1/items query parameters; each one given must match."""

    state: State | None = None
    seat: str | None = None
    lane: Lane | None = None
    kind: Kind | None = None
    mode: Mode | None = None
    parent: str | None = None


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
    updated_at: str


class SeatPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lane: Lane | None = None
    status: SeatStatus | None = None
    owner: str | None = None
    turn: Turn | None = None
    task: str | None = None
    item: str | None = None


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
