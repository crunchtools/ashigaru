"""GitHub: X-Hub-Signature-256, X-GitHub-Delivery, and the events the Desk takes.

CI comes from `workflow_run` (requested, in_progress, completed: one check per
workflow and triggering event) and `check_suite` completed (one check per App
that is not GitHub Actions, whose suites `workflow_run` already names better).
An org webhook receives only `completed` for check suites, so another App's
check appears once it finishes.

Intake comes from `issues` (an outcome) and `pull_request` (a story), keyed
`owner/name#number`; GitHub numbers issues and PRs from one sequence. A label
`lane:<lane>` is the lane hint.
"""

import hashlib
import hmac
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from ..model import Kind, Verdict, subject_key
from .base import CIUpdate, Delivery, IntakeItem, payload_get

SIGNATURE = "x-hub-signature-256"
EVENT = "x-github-event"
API = "https://api.github.com"
WRITERS = frozenset({"admin", "maintain", "write"})
"""Collaborator permissions that may write. GitHub reports `maintain` as write and `triage` as read
in this field; `maintain` is named so the rule does not depend on that mapping."""
ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
"""A person's login. A bot's (`name[bot]`) never matches, so a bot's intake is always held."""
REPO = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")
ACTIONS_APP = "github-actions"
LANE_LABEL = "lane:"
NOT_A_COLLABORATOR = 404
LOG = logging.getLogger(__name__)

CONCLUSIONS = {
    "success": Verdict.SUCCESS,
    "failure": Verdict.FAILURE,
    "cancelled": Verdict.CANCELLED,
    "skipped": Verdict.SKIPPED,
    "neutral": Verdict.NEUTRAL,
    "timed_out": Verdict.FAILURE,
    "action_required": Verdict.FAILURE,
    "startup_failure": Verdict.FAILURE,
    "stale": Verdict.FAILURE,
}
"""Anything GitHub adds later that is not here counts as a failure, never as a pass."""


def _verdict(status: Any, conclusion: Any) -> Verdict:
    return CONCLUSIONS.get(str(conclusion), Verdict.FAILURE) if status == "completed" else Verdict.PENDING


def _ordinal(value: Any, what: str) -> str:
    """A GitHub id or attempt number, zero-padded so that text order is number order."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"no {what}")
    return f"{value:020d}"


def _repo(payload: Mapping[str, Any]) -> str:
    full_name = payload_get(payload, "repository", "full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        raise ValueError("no repository.full_name")
    return full_name.lower()


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str | int) or isinstance(value, bool) or value == "":
        raise ValueError(f"no {what}")
    return str(value)


def workflow_run(payload: Mapping[str, Any]) -> tuple[CIUpdate, ...]:
    run = payload.get("workflow_run")
    if not isinstance(run, Mapping):
        raise ValueError("no workflow_run")
    trigger = _text(run.get("event"), "workflow_run.event")
    attempt = _ordinal(run.get("run_attempt") or 1, "workflow_run.run_attempt")
    return (
        CIUpdate(
            repo=_repo(payload),
            sha=_text(run.get("head_sha"), "workflow_run.head_sha").lower(),
            key=f"workflow:{_text(run.get('workflow_id'), 'workflow_run.workflow_id')}:{trigger}",
            name=f"{_text(run.get('name'), 'workflow_run.name')} ({trigger})",
            verdict=_verdict(run.get("status"), run.get("conclusion")),
            run=f"{_ordinal(run.get('id'), 'workflow_run.id')}:{attempt}",
            updated_at=_text(run.get("updated_at"), "workflow_run.updated_at"),
            url=run.get("html_url") if isinstance(run.get("html_url"), str) else None,
        ),
    )


def check_suite(payload: Mapping[str, Any]) -> tuple[CIUpdate, ...]:
    suite = payload.get("check_suite")
    if not isinstance(suite, Mapping):
        raise ValueError("no check_suite")
    found = suite.get("app")
    app: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    if payload.get("action") != "completed" or app.get("slug") == ACTIONS_APP:
        return ()
    return (
        CIUpdate(
            repo=_repo(payload),
            sha=_text(suite.get("head_sha"), "check_suite.head_sha").lower(),
            key=f"app:{_text(app.get('id'), 'check_suite.app.id')}",
            name=_text(app.get("name") or app.get("slug"), "check_suite.app.name"),
            verdict=_verdict(suite.get("status"), suite.get("conclusion")),
            run=_ordinal(suite.get("id"), "check_suite.id"),
            updated_at=_text(suite.get("updated_at"), "check_suite.updated_at"),
        ),
    )


def _intake(payload: Mapping[str, Any], subject: str, kind: Kind) -> tuple[IntakeItem, ...]:
    if payload.get("action") == "deleted":
        return ()
    thing = payload.get(subject)
    if not isinstance(thing, Mapping):
        raise ValueError(f"no {subject}")
    repo = _repo(payload)
    labels = tuple(
        label["name"]
        for label in thing.get("labels") or ()
        if isinstance(label, Mapping) and isinstance(label.get("name"), str)
    )
    lanes = [label[len(LANE_LABEL) :] for label in labels if label.startswith(LANE_LABEL)]
    return (
        IntakeItem(
            subject_key=_subject_key(repo, _text(thing.get("number"), f"{subject}.number")),
            title=_text(thing.get("title"), f"{subject}.title"),
            ref=_text(thing.get("html_url"), f"{subject}.html_url"),
            source="github",
            repo=repo,
            kind=kind,
            closed=thing.get("state") == "closed",
            lane_hint=lanes[0] if lanes else None,
            labels=labels,
            author=author if isinstance(author := payload_get(thing, "user", "login"), str) else None,
            sender=sender if isinstance(sender := payload_get(payload, "sender", "login"), str) else None,
            approves_lane=_lane_label(payload, "labeled"),
            revokes_lane=_lane_label(payload, "unlabeled"),
            head=head.lower() if isinstance(head := payload_get(thing, "head", "sha"), str) else None,
        ),
    )


def _subject_key(repo: str, number: str) -> str:
    """The queue's dedupe key, in the one form every writer uses (model.subject_key)."""
    key = subject_key(f"{repo}#{number}")
    if key is None:
        raise ValueError(f"{repo}#{number} is not an owner/repo#N subject")
    return key


