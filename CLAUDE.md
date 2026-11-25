# Dev Commands

Everything runs from containers; the host needs only git, podman, pre-commit and gh.

- **Lint and test:** `podman run --rm -v .:/src:Z -w /src docker.io/library/python:3.12 bash -c 'pip -q install pytest==9.1.1 pyyaml==6.0.3 ruff==0.16.9 && ruff check . && ruff format --check . && pytest -q'`
- **Workflows:** `podman run --rm -v .:/repo:Z -w /repo docker.io/rhysd/actionlint:1.7.12`

# Layout

- `.github/workflows/ci.yml`: the fleet's one file. Its `ai` and `write` jobs run triage, code, fix and release tagging in every enrolled repo, this one included, from the scripts the sdlc image carries.
- `.github/workflows/sweep.yml`: dispatched here only. `rpm.yml` builds the RPM.
- `scripts/`: every decision and every write. Standard library and `gh` only. Pure functions are tested in `tests/`; `main()` does the I/O.
- `prompts/`, `schemas/`: what each agent is told and what it must return.
- `src/ashigaru/`: 3.x, the Desk and its clients; `docs/desk-api.md` is the contract.
- `Containerfile`, `[tool.sdlc.image]`: the Desk image; ci.yml's `image` job builds it, never a seat.
- `packaging/`: the RPM spec and the scripts `rpm.yml` runs; `packaging/build.sh registry.fedoraproject.org/fedora:43 out` builds locally with podman.

# Rules that are easy to break

- The job that runs a model holds a read-only token and never the app key; the job that writes runs no model. sdlc's `ai` and `write` enforce it.
- Logic goes in `scripts/`, not in workflow shell.
- Event values reach scripts through `env:`, never interpolated into `run:`.
- Releasing: see `docs/enrollment.md`. `VERSION` and the CHANGELOG move together. The fleet runs the commit sdlc pins, not a tag of ours.
- Label names, the `ashigaru/` branch prefix and the scripts' arguments sdlc calls are public interface: a change is a MAJOR release.
