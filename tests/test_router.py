"""Every event the caller subscribes to reaches exactly the job it should, or the no-op.

The caller names one workflow, router.yml, so routing lives in its `if:` lines.
These tests evaluate those lines the way GitHub does, against event payloads.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "crunchtools/example"
CALLER = yaml.safe_load((ROOT / "examples" / "ashigaru.yml").read_text())
ROUTER = yaml.safe_load((WORKFLOWS / "router.yml").read_text())
JOBS = ROUTER["jobs"]
LEVEL = {"none": 0, "read": 1, "write": 2}

PLACEHOLDER = re.compile(r"\$\{\{ (.*?) \}\}")
TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(==|!=|&&|\|\||[!(),])|([A-Za-z_][\w.\-]*))")


class Expression:
    """Enough of GitHub's expression language for the router's `if:` lines."""

    FUNCTIONS = {
        "always": lambda: True,
        "startsWith": lambda text, prefix: str(text or "").lower().startswith(str(prefix).lower()),
        "contains": lambda text, part: str(part).lower() in str(text or "").lower(),
    }
    LITERALS = {"true": True, "false": False, "null": None}

    def __init__(self, source: str, context: dict) -> None:
        self.tokens = [match.group(1) or match.group(2) or match.group(3) for match in TOKEN.finditer(source)]
        self.context = context
        self.at = 0

    def value(self) -> object:
        result = self.either()
        assert self.at == len(self.tokens), self.tokens[self.at :]
        return result

    def peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.tokens[self.at]
        assert expected is None or token == expected, (expected, token)
        self.at += 1
        return token

    def either(self) -> object:
        left = self.both()
        while self.peek() == "||":
            self.take()
            right = self.both()
            left = left or right
        return left

    def both(self) -> object:
        left = self.compare()
        while self.peek() == "&&":
            self.take()
            right = self.compare()
            left = left and right
        return left

    def compare(self) -> object:
        left = self.primary()
        if self.peek() in ("==", "!="):
            equal = self.take() == "=="
            return same(left, self.primary()) == equal
        return left

    def primary(self) -> object:
        token = self.take()
        if token == "!":
            return not self.primary()
        if token == "(":
            inner = self.either()
            self.take(")")
            return inner
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        if token in self.LITERALS:
            return self.LITERALS[token]
        if self.peek() == "(":
            return self.FUNCTIONS[token](*self.arguments())
        return lookup(self.context, token)

    def arguments(self) -> list[object]:
        self.take("(")
        values: list[object] = []
        while self.peek() != ")":
            values.append(self.either())
            if self.peek() == ",":
                self.take()
        self.take(")")
        return values


def same(left: object, right: object) -> bool:
    """GitHub compares strings without regard to case."""
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    return left == right


def lookup(context: dict, path: str) -> object:
    value: object = context
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def runs(job: str, context: dict) -> bool:
    return bool(Expression(JOBS[job]["if"], context).value())


def route(event_name: str, event: dict, auto_code: str = "", enabled: str = "true") -> set[str]:
    """The router jobs that start for one event; code waits on triage's verdict."""
    context = {
        "github": {"event_name": event_name, "event": event, "repository": REPO},
        "vars": {"ASHIGARU_ENABLED": enabled},
        "needs": {"triage": {"outputs": {"auto_code": ""}}},
    }
    started = {job for job in JOBS if job != "code" and runs(job, context)}
    if "triage" in started:
        context["needs"]["triage"]["outputs"]["auto_code"] = auto_code
    if runs("code", context):
        started.add("code")
    return started


def issue(action: str, label: str | None = None) -> dict:
    event: dict = {"action": action, "issue": {"number": 7}}
    if label:
        event["label"] = {"name": label}
    return event


def gatehouse(branch: str, event: str = "pull_request_target", repo: str = REPO) -> dict:
    return {
        "action": "completed",
        "workflow_run": {"event": event, "head_branch": branch, "head_repository": {"full_name": repo}},
    }


def pull(action: str, branch: str, merged: bool = False, repo: str = REPO) -> dict:
    return {
        "action": action,
        "pull_request": {"merged": merged, "head": {"ref": branch, "repo": {"full_name": repo}}},
    }