def _lane_label(payload: Mapping[str, Any], action: str) -> str | None:
    """The lane a `labeled` event adds or an `unlabeled` one removes, when its label is `lane:<lane>`."""
    label = payload_get(payload, "label", "name")
    matched = payload.get("action") == action and isinstance(label, str) and label.startswith(LANE_LABEL)
    return label[len(LANE_LABEL) :] if matched else None


class GitHub:
    name = "github"

    def verify(self, headers: Mapping[str, str], body: bytes, secret: bytes) -> bool:
        sent = headers.get(SIGNATURE, "")
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sent.encode(), expected.encode())

    def parse(self, headers: Mapping[str, str], body: bytes) -> Delivery:
        event = headers.get(EVENT, "")
        if not event:
            raise ValueError("no event name")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("the payload is not JSON; set the webhook's content type to JSON") from error
        if not isinstance(payload, Mapping):
            raise ValueError("the payload is not an object")
        if event == "workflow_run":
            return Delivery(event, ci=workflow_run(payload))
        if event == "check_suite":
            return Delivery(event, ci=check_suite(payload))
        if event == "issues":
            return Delivery(event, intake=_intake(payload, "issue", Kind.OUTCOME))
        if event == "pull_request":
            return Delivery(event, intake=_intake(payload, "pull_request", Kind.STORY))
        return Delivery(event)

    def may_write(self, repo: str, account: str, token: str) -> bool:
        """The collaborator permission API: write, maintain or admin. A bot, an outsider (404), a
        failed call or a name that is not a plain account or repo is no; a failure is logged with
        its status or type, never the token or the response."""
        if not (token and REPO.match(repo) and ACCOUNT.match(account)):
            return False
        request = urllib.request.Request(
            f"{API}/repos/{repo}/collaborators/{account}/permission",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ashigaru-desk",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                found = json.load(response)
        except urllib.error.HTTPError as error:
            # 404 is GitHub's answer for an account that is not a collaborator: an ordinary no.
            level = logging.DEBUG if error.code == NOT_A_COLLABORATOR else logging.WARNING
            LOG.log(level, "permission check for %s on %s: HTTP %s", account, repo, error.code)
            return False
        except (OSError, ValueError) as error:
            LOG.warning("permission check for %s on %s failed: %s", account, repo, type(error).__name__)
            return False
        return isinstance(found, Mapping) and found.get("permission") in WRITERS


FORGE = GitHub()
