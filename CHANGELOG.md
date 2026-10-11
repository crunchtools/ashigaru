# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.3.0] - 2026-10-10

### Added

- Issues from people without write access are triaged. They get the same labels and summary as any other issue, but a bug-class verdict is labeled `triaged`, not `ready-to-code`: the fix starts only when a maintainer adds that label. The sweep queues outside issues from the backlog as well.
- First-pass review of outside pull requests. Each hour the sweep sends up to five open pull requests from outside contributors to the new `review.yml`, which leaves one comment for the maintainers: what the change does, a recommendation (merge, request changes, close), whether the base branch already has it, and whether it merges and passes its checks. The comment is updated when the contributor pushes. The fork's code is never checked out or run, no event a fork can fire starts the workflow, and nothing is labeled, merged or closed.
- An inbox. The sweep keeps one issue in the Ashigaru repository, labeled `ashigaru-inbox`, listing what waits on a maintainer across the organization: outside pull requests with Ashigaru's recommendation, outside issues with their category, pull requests labeled `needs-human`, and a count of `needs-info` issues per repository. It is edited only when the list changes.
- Type labels for every category with a meaning: `performance` and `question` join `bug`, `documentation` and `enhancement`, and a repository that lacks one gets it created.

### Changed

- `scripts/guard.py` returns a trust level (`maintainer`, `outside`, `none`) and takes `--stage`. Triage runs for `maintainer` and `outside`; the code agent runs for `maintainer` only. On an issue someone without write access wrote, only a maintainer adding `ready-to-code` vouches for it; before, any label from a maintainer did. When the sweep re-adds `ready-to-code` to an outside issue, the guard reads the label history and proceeds only if a person with write access added that label before.
- The caller file's header says where the switch, the author check and the branch check are enforced. Its jobs are unchanged apart from the pins.

### Security

- No agent can read its own environment. With `Read,Glob,Grep` allowed, `Grep` on `/proc/self/environ` returned the process environment, which holds the Claude token. Every agent step now passes settings that deny `/proc` and `/sys` to the file tools, and a test requires it.
- Agent-written text is cleaned before it is posted as a comment: anything shaped like a Claude or GitHub token is blanked, and mentions, HTML and Markdown links are made inert, since an agent that read a stranger's words may repeat them.

## [2.2.0] - 2026-10-10

### Changed

- The sweep runs hourly instead of nightly and has two limits instead of one. It queues up to 20 issues for triage per run, and starts up to five fixes per hour across the organization, less the pull requests Ashigaru opened in the past hour.
- Only a newly opened issue goes from triage straight to the code agent. An issue triaged from the backlog is labeled `ready-to-code` and waits for the sweep to start it, which is what holds coding to the hourly budget.

## [2.1.0] - 2026-10-10

### Added

- Conflict recovery. When two Ashigaru pull requests in one repository touch the same file, the second can no longer merge once the first lands, and nothing used to bring it back. The nightly sweep now finds Ashigaru's open pull requests that conflict with the default branch and sends their issues back to the code agent; the publish job rebuilds a conflicting pull request's branch in place instead of doing nothing. Rebuilds count against the sweep limit and skip pull requests labeled `needs-human`.

### Changed

- The sweep's app token now also asks for `pull-requests: read`.

## [2.0.1] - 2026-10-10

### Security

- The caller's `fix` job now requires the pull request branch to live in the same repository before any secret is passed to `fix.yml`. `fix.yml` already refused forks; the caller now shows it too, so a reviewer reading only the caller can see the boundary.

### Documentation

- Enrollment: say that permissions added after installation must be accepted by an organization owner, how to check, and that the sweep reaches only repositories the app is installed on.

## [2.0.0] - 2026-10-10

Ashigaru is rebuilt from scratch. Version 1 was an MCP server on a host that
worked a ticket when an operator told another agent to dispatch it; it opened
its last pull request in June. Version 2 is a set of reusable GitHub Actions
workflows that watch a repository's issues and fix the bug-class ones without
being asked. Nothing from version 1 is kept: no server, no container image, no
MCP tools.

### Added
- `triage.yml`: classifies a new issue. Bug, documentation and performance
  issues are labeled `ready-to-code`; features are labeled `triaged` and left
  for a maintainer; unclear ones get `needs-info`.
- `code.yml`: fixes a `ready-to-code` issue. The agent edits a checkout with a
  read-only token, the repository's pre-commit hooks run over the result on the
  same runner and failures go back to the same session (at most twice), then a
  second job on a fresh runner pushes `ashigaru/issue-N`, opens the pull
  request and turns on auto-merge.
- `fix.yml`: after each Gatehouse review of an Ashigaru pull request, answers
  every finding in its thread (`fixed in <sha>` or `not a bug: <reason>`) and
  fixes failed checks, for at most five rounds; then labels the pull request
  `needs-human`, turns auto-merge off and stops.
- `sweep.yml`: each night queues the five oldest untriaged issues across
  enrolled repos.
- `examples/ashigaru.yml`: the one file that enrolls a repository.
- Organization variable `ASHIGARU_ENABLED`: every workflow stops at its first
  step unless it is `true`.

### Removed
- The MCP server and all of its tools (`create_run`, `dispatch_worker`,
  `status`, `promote` and the rest), the worker container image, the preview
  deployer, the Matrix and webhook notifications and the Cockpit plugin.
- The `quay.io/crunchtools/mcp-ashigaru` and `mcp-ashigaru-agent-claude`
  images are no longer built.

### Changed
- The repository is renamed from `mcp-ashigaru` to `ashigaru` and moves from
  the MCP Server profile to Workflow Automation (constitution v1.23.0).

## [1.1.0] - 2026-10-10

### Added

- The four tools that only read (`status`, `list_runs`, `run_activity`,
  `run_log`) publish `readOnlyHint: true`. A gateway uses it to decide whether
  an invalid optional argument may be dropped or must refuse the call
  (crunchtools/constitution#35).
- Tests pin every registered tool into `READ_ONLY` or `WRITES`, and check that
  a read-only tool changes no file under the state directory and starts no
  subprocess. `run_build` is the control that trips both checks.

### Fixed

- `run_log` and `GET /api/runs/{run_id}/log` took the log name as a path:
  an absolute path or `../` read any file the server could. The name now has
  to resolve to a file directly inside the run directory.

### Changed

- Inherits constitution v1.22.0; the workflow pins and the pre-commit hook rev
  move with it.

- Constitution is now a v1.18.0 manifest: it holds only what is specific to
  this repo; fleet and profile rules apply by reference.
- Constitution validation is pinned to the inherited release via
  `.github/workflows/constitution.yml`.
- Dependabot auto-merges GitHub Actions minor and patch updates.

## [1.0.0] - 2026-09-20

First tagged release. This image has been running in production since
before it had version control; this release marks the current state as
the baseline going forward.
