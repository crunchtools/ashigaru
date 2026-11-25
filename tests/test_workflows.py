"""The workflow files hold the limits and the authority split the profile requires.

Triage, code, fix and release tagging run in the fleet's ci.yml, through sdlc's
`ai` and `write` jobs. The old hourly sweep is gone too: the Desk reads GitHub itself.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
VERSION = (ROOT / "VERSION").read_text().strip()
RETIRED = (
    "triage.yml", "code.yml", "fix.yml", "review.yml", "router.yml", "release.yml", "release-tag.yml",
    "major-tag.yml", "ashigaru.yml", "sweep.yml",
)  # fmt: skip


def test_the_workflows_ci_yml_replaced_stay_gone():
    assert not [name for name in RETIRED if (WORKFLOWS / name).exists()]
    assert not (ROOT / "examples" / "ashigaru.yml").exists()


def test_changelog_has_this_version():
    assert f"## [{VERSION}]" in (ROOT / "CHANGELOG.md").read_text()
