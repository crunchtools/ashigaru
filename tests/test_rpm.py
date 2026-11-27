"""The RPM workflow's authority split and the spec's package split."""

import re
import subprocess
import tomllib
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


def test_only_publish_writes_and_no_pull_request_runs_the_workflow() -> None:
    """Pull requests build and smoke-test in sdlc ci (the rpm-* checks); rpm.yml only attaches."""
    jobs = WORKFLOW["jobs"]
    writers = [name for name, job in jobs.items() if "write" in job["permissions"].values()]
    assert writers == ["publish"]
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert set(WORKFLOW[True]) == {"release", "workflow_dispatch"}
    assert not any("run" in step and "packaging/" in step["run"] for step in jobs["publish"]["steps"])


def test_every_target_is_an_sdlc_check_on_a_digest_pinned_root() -> None:
    """rpm.yml's pull-request half: each target builds packaging/rpm.Containerfile on its own
    root, pinned by digest (constitution S2), and the build fails if the smoke test does."""
    ci = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["sdlc"]["ci"]
    containerfile = (ROOT / "packaging" / "rpm.Containerfile").read_text()
    for target in ("fc43", "fc45", "el10"):
        check = ci[f"rpm-{target}"]
        assert f"rpm-{target}" in ci["checks"]
        assert check["kind"] == "image-build"
        assert check["containerfile"] == "packaging/rpm.Containerfile"
        assert check["build_args"] == {"TARGET": target}
        assert re.search(rf"^FROM \S+@sha256:[0-9a-f]{{64}} AS root-{target}$", containerfile, re.M)
    assert "FROM root-${TARGET} AS build" in containerfile
    assert containerfile.rstrip().endswith("RUN bash /packaging/smoke-in-container.sh")


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


def test_the_cli_package_ships_the_skill_and_smoke_checks_it() -> None:
    skill = "%{_datadir}/ashigaru/skills/ashigaru/SKILL.md"
    assert f"install -Dpm 0644 skills/ashigaru/SKILL.md %{{buildroot}}{skill}" in SPEC
    assert "%doc README.md\n%{_datadir}/ashigaru/\n" in SPEC
    assert "COPY skills/ /tree/skills/" in (ROOT / "packaging" / "rpm.Containerfile").read_text()
    smoke = (ROOT / "packaging" / "smoke-in-container.sh").read_text()
    assert "/usr/share/ashigaru/skills/ashigaru/SKILL.md" in smoke


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
    """rpm.yml is outside the ASHIGARU_ENABLED switch (constitution), so it must never run an
    agent. The image is the fleet ci.yml's `image` job, gated by the contract in [tool.sdlc.image]."""
    text = (ROOT / ".github" / "workflows" / "rpm.yml").read_text()
    assert "claude-code-action" not in text and "ASHIGARU_APP_KEY" not in text
    image = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["sdlc"]["image"]
    assert image["image"] == "quay.io/crunchtools/ashigaru"
    assert image["boot"] == "healthcheck"
    assert image["contract-test"] == "packaging/container-test.sh"


def test_the_image_is_built_from_the_default_branch_and_not_from_a_tag() -> None:
    """major-tag.yml moves `v5` on every release; a tag push must build nothing. In the fleet
    ci.yml only `check` decides, and a push reaches it only on the default branch."""
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    default_branch = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    assert default_branch in jobs["check"]["if"]
    assert jobs["image"]["needs"] == "check"
    assert "needs.check.result == 'success'" in jobs["image"]["if"]
    assert "needs.image.result == 'success'" in jobs["promote"]["if"]
