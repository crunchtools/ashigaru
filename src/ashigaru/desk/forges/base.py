"""The shapes every forge adapter maps into, and the protocol it implements."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..model import Kind, Verdict


@dataclass(frozen=True)
class CIUpdate:
    """One check of one commit, as the forge last reported it.

    `key` is stable across re-runs of the same check, so a re-run replaces it.
    `run` names this run and sorts as text in run order (a re-run after its
    original); `updated_at` (the forge's clock, UTC) orders deliveries of one
    run that arrive out of order.
    """

    repo: str
    sha: str
    key: str
    name: str
    verdict: Verdict
    run: str
    updated_at: str
    url: str | None = None


@dataclass(frozen=True)
class IntakeItem:
    """One unit of incoming work, whatever its source. `subject_key` dedupes it in the queue."""

    subject_key: str
    title: str
    ref: str
    source: str
    repo: str | None
    kind: Kind
    closed: bool = False
    lane_hint: str | None = None
    labels: tuple[str, ...] = field(default_factory=tuple)
    author: str | None = None
    """Who opened the subject: if they may write to the repo, the work is claimable from the start."""
    sender: str | None = None
    """Who caused this event; it counts only when the event is an approval (`approves_lane`)."""
    approves_lane: str | None = None
    """The lane this event explicitly gives the subject (on a forge with labels, adding a lane label). From
    a sender who may write, that is a maintainer's approval and makes a held item claimable."""
    revokes_lane: str | None = None
    """The lane this event explicitly takes away (removing a lane label). From a sender who may write,
    removing the approved lane withdraws the approval."""
    head: str | None = None
    """The content the subject stands at, where the forge versions it (a PR's head commit). Approval is
    tied to it: any delivery at another head withdraws the approval, unless a writer's push moved it."""
    head_before: str | None = None
    """The head a push moved the subject from, where the forge says. Only a writer's push from the
    approved head moves the approval with it."""
    merged: bool = False
    """The subject is a change the forge merged; `closed` is then true too."""
    draft: bool | None = None
    """A change still in draft; None where the forge has no drafts, or the event does not say."""
    review: str | None = None
    """The review this event submits or dismisses: approved, changes_requested or dismissed."""
    updated_at: str | None = None
    """When the forge last changed the subject (UTC), so an older delivery never overwrites a newer."""
    author_writes: bool = False
    """Set by the Desk, never by an adapter's parse: the author may write to the repo."""
    approved: bool = False
    """Set by the Desk, never by an adapter's parse: the sender may write and this is an approval."""
    revoked: bool = False
    """Set by the Desk, never by an adapter's parse: a writer removed the approved lane."""
    pushed_by_writer: bool = False
    """Set by the Desk, never by an adapter's parse: a writer pushed from the approved head to `head`."""
    judged_at: int | None = None
    """Set by the Desk, never by an adapter's parse: the item's last event when it was judged, 0 when
    there was no item. A row that changed since loses whatever the judging granted."""


@dataclass(frozen=True)
class Found:
    """One subject a sweep search found."""

    item: IntakeItem
    association: str
    """The author's tie to the org, as the adapter maps it: OWNER and MEMBER count as writers; anything
    else (CONTRIBUTOR, NONE) does not."""
    body: str
    """The subject's text, empty when it has none."""


@dataclass(frozen=True)
class Search:
    """A sweep search's hits, oldest change first, and whether it read them all: False when the
    forge's cap cut it short, so the caller reads on from the last hit."""

    hits: list[Found]
    complete: bool


@dataclass(frozen=True)
class Delivery:
    """A verified delivery, mapped. An event the adapter does not handle maps to neither."""

    event: str
    ci: tuple[CIUpdate, ...] = ()
    intake: tuple[IntakeItem, ...] = ()


class Forge(Protocol):
    name: str
    """The adapter's settings name: its secret is ASHIGARU_WEBHOOK_SECRET_<NAME>, its API token
    ASHIGARU_FORGE_TOKEN_<NAME>."""

    def verify(self, headers: Mapping[str, str], body: bytes, secret: bytes) -> bool:
        """Timing-safe: True only when `body` was signed with `secret`."""
        ...

    def parse(self, headers: Mapping[str, str], body: bytes) -> Delivery:
        """Map a verified delivery. Raises ValueError for one it cannot read."""
        ...

    def may_write(self, repo: str, account: str, token: str) -> bool:
        """True only when the forge says `account` may write to `repo`; False on any doubt (no
        answer, an error, a name it would not ask about)."""
        ...

    def required(self, repo: str, sha: str, branch: str | None, token: str) -> tuple[str, dict[str, Verdict]]:
        """The branch (`branch`, else the repo's default) and the verdict on `sha` of each check it
        requires, pending for one that has not reported. Raises OSError or ValueError when the forge
        cannot say."""
        ...

    def subject(self, repo: str, number: int, token: str) -> IntakeItem:
        """One issue or PR as a delivery would have mapped it, read from the forge. Raises OSError or
        ValueError when the forge cannot say."""
        ...

    def updated(self, org: str, since: str, token: str) -> Search:
        """Every subject in `org` changed at or after `since` (a UTC time as ISO 8601 with a Z),
        oldest change first. Raises OSError or ValueError when the forge cannot say."""
        ...


def payload_get(payload: Any, *path: str) -> Any:
    """payload[path[0]][path[1]]..., or None where any step is missing or not a mapping."""
    for step in path:
        if not isinstance(payload, Mapping):
            return None
        payload = payload.get(step)
    return payload
