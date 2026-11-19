"""GitHub: X-Hub-Signature-256, X-GitHub-Delivery, and the events the Desk takes.

CI comes from `workflow_run` (requested, in_progress, completed: one check per
workflow and triggering event) and `check_suite` completed (one check per App
that is not GitHub Actions, whose suites `workflow_run` already names better).
An org webhook receives only `completed` for check suites, so another App's
check appears once it finishes.

Intake comes from `issues` (an outcome) and `pull_request` (a story), keyed
`owner/name#number`; GitHub numbers issues and PRs from one sequence. A label
`lane:<lane>` is the lane hint.

`required_checks` names the status checks a branch requires (rulesets, org ones included, and classic
protection), each with the App it must come from, and `reported` what each name and App has said on a
commit; `GitHub.required` joins them by exact name and App, a required check nobody has reported being
pending. Errors propagate: the caller turns them into "could not read".
"""

import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.parse
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
RULES_TTL = 60.0
"""Seconds a branch's required checks are remembered."""
PAGE = 100
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


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ashigaru-desk",
    }


def _get(url: str, token: str) -> Any:
    """An authenticated GET, parsed as JSON. HTTPError, OSError and ValueError propagate."""
    request = urllib.request.Request(url, headers=_headers(token))
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def _items(found: Any, key: str | None = None) -> list[Mapping[str, Any]]:
    """The mappings in a list (or in `found[key]`); anything else is skipped."""
    if key is not None:
        found = found.get(key) if isinstance(found, Mapping) else None
    return [item for item in found if isinstance(item, Mapping)] if isinstance(found, list) else []


Requirement = tuple[str, int | None]
"""A required check: its exact name, and the App that must report it (None: any App or a commit status)."""


def _app(value: Any) -> int | None:
    """A required App id; absent, null or -1 (GitHub's "any source") is any."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _contexts(found: Any, app: str) -> list[Requirement]:
    """Each `{context, <app>}` entry as a Requirement: ruleset entries name the App `integration_id`,
    classic protection's `app_id`."""
    return [(c, _app(item.get(app))) for item in _items(found) if isinstance(c := item.get("context"), str)]


_rules: dict[tuple[str, str | None], tuple[float, tuple[str, tuple[Requirement, ...]]]] = {}
_clock = time.monotonic


def _valid(repo: str, token: str) -> str:
    repo = repo.lower()
    if not (token and REPO.match(repo)):
        raise ValueError("a repo as owner/name and a token are needed")
    return repo


def _default_branch(base: str, token: str) -> str:
    found = _get(base, token)
    name = found.get("default_branch") if isinstance(found, Mapping) else None
    if not isinstance(name, str) or not name:
        raise ValueError("no default_branch")
    return name


def _ruleset_checks(base: str, quoted: str, token: str) -> set[Requirement]:
    """Every `required_status_checks` rule on the branch, repo and org rulesets alike, all pages."""
    names: set[Requirement] = set()
    page = 1
    while True:
        rules = _items(_get(f"{base}/rules/branches/{quoted}?per_page={PAGE}&page={page}", token))
        for rule in rules:
            parameters = rule.get("parameters")
            if rule.get("type") == "required_status_checks" and isinstance(parameters, Mapping):
                names.update(_contexts(parameters.get("required_status_checks"), "integration_id"))
        if len(rules) < PAGE:
            return names
        page += 1


def _classic_checks(base: str, quoted: str, token: str) -> set[Requirement]:
    """The branch's classic protection: its `checks`, and the legacy `contexts` GitHub mirrors them
    into, kept only for a name `checks` does not carry; none when it is unprotected."""
    classic = payload_get(_get(f"{base}/branches/{quoted}", token), "protection", "required_status_checks")
    checks = set(_contexts(payload_get(classic, "checks"), "app_id"))
    legacy = payload_get(classic, "contexts")
    named = {check for check, _ in checks}
    return checks | {
        (c, None)
        for c in (legacy if isinstance(legacy, list) else ())
        if isinstance(c, str) and c not in named
    }


