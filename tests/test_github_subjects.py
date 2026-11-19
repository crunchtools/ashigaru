"""The GitHub adapter's subject state: merged, draft, review and updated_at."""

import json
from pathlib import Path
from typing import Any

from ashigaru.desk.forges.base import IntakeItem
from ashigaru.desk.forges.github import FORGE
from ashigaru.desk.model import Kind

FIXTURES = Path(__file__).parent / "fixtures" / "github"
UPDATED = "2026-11-19T10:00:00Z"


def item(fixture: str, event: str, subject: str, action: str | None = None, **changes: Any) -> IntakeItem:
    body: dict[str, Any] = json.loads((FIXTURES / f"{fixture}.json").read_text())
    body[subject] = {**body[subject], **changes}
    if action:
        body["action"] = action
    delivery = FORGE.parse({"x-github-event": event}, json.dumps(body).encode())
    (found,) = delivery.intake
    return found


def pull(action: str = "opened", **changes: Any) -> IntakeItem:
    return item("pull_request", "pull_request", "pull_request", action, **changes)


def review(action: str, state: str | None = None) -> IntakeItem:
    body: dict[str, Any] = json.loads((FIXTURES / "pull_request_review.json").read_text())
    body["action"] = action
    body["review"]["state"] = state
    delivery = FORGE.parse({"x-github-event": "pull_request_review"}, json.dumps(body).encode())
    (found,) = delivery.intake
    return found


def test_merged_pull_request_closed() -> None:
    found = pull("closed", state="closed", merged=True, updated_at=UPDATED)
    assert found.merged and found.closed and found.updated_at == UPDATED


def test_merged_at_alone_counts_as_merged() -> None:
    assert pull("closed", state="closed", merged_at=UPDATED).merged
    assert not pull("closed", state="closed", merged_at=None).merged


def test_closed_unmerged_pull_request() -> None:
    found = pull("closed", state="closed", merged=False)
    assert found.closed and not found.merged


def test_draft_pull_request_opened() -> None:
    assert pull(draft=True).draft is True
    assert pull(draft=False).draft is False
    assert pull().draft is None


def test_issue_has_no_merge_or_draft() -> None:
    found = item("issues", "issues", "issue", updated_at=UPDATED, merged=True, draft=True)
    assert found.merged is False and found.draft is None and found.updated_at == UPDATED


def test_review_approved() -> None:
    found = review("submitted", "APPROVED")
    assert found.review == "approved" and found.kind is Kind.STORY
    assert found.updated_at == UPDATED and found.merged is False
    assert found.head == "acb5820ced9479c074f688cc328bf03f341a511d"


def test_review_changes_requested() -> None:
    assert review("submitted", "changes_requested").review == "changes_requested"


def test_review_comment_changes_nothing() -> None:
    assert review("submitted", "commented").review is None
    assert review("edited", "approved").review is None


def test_review_dismissed() -> None:
    assert review("dismissed", "dismissed").review == "dismissed"


def test_review_never_approves_a_lane() -> None:
    found = review("submitted", "approved")
    assert found.approves_lane is None and found.revokes_lane is None and found.head_before is None