CASES = [
    ("issue opened, triage says code", "issues", issue("opened"), "true", {"triage", "code"}),
    ("issue opened, triage says wait", "issues", issue("opened"), "false", {"triage"}),
    ("issue labeled ready-for-triage", "issues", issue("labeled", "ready-for-triage"), "", {"triage"}),
    ("issue labeled ready-to-code", "issues", issue("labeled", "ready-to-code"), "", {"code"}),
    ("issue labeled anything else", "issues", issue("labeled", "bug"), "", {"ignore"}),
    ("issue reopened", "issues", issue("reopened"), "", {"ignore"}),
    ("comment", "issue_comment", {"action": "created", "issue": {"number": 7}}, "", {"ignore"}),
    ("Gatehouse on an Ashigaru branch", "workflow_run", gatehouse("ashigaru/issue-7"), "", {"fix"}),
    ("Gatehouse on another branch", "workflow_run", gatehouse("feature/x"), "", {"ignore"}),
    ("Gatehouse started by a reply", "workflow_run", gatehouse("ashigaru/issue-7", "issue_comment"), "",
     {"ignore"}),
    ("Gatehouse on a fork's ashigaru/ branch", "workflow_run",
     gatehouse("ashigaru/issue-7", repo="evil/fork"), "", {"ignore"}),
    ("release pull request merged", "pull_request_target", pull("closed", "ashigaru/release-1.2.0", True), "",
     {"release"}),
    ("release pull request closed unmerged", "pull_request_target", pull("closed", "ashigaru/release-1.2.0"),
     "", {"ignore"}),
    ("fork's release-named branch merged", "pull_request_target",
     pull("closed", "ashigaru/release-1.2.0", True, "evil/fork"), "", {"ignore"}),
    ("fix pull request merged", "pull_request_target", pull("closed", "ashigaru/issue-7", True), "",
     {"ignore"}),
    ("pull request opened", "pull_request_target", pull("opened", "ashigaru/release-1.2.0"), "", {"ignore"}),
    ("pull request pushed", "pull_request_target", pull("synchronize", "ashigaru/issue-7"), "", {"ignore"}),
    ("run by hand", "workflow_dispatch", {}, "", {"ignore"}),
]  # fmt: skip


@pytest.mark.parametrize(("event_name", "event", "auto_code", "expected"),
                         [case[1:] for case in CASES], ids=[case[0] for case in CASES])  # fmt: skip
def test_each_event_reaches_its_job(event_name, event, auto_code, expected):
    assert route(event_name, event, auto_code) == expected


@pytest.mark.parametrize(("event_name", "event", "auto_code"),
                         [case[1:4] for case in CASES], ids=[case[0] for case in CASES])  # fmt: skip
def test_the_switch_stops_every_route(event_name, event, auto_code):
    assert route(event_name, event, auto_code, enabled="false") == set()


@pytest.mark.parametrize(("event_name", "event", "auto_code"),
                         [case[1:4] for case in CASES], ids=[case[0] for case in CASES])  # fmt: skip
def test_the_no_op_runs_exactly_when_nothing_else_does(event_name, event, auto_code):
    started = route(event_name, event, auto_code)
    assert ("ignore" in started) == (started == {"ignore"})


def test_every_event_the_caller_subscribes_to_has_a_case():
    subscribed = {
        (event, action)
        for event, config in CALLER[True].items()
        for action in ((config or {}).get("types") or [None])
    }
    covered = {(case[1], case[2].get("action")) for case in CASES}
    assert subscribed <= covered, subscribed - covered


def test_the_no_op_reads_nothing_and_runs_one_step():
    job = JOBS["ignore"]
    assert job["permissions"] == {}
    assert job["timeout-minutes"] == 1
    assert "secrets." not in yaml.safe_dump(job)
    assert [set(step) & {"uses", "run"} for step in job["steps"]] == [{"run"}]


def test_the_router_checks_out_nothing():
    """Under pull_request_target a checkout of the pull request would run a stranger's code."""
    assert all("steps" not in job for name, job in JOBS.items() if name != "ignore")
    assert "checkout" not in (WORKFLOWS / "router.yml").read_text()


def test_each_route_gets_only_the_secrets_its_workflow_declares():
    for name, job in JOBS.items():
        if "uses" not in job:
            continue
        called = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())[True]["workflow_call"]
        assert isinstance(job["secrets"], dict), name
        assert set(job["secrets"]) <= set(called.get("secrets", {})), name


def test_the_caller_is_thin_and_grants_what_every_route_needs():
    """A called workflow can only narrow the caller's token, never widen it."""
    (caller_job,) = CALLER["jobs"].values()
    assert set(caller_job) == {"name", "permissions", "uses", "secrets"}
    assert caller_job["secrets"] == "inherit"
    for name, job in JOBS.items():
        for scope, level in job["permissions"].items():
            assert LEVEL[caller_job["permissions"].get(scope, "none")] >= LEVEL[level], (name, scope)


def group(event_name: str, event: dict, run_id: str = "1") -> str:
    context = {"github": {"event_name": event_name, "event": event, "run_id": run_id}}
    return PLACEHOLDER.sub(lambda match: str(Expression(match.group(1), context).value()),
                           CALLER["concurrency"]["group"])  # fmt: skip


def test_events_of_one_kind_for_one_issue_or_branch_wait_for_each_other():
    labeled = issue("labeled", "ready-to-code")
    assert group("issues", issue("opened")) == group("issues", labeled) == "ashigaru-issues-7"
    assert group("workflow_run", gatehouse("ashigaru/issue-7")) == "ashigaru-workflow_run-ashigaru/issue-7"
    closed = pull("closed", "ashigaru/release-1.2.0", True)
    assert group("pull_request_target", closed) == "ashigaru-pull_request_target-ashigaru/release-1.2.0"
    assert CALLER["concurrency"]["cancel-in-progress"] is False


def test_ignored_events_do_not_share_a_concurrency_group_with_routed_ones():
    """A waiting run is replaced by the next one in its group; a comment must not replace a code run."""
    comment = {"action": "created", "issue": {"number": 7}}
    assert group("issue_comment", comment) != group("issues", issue("labeled", "ready-to-code"))
    assert group("workflow_dispatch", {}, "41") != group("workflow_dispatch", {}, "42")
