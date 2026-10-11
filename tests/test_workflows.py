"""The workflow files hold the limits and the authority split the profile requires."""

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
VERSION = (ROOT / "VERSION").read_text().strip()
# The tag callers use. Each release moves it, so no caller is edited to get a release.
MAJOR = f"v{VERSION.split('.')[0]}"
PRODUCT = ("triage.yml", "code.yml", "fix.yml", "sweep.yml", "review.yml", "major-tag.yml")
REUSABLE = PRODUCT[:3]
AGENTS = (*REUSABLE, "review.yml")
"""Workflows that run a model. Each fetches its scripts at the major tag."""
BASE_ACTION = "anthropics/claude-code-action/base-action@"
WRITE = {"write"}


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def jobs(name):
    return load(name)["jobs"].items()


def steps_of(job):
    return job.get("steps", [])


def runs_llm(job):
    return any(str(step.get("uses", "")).startswith(BASE_ACTION) for step in steps_of(job))


@pytest.mark.parametrize("name", PRODUCT)
def test_workflows_start_from_no_permissions(name):
    assert load(name)["permissions"] == {}


@pytest.mark.parametrize("name", PRODUCT)
def test_the_switch_gates_the_first_job_and_every_job_has_a_timeout(name):
    gated = False
    for _, job in jobs(name):
        # A job that calls a workflow cannot set a timeout; the called jobs set their own.
        assert "uses" in job or isinstance(job.get("timeout-minutes"), int)
        if "needs" not in job:
            assert "vars.ASHIGARU_ENABLED == 'true'" in job["if"]
            gated = True
    assert gated


@pytest.mark.parametrize("name", AGENTS)
def test_a_job_that_runs_an_llm_holds_no_write_credential(name):
    seen = False
    for job_name, job in jobs(name):
        if not runs_llm(job):
            continue
        seen = True
        assert not set(job["permissions"].values()) & WRITE, job_name
        for step in steps_of(job):
            assert "create-github-app-token" not in str(step.get("uses", "")), job_name
            assert "ASHIGARU_APP" not in str(step), job_name
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, job_name
    assert seen


@pytest.mark.parametrize("name", AGENTS)
def test_every_agent_step_has_a_turn_limit_and_a_tool_list(name):
    for _, job in jobs(name):
        for step in steps_of(job):
            if str(step.get("uses", "")).startswith(BASE_ACTION):
                args = step["with"]["claude_args"]
                assert "--max-turns" in args and "--allowedTools" in args


@pytest.mark.parametrize("name", ["triage.yml", "review.yml"])
def test_an_agent_that_reads_outside_text_cannot_edit(name):
    args = [
        step["with"]["claude_args"]
        for _, job in jobs(name)
        for step in steps_of(job)
        if str(step.get("uses", "")).startswith(BASE_ACTION)
    ]
    assert args and all('--allowedTools "Read,Glob,Grep"' in value for value in args)


@pytest.mark.parametrize("name", AGENTS)
def test_reusable_workflows_fetch_their_scripts_at_the_major_tag(name):
    """A called workflow cannot name its own ref, so the tag is a literal: the one callers use."""
    fetches = [
        step["with"]["ref"]
        for _, job in jobs(name)
        for step in steps_of(job)
        if step.get("with", {}).get("repository") == "crunchtools/ashigaru"
    ]
    assert fetches and set(fetches) == {MAJOR}


def test_the_caller_uses_the_major_tag_and_matches_the_example():
    example = (ROOT / "examples" / "ashigaru.yml").read_text()
    assert (WORKFLOWS / "ashigaru.yml").read_text() == example
    calls = {name: job["uses"] for name, job in yaml.safe_load(example)["jobs"].items()}
    assert calls == {
        name: f"crunchtools/ashigaru/.github/workflows/{name}.yml@{MAJOR}"
        for name in ("triage", "code", "fix")
    }


