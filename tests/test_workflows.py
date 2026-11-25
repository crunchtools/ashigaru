"""The workflow files hold the limits and the authority split the profile requires.

Triage, code, fix and release tagging run in the fleet's ci.yml, through sdlc's
`ai` and `write` jobs; what is left here is sweep.yml.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
VERSION = (ROOT / "VERSION").read_text().strip()
RETIRED = (
    "triage.yml", "code.yml", "fix.yml", "review.yml", "router.yml", "release.yml", "release-tag.yml",
    "major-tag.yml", "ashigaru.yml",
)  # fmt: skip


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def jobs(name):
    return load(name)["jobs"].items()


def steps_of(job):
    return job.get("steps", [])


def tokens(job):
    return {step["id"]: step for step in steps_of(job) if "create-github-app-token" in str(step.get("uses"))}


def grants(step):
    return {key: value for key, value in step["with"].items() if key.startswith("permission-")}


def test_the_workflows_ci_yml_replaced_stay_gone():
    assert not [name for name in RETIRED if (WORKFLOWS / name).exists()]
    assert not (ROOT / "examples" / "ashigaru.yml").exists()


def test_the_sweep_starts_from_no_permissions():
    assert load("sweep.yml")["permissions"] == {}


def test_the_switch_gates_the_sweep_and_every_job_has_a_timeout():
    gated = False
    for _, job in jobs("sweep.yml"):
        # A job that calls a workflow cannot set a timeout; the called jobs set their own.
        assert "uses" in job or isinstance(job.get("timeout-minutes"), int)
        if "needs" not in job:
            assert "vars.ASHIGARU_ENABLED == 'true'" in job["if"]
            gated = True
    assert gated


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


def test_no_event_a_fork_can_fire_starts_a_sweep():
    assert set(load("sweep.yml")[True]) == {"workflow_dispatch"}
