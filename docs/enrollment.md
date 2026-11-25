# Enrollment

Setting up an organization, enrolling a repository, and releasing Ashigaru itself.

## Why it matters

A repository is enrolled by carrying the fleet's one `ci.yml`, the same file in every repository. Its `ai` job runs triage, code and fix; its `write` job applies what they decide and tags releases. Both run in the sdlc image, which carries this repository's `scripts/`, `prompts/` and `schemas/` at the commit sdlc pins, so a release of Ashigaru reaches the fleet in the next sdlc image, with no change in any repository.

## How it works

**Organization**

1. Create a GitHub App. Repository permissions: Contents, Issues, Pull requests read and write; Metadata read. No Workflows permission, no organization permissions. Install it on the repositories to enroll.
   - Permissions added after installation are not live until accepted. If the app is created or installed before its repository permissions are set, the installation keeps its old permissions until an organization owner accepts the request on the installation's settings page. Until then `actions/create-github-app-token` fails when a workflow asks for `issues: write` or `pull-requests: write`. Check with `gh api orgs/<org>/installations --jq '.installations[] | select(.app_slug=="<slug>") | .permissions'`; `contents`, `issues` and `pull_requests` must show `write`.
2. Secrets: `ASHIGARU_APP_KEY` (the app's private key) and `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`).
3. Variables: `ASHIGARU_CLIENT_ID` (the app's client ID, on its settings page — `create-github-app-token` mints every token from it and the `ASHIGARU_APP_KEY` secret) and `ASHIGARU_ENABLED` = `true`.
4. A branch ruleset with your required checks, without a bypass for the app. Allow auto-merge on each repository.

On a GitHub Free organization, organization secrets reach public repositories only.

**Repository.** Convert it to the fleet's `ci.yml` with sdlc (`scripts/convert_repo.py` there). The agents run where sdlc's provisioner has copied `CLAUDE_CODE_OAUTH_TOKEN` into the repository's `ai` environment; that copy is the per-repository switch. The state labels are created the first time they are needed.

**Releases** are off until the repository carries a topic. Topics, not inputs, so `ci.yml` stays identical everywhere and the sweep can read the setting without opening each repository's workflow.

| Topic | Effect |
|-------|--------|
| `ashigaru-release-pr` | Ashigaru opens the release pull request; a maintainer merges it; Ashigaru publishes |
| `ashigaru-release-auto` | as above, and GitHub merges it when the required checks pass |
| `ashigaru-release-max-patch`, `-minor`, `-major` | the largest bump `auto` may merge without a person; `minor` when absent |

```bash
gh repo edit crunchtools/mcp-pcloud --add-topic ashigaru-release-auto
```

See [Release](release.md) for what the repository's `CHANGELOG.md` must look like.

**Watch-only mode.** To see Ashigaru's judgement before it acts, add the topic `ashigaru-observe` (admins only). It is the only switch: no input or variable.

```bash
gh repo edit crunchtools/mcp-pcloud --add-topic ashigaru-observe
```

While the topic is there, the triage and code agents still run and every step that would write instead records what it would have done in one issue in that repository, labeled `ashigaru-observe` and titled "Ashigaru is watching". The record is rewritten as things change, and the inbox lists it under `## Watching`. Nothing else is written: no label on any other issue, no comment, no branch, no pull request, no review, no fix round, no release. The sweep queues no triage or fix there, and nothing there counts against `code_per_hour`. Removing the topic ends it: the next sweep closes the issue with a comment, and Ashigaru acts normally with no other change. Ashigaru never triages or fixes its own observe issue.

**Settings.** None per repository: the models, turn limits, tools, triage threshold and round limit are sdlc's, in `sdlc_jobs/ai.py`, and the same everywhere.

**Turning it off.** Set `ASHIGARU_ENABLED` to `false` to stop the sweep. To stop the agents in one repository, remove `CLAUDE_CODE_OAUTH_TOKEN` from its `ai` environment. Release tagging keeps running: a release pull request that has merged is still tagged and published (docs/release.md).

## Releasing

To release `X.Y.Z`: a `chore/release-X.Y.Z` branch moves `VERSION` and the CHANGELOG, dispatching `ci.yml` with `version: X.Y.Z` has its `write` job open `ashigaru/release-X.Y.Z` as the app, and that pull request's merge is tagged and published by `ci.yml`'s `write` job. The fleet picks it up when sdlc pins the new commit and ships its image.

## Related

- [Authority and limits](authority.md)
- [Triage](triage.md), [Code](code.md), [Fix](fix.md), [Sweep](sweep.md), [Release](release.md)
