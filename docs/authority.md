# Authority and limits

Who holds which credential, what input reaches an agent, and what stops a run.

## Why it matters

A model that reads issue text can be talked into things. The design assumes that will happen and arranges for the component that can be persuaded to hold nothing worth taking.

## How it works

**Credentials**

| Job | Runs a model | GitHub credential | Other secrets |
|-----|--------------|-------------------|---------------|
| Classify, Code agent, Fix agent, Read the pull request | yes | job token, read-only | the Claude token; in Code agent, the reviewer key in the hook step only |
| Apply verdict | no | job token, `issues: write` | none |
| Open pull request, Push and answer findings | no | Ashigaru app token | none |
| Sweep | no | Ashigaru app token, `issues: write`, `pull-requests: read` and `contents: read` | none |
| Cut releases: find and record | no | Ashigaru app token, `issues: write`, `pull-requests: read` and `contents: read` | none |
| Cut releases: open and publish | no | a second Ashigaru app token, `contents: write` and `pull-requests: write`, minted only when a release is due and only for the repositories that have one | none |
| Post the review | no | Ashigaru app token, `pull-requests: write` only | none |

Agent jobs check out with `persist-credentials: false`. What leaves an agent job is structured output validated against a schema and a patch file; the writing job applies the patch on a fresh runner. The app has no Workflows permission, and the publish scripts refuse a patch that touches `.github/`.

**Merging.** No job merges. The publish job turns on auto-merge and GitHub merges when the repository's required checks pass. The app is not an organization admin and cannot bypass a ruleset.

**Releasing.** Off unless a repository carries a release topic, which only its admins can set; the app cannot. The release job runs no model and no repository code: it edits text in a clone and pushes a branch. It tags only the merge commit of a release pull request that Ashigaru wrote and that merged into the default branch, so the required checks and, in `pr` mode, a maintainer stand between the changelog and the tag. See [Release](release.md).

**Untrusted input.** `scripts/guard.py` asks the API for the effective repository permission of the issue's author and of whoever triggered the event, and returns one of three trust levels.

| Trust | When | Triage | Code |
|-------|------|--------|------|
| `maintainer` | the author has write access, or someone with write access added `ready-to-code`, or automation re-added that label after a person with write access had added it | runs | runs |
| `outside` | a person without write access wrote it, and it was just opened or labeled `ready-for-triage` (by anyone) | runs; the verdict never becomes `ready-to-code` | never |
| `none` | closed, a pull request, written by a bot without write access, or labeled `ready-to-code` by someone who may not | never | never |

Two agents read text a stranger wrote: triage (an outside issue) and review (an outside pull request, as a diff). Both have the tools `Read,Glob,Grep` and a read-only token, and return structured output that a separate job posts. Neither can change a file, and the review never checks out or runs the contributor's code. Comments are filtered when the prompt is built: only those from accounts with write access are included. Review findings and check logs on an Ashigaru pull request are treated as data in the prompt.

**The agent's own token.** The Claude token is in the agent process's environment, and a file tool pointed at `/proc/self/environ` would return it. Every agent step passes settings that deny `/proc` and `/sys` to the file tools. Text the agent returns is also scrubbed of anything shaped like a Claude or GitHub token before it is posted.

**Limits**

| Limit | Default | Where |
|-------|---------|-------|
| Switch: every workflow stops at its first job unless it is `true` | n/a | organization variable `ASHIGARU_ENABLED` |
| Turns per agent step | 40 triage, 80 code and fix | input `max_turns` |
| Job timeout | 20, 60 and 75 minutes | workflow files |
| Hook passes fed back to the code agent | 2 | `code.yml` |
| Fix rounds per pull request | 5 | input `max_rounds` |
| Issues queued for triage per sweep | 20, never more | `sweep.yml` input `limit`, `MAX_LIMIT` in `scripts/sweep.py` |
| Fixes started by the sweep per hour, fleet-wide | 5, less the pull requests Ashigaru opened in the past hour | `sweep.yml` input `code_per_hour`, `remaining` in `scripts/sweep.py` |
| Release bump merged without a person | minor, and only with `ashigaru-release-auto` | topics `ashigaru-release-max-patch`, `-minor`, `-major`; `configured` in `scripts/release.py` |
| Quiet time on the default branch before a release | 20 minutes | `SETTLE` in `scripts/sweep.py` |
| Outside pull requests reviewed per sweep | 5 | `MAX_REVIEWS` in `scripts/inbox.py` |
| Runs at a time per issue or pull request branch | 1 | caller `concurrency` |

Each is covered in `tests/test_workflows.py`, `tests/test_findings.py`, `tests/test_sweep.py`, `tests/test_inbox.py` or `tests/test_release.py`.

## Known gaps

- The agent job's runner is the agent's: it can write any file there, and writing files is enough to run code (a git hook, a repository script a pre-commit hook calls). Two secrets are present on that runner. The Claude token is in the agent's process environment and cannot be elsewhere. The reviewer key is on disk only while the Gatehouse hook runs, after the other hooks, and is never in the environment of a hook that runs repository code; that narrows the exposure and does not remove it. Both are bounded by who can reach the code agent: only issues a maintainer wrote or vouched for. The triage and review agents, which read outside text, cannot write a file.
- A pull request from `ashigaru/issue-N` is a same-repository pull request, so the repository's own CI runs the changed code with whatever secrets that CI has. That is true of any contributor with write access; here the contributor is a model.
- With file tools only the agent has no direct network access. A repository that adds `Bash` to `allowed_tools` gives it one.
- Concurrency groups are per repository. Nothing limits how many repositories run an agent at once except the sweep's hourly budget and how often people file issues. Newly opened issues and hand-added `ready-to-code` labels start at once, so a burst of them can exceed the budget for that hour; the sweep then starts nothing until the hour clears.
- Outside text reaches the triage and review agents, and their summaries are posted in public. The deny rule and the scrub close the known way to read the Claude token back; a way not yet known would leak it into a comment. Rotating that token is the remedy, and `ASHIGARU_ENABLED` stops the agents while it is done.
- Anyone can open issues or pull requests, and each costs one triage or review run on the subscription. Reviews are capped at five per sweep; triage of newly opened issues is not capped.
- A waiting issue whose code run dies without handing off keeps its `ready-to-code` label and is started again by a later sweep. Nothing counts those attempts yet.
- A release pull request counts as a pull request Ashigaru opened, so each one takes a slot from that hour's coding budget.
- The bump is read from the changelog. A breaking change entered under `### Fixed` without the word `BREAKING` is released as a patch, and with `ashigaru-release-auto` nobody is asked. Reviewing the changelog entry is part of reviewing the change.
- A release in `auto` mode reaches whatever deploys from the tag or the default branch with no person in between. That is the point of the mode; choose it per repository.

## Related

- [Enrollment](enrollment.md)
- [Fix](fix.md)
- [Release](release.md)