def test_the_fix_job_ignores_runs_that_a_reply_started():
    condition = load("fix.yml")["jobs"]["agent"]["if"]
    assert "workflow_run.event == 'pull_request_target'" in condition
    assert "startsWith(github.event.workflow_run.head_branch, 'ashigaru/')" in condition
    assert "head_repository.full_name == github.repository" in condition


def test_the_fix_agent_leaves_release_pull_requests_alone():
    condition = load("fix.yml")["jobs"]["agent"]["if"]
    assert "!startsWith(github.event.workflow_run.head_branch, 'ashigaru/release-')" in condition


def tokens(job):
    return {step["id"]: step for step in steps_of(job) if "create-github-app-token" in str(step.get("uses"))}


def grants(step):
    return {key: value for key, value in step["with"].items() if key.startswith("permission-")}


def test_the_release_job_can_write_only_where_and_when_a_release_is_due():
    job = load("sweep.yml")["jobs"]["release"]
    reader, writer = tokens(job)["reader"], tokens(job)["writer"]
    assert grants(reader) == {"permission-contents": "read", "permission-issues": "write",
                              "permission-pull-requests": "read"}  # fmt: skip
    assert grants(writer) == {"permission-contents": "write", "permission-pull-requests": "write"}
    assert writer["if"] == "steps.plan.outputs.repos != ''"
    assert writer["with"]["repositories"] == "${{ steps.plan.outputs.repos }}"
    holders = [step for step in steps_of(job) if "steps.writer.outputs.token" in str(step)]
    assert len(holders) == 1 and holders[0]["if"] == writer["if"]
    assert holders[0]["run"].startswith("python3 scripts/release.py act ")
    assert job["concurrency"] == {"group": "ashigaru-release", "cancel-in-progress": False}


def test_changelog_has_this_version():
    assert f"## [{VERSION}]" in (ROOT / "CHANGELOG.md").read_text()


def test_the_caller_passes_secrets_to_fix_only_for_this_repository():
    condition = yaml.safe_load((ROOT / "examples" / "ashigaru.yml").read_text())["jobs"]["fix"]["if"]
    assert "head_repository.full_name == github.repository" in condition


def test_the_guard_is_told_which_stage_it_guards():
    for name, stage in (("triage.yml", "triage"), ("code.yml", "code")):
        runs = [step["run"] for _, job in jobs(name) for step in steps_of(job) if step.get("id") == "guard"]
        assert runs and all(run.endswith(f'guard.py" --stage {stage}') for run in runs), name


def test_no_event_a_fork_can_fire_starts_a_review():
    assert set(load("review.yml")[True]) == {"workflow_call"}
    assert set(load("sweep.yml")[True]) == {"schedule", "workflow_dispatch"}


def test_the_review_never_checks_out_the_pull_request():
    for _, job in jobs("review.yml"):
        for step in steps_of(job):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                # Ashigaru itself, or the branch the pull request targets.
                assert step["with"]["ref"] in (MAJOR, "${{ inputs.base }}")
    assert "pull_request_target" not in (WORKFLOWS / "review.yml").read_text().split("on:")[1]


def test_the_review_publisher_can_comment_and_nothing_more():
    mint = [
        step["with"]
        for step in steps_of(load("review.yml")["jobs"]["publish"])
        if "create-github-app-token" in str(step.get("uses", ""))
    ]
    granted = {key: value for key, value in mint[0].items() if key.startswith("permission-")}
    assert granted == {"permission-pull-requests": "write"}


@pytest.mark.parametrize("name", AGENTS)
def test_no_agent_can_read_its_own_environment(name):
    """Grep on /proc/self/environ returns the Claude token unless the settings deny it."""
    seen = False
    for _, job in jobs(name):
        for step in steps_of(job):
            if str(step.get("uses", "")).startswith(BASE_ACTION):
                seen = True
                assert "Read(//proc/**)" in json.loads(step["with"]["settings"])["permissions"]["deny"]
    assert seen