def required_checks(repo: str, branch: str | None, token: str) -> tuple[str, tuple[Requirement, ...]]:
    """The branch (the default one when None) and the sorted checks it requires, by ruleset (org
    rulesets included) or classic protection, each with the App it must come from; remembered for
    RULES_TTL seconds."""
    repo = _valid(repo, token)
    now = _clock()
    hit = _rules.get((repo, branch))
    if hit and now - hit[0] < RULES_TTL:
        return hit[1]
    base = f"{API}/repos/{repo}"
    name = _default_branch(base, token) if branch is None else branch
    quoted = urllib.parse.quote(name, safe="")
    names = _ruleset_checks(base, quoted, token) | _classic_checks(base, quoted, token)
    result = (name, tuple(sorted(names, key=lambda need: (need[0], need[1] or 0))))
    _rules[(repo, branch)] = (now, result)
    return result


STATUSES = {"success": Verdict.SUCCESS, "pending": Verdict.PENDING}
"""A commit status's state; failure and error (and anything new) count as a failure."""
PASSING = (Verdict.SUCCESS, Verdict.SKIPPED, Verdict.NEUTRAL)


def _combine(verdicts: list[Verdict]) -> Verdict:
    """One name, several entries: any pending is pending, else any failure or cancel fails, else the
    first pass (success preferred)."""
    if Verdict.PENDING in verdicts:
        return Verdict.PENDING
    if Verdict.FAILURE in verdicts or Verdict.CANCELLED in verdicts:
        return Verdict.FAILURE
    if Verdict.SUCCESS in verdicts:
        return Verdict.SUCCESS
    return next((v for v in verdicts if v in PASSING), Verdict.FAILURE)


def _pages(url: str, key: str, token: str) -> list[Mapping[str, Any]]:
    """Every entry under `key` across the pages of `url` (which takes `&page=`), up to `total_count`."""
    entries: list[Mapping[str, Any]] = []
    page = 1
    while True:
        found = _get(f"{url}&page={page}", token)
        batch = _items(found, key)
        entries += batch
        total = found.get("total_count") if isinstance(found, Mapping) else None
        if len(batch) < PAGE or (isinstance(total, int) and len(entries) >= total):
            return entries
        page += 1


def reported(repo: str, sha: str, token: str) -> dict[Requirement, Verdict]:
    """What each check has reported on the commit, by name and the App that ran it: check runs
    (latest) and commit statuses (App None). Never cached."""
    repo = _valid(repo, token)
    base = f"{API}/repos/{repo}/commits/{urllib.parse.quote(sha, safe='')}"
    seen: dict[Requirement, list[Verdict]] = {}
    for run in _pages(f"{base}/check-runs?per_page={PAGE}&filter=latest", "check_runs", token):
        if isinstance(name := run.get("name"), str):
            app = _app(payload_get(run, "app", "id"))
            seen.setdefault((name, app), []).append(_verdict(run.get("status"), run.get("conclusion")))
    # The combined status: GitHub already keeps only the latest status of each context.
    for status in _pages(f"{base}/status?per_page={PAGE}", "statuses", token):
        if isinstance(context := status.get("context"), str):
            seen.setdefault((context, None), []).append(
                STATUSES.get(str(status.get("state")), Verdict.FAILURE)
            )
    return {name: _combine(verdicts) for name, verdicts in seen.items()}


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
            head_before=before.lower()
            if payload.get("action") == "synchronize" and isinstance(before := payload.get("before"), str)
            else None,
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
            headers=_headers(token),
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

    def required(self, repo: str, sha: str, branch: str | None, token: str) -> tuple[str, dict[str, Verdict]]:
        """The branch and each required check's verdict on `sha`; one nobody has reported is pending.
        Names match exactly, and a check required from an App counts only that App's check runs, as
        GitHub's merge gate does: a same-named status or another App's run does not satisfy it."""
        name, wanted = required_checks(repo, branch, token)
        by_name: dict[str, list[tuple[int | None, Verdict]]] = {}
        for (check, app), verdict in (reported(repo, sha, token) if wanted else {}).items():
            by_name.setdefault(check, []).append((app, verdict))
        verdicts: dict[str, list[Verdict]] = {}
        for check, app in wanted:
            matched = [v for a, v in by_name.get(check, []) if app is None or a == app]
            verdicts.setdefault(check, []).append(_combine(matched) if matched else Verdict.PENDING)
        return name, {check: _combine(found) for check, found in verdicts.items()}


FORGE = GitHub()
