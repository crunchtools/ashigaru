"""The RPM workflow's authority split and the spec's package split."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "rpm.yml").read_text())
SPEC = (ROOT / "packaging" / "ashigaru.spec").read_text()
PINNED = re.compile(r"@[0-9a-f]{40}$")


def test_it_starts_from_no_permissions_and_every_job_has_a_timeout() -> None:
    assert WORKFLOW["permissions"] == {}
    for job in WORKFLOW["jobs"].values():
        assert isinstance(job["timeout-minutes"], int)


def test_only_publish_writes_and_never_for_a_pull_request() -> None:
    jobs = WORKFLOW["jobs"]
    writers = [name for name, job in jobs.items() if "write" in job["permissions"].values()]
    assert writers == ["publish"]
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert "github.event_name != 'pull_request'" in jobs["publish"]["if"]
    assert not any("run" in step and "packaging/" in step["run"] for step in jobs["publish"]["steps"])


def test_actions_are_pinned_by_commit_and_event_values_go_through_env() -> None:
    for job in WORKFLOW["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert PINNED.search(step["uses"].split(" ")[0]), step["uses"]
            assert "${{" not in step.get("run", ""), step


def test_2x_releases_are_skipped() -> None:
    assert (
        "!startsWith(github.event.release.tag_name || inputs.tag, 'v2.')" in WORKFLOW["jobs"]["build"]["if"]
    )


def test_the_cli_package_does_not_carry_the_service() -> None:
    assert "%files desk\n%{_bindir}/ashigaru-desk\n" in SPEC
    assert "ashigaru-desk ] ||" in SPEC
    assert "%pyproject_extras_subpkg -n %{name} desk" in SPEC


def test_the_build_refuses_a_tag_that_does_not_name_version(tmp_path: Path) -> None:
    result = subprocess.run(
        [ROOT / "packaging" / "build.sh", "unused-image", tmp_path / "out", "v0.0.0-not-this"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 1 and "does not match VERSION" in result.stderr
    assert not (tmp_path / "out").exists()


def test_packaging_runs_no_model() -> None:
    """rpm.yml and container.yml are outside the ASHIGARU_ENABLED switch (constitution), so they must
    never run an agent; container.yml calls the constitution's release workflow at an exact version."""
    for name in ("rpm.yml", "container.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        assert "claude-code-action" not in text and "ASHIGARU_APP_KEY" not in text, name
    container = yaml.safe_load((ROOT / ".github" / "workflows" / "container.yml").read_text())
    job = container["jobs"]["container"]
    assert re.fullmatch(r".*/container-release\.yml@v\d+\.\d+\.\d+", job["uses"])
    assert job["with"]["contract-test"] == "packaging/container-test.sh"
    assert container["permissions"] == {}
