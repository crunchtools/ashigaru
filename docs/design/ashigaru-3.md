# Ashigaru 3.x design: the Desk, Josui dispatch, Exedore runners

Status: accepted design, 2026-11-17; outcome #178
Owner: Josui (CTO). Product owner: Scott.

## Problem

Work arrives from many places, and three systems act on it independently.
Ashigaru acts autonomously inside GitHub Actions. Josui dispatches seats from a
JSONL ledger. Scott drives seats by hand. They overlap, nobody sees the whole
queue, and one finding spawns follow-ups without limit. On 2026-11-17 this
produced more than 20 open items and a day of churn. It also used up the Team
plan's included private-repo Actions allowance 14 days early. GitHub-hosted
runners are slow, and Gloval's runners share a box with production.

## Goals

1. One queue for every source of change, with one owner per item.
2. One dispatcher (Josui) and one worker pool (the seats). No autonomous agents
   running in CI.
3. CI runs on our hardware (Exedore) wherever that is safe. GitHub-hosted
   runners remain only where untrusted code runs.
4. Production (Gloval) runs no CI.

Non-goals: rewriting Gatehouse or the constitution, or migrating RT into GitHub.

## Outcomes and stories (Scott, 2026-11-17)

- **RT tickets and GitHub issues are outcomes:** the business problems humans
  care about. Agents never open them. Every outcome row in the Desk carries an
  RT# or GH# ref, except one standing "keep the fleet healthy" outcome per repo
  group, which holds robot-only work (dependency bumps, pin churn, CI fixes).
- **Desk items are stories:** the units of work workers pick up. Every story
  links to the outcome it serves. Josui breaks an outcome into stories. When
  its stories are done, Josui verifies the result and resolves the RT ticket
  or closes the GH issue with a one-line verification. Opening is the control;
  closing is bookkeeping.
- **Filing.** When an agent wants to file something, it calls `desk_file`. The
  Desk rejects a second call in the same assignment, records the child in
  state `proposed`, linked to the parent story's outcome, and auto-abandons it
  after 14 days unless Josui promotes it to `queued`. A child never becomes an
  outcome: if Josui judges it a business problem, Josui marks it `needs-outcome`
  and Scott opens the RT ticket or GitHub issue, or it dies. Nothing an agent
  files appears in RT or GitHub.

## Sources of change

| Source | Arrives by | Becomes |
|---|---|---|
| Our PRs (seats, Scott) | GitHub webhook | story; review seat, then Josui merges |
| External PRs (forks, outsiders) | GitHub webhook | story; Gatehouse, review seat, Scott for anything non-trivial |
| GitHub issues | GitHub webhook | outcome; Josui triages into stories |
| Dependabot PRs | GitHub webhook | story under fleet health; auto-merged by the constitution workflow on green for patch/minor |
| Dependabot / code-scanning alerts | GitHub webhook | story under fleet health |
| RT tickets | RT poll (phase 4b) | outcome |
| Gatehouse findings | check run + review comments | attached to the PR's story; never a new item |
| CI failure on a default branch | workflow_run webhook | story under fleet health |
| Nagios alerts | notification command (phase 4b) | story (ops lane) |
| Scott's requests | Josui | outcome or story |

Dedup: every item has a `subject_key`, one of (repo, PR#), (repo, issue#),
RT#, or (repo, alert id), under a unique index. Webhooks upsert on it, so a
PR, the issue it closes and its findings land on one story.

## Components

### 1. The Desk (Ashigaru 3.x), on Gloval

**Product requirements (Scott, 2026-11-17).**

- **Python.** The service, the CLI and the MCP server are one Python
  project.
- **A service with a REST API** at its own hostname (working name
  ashigaru.crunchtools.com). Everything else is a client of this API.
- **A local `ashigaru` command** that talks to the service over that REST
  API. It's what Claude Code hooks call (session start, prompt submit,
  stop) to report seat state and turn activity, replacing today's
  `seat-state.json` hooks. Seats, Josui and scripts all use the same
  command.
- **The CLI, the `/ashigaru` skill and the MCP tools share one command set** (see the `/ashigaru` section below), so moving between them means nothing new to learn.
- **API keys for the client (Scott, 2026-11-17).** Each identity (a seat,
  Josui, Scott, a CI job) has its own API key, minted and revoked through
  the service. The CLI reads the service URL and key from
  `~/.config/ashigaru/config.toml` (mode 0600) or from `ASHIGARU_URL` and
  `ASHIGARU_API_KEY` in the environment, which win. A key is never passed
  as a command-line argument. Images ship the CLI without a key; the key is
  injected at run time (a podman secret or a mounted file).
- **Shipped as an RPM built on GitHub**, so it bakes into the developer
  seat image, bootc images and any other container, and installs on a
  plain host with `dnf`.
- **A built-in MCP server**, served by the service at a generated URL that
  ends in a secret code, `https://<host>/mcp/<code>`, the way Acquacotta and
  Postiz serve hosted MCP. Generating, listing and revoking those URLs is a
  CLI and API action. Each code is bound to one identity (a seat, Josui or
  Scott), which is how the service knows who is calling.
- **The Dolza ledger's model, carried over whole.** Every field and state
  the `/agents` workflow uses today:
  - item: id, task (one-line objective), done_when, ref (issue or RT URL),
    lane (vertex / dolza-max / exedore-max), seat, mode (auto / manual),
    after (ids it waits on), check_after, result, next, learned, memorized,
    verified_by, timestamps;
  - states: proposed, queued, assigned, working, review, done, blocked,
    wontdo, abandoned, plus wrap-up (cleared);
  - seat state: free / busy, owner (manual), turn active / idle, the first
    prompt as its task, since;
  - the report contract: DONE / BLOCKED / WORKING, Filed:, Learned:,
    CC-DONE;
  - the `/agents check` table (id, seat, mode, task, state, result or
    blocker, next) is a first-class CLI and MCP view, not something Josui
    rebuilds from a JSONL file.

- **CI callbacks (Scott, 2026-11-17).** GitHub's `workflow_run` and
  `check_suite` "completed" events reach the Desk through its org webhook.
  `desk_wait(repo, sha)` returns the verdict and the failing checks as soon
  as that event lands, so a seat calls it once after a push (in the
  background if it has other work) and never sleeps or polls GitHub. The
  same events update the story's state.

- **Intake.** One org-level GitHub webhook in the MVP. The Desk verifies
  `X-Hub-Signature-256` and dedupes on `X-GitHub-Delivery`. RT and Nagios
  intake follow in phase 4b. Gloval has a public IP behind Cloudflare.
- **Queue.** A SQLite store. Each item carries id, kind (outcome or story),
  parent outcome, `subject_key`, source refs, repo, lane (vertex / dolza-max /
  exedore-max / rule), state (proposed, queued, assigned, working, review,
  done, blocked, wontdo, abandoned), a blocked reason, owner seat,
  `verified_by`, priority and timestamps. It replaces
  Josui's JSONL agents ledger, whose history is imported once.
- **Interface.** MCP tools through Trentina: `desk_list`, `desk_get`,
  `desk_claim`, `desk_update`, `desk_file`. The caller's identity comes from
  its Trentina profile, never from a tool argument. Seats may update or file
  only on their own items. Josui gets everything.
- **No LLM and no Actions minutes.** It records and routes, and never edits code.
- **Policy file.** One YAML file: lane defaults by repo and the child cap.
  Dependabot auto-merge stays in the constitution's reusable workflow; the
  Desk records the PR and its outcome.

### The `/ashigaru` skill and the `ashigaru` CLI (Scott, 2026-11-17)

The `/agents` skill becomes `/ashigaru`, a thin skill over the `ashigaru` CLI.
The CLI, the skill and the MCP tools share one command set with the same names
and arguments, so moving between a terminal, a Claude session and an MCP
client means nothing new to learn.

Views:

| Command | Shows |
|---|---|
| `ashigaru usage` | One row per seat: context used, tokens this session, time in the current turn, model. Spend by billing source (Max, Vertex, OpenRouter), today and this month, against the limits. |
| `ashigaru agents` | One row per connected seat (Dolza 1 - Max, Dolza 2 - Vertex, Exedore 3 - Max, ...): free or busy, the item it holds, how long it has been at it. |
| `ashigaru work` | One row per item (a204, a203, ...): seat, auto or manual, task, state, result, next. Newest first, with items blocked on Scott at the top. |
| `ashigaru queue` | Queued items and what each waits on, plus agent-filed children in `proposed` and their expiry. |
| `ashigaru outcomes` | Each RT ticket or GitHub issue with its stories and their done, blocked and open counts. |

Actions:

| Command | Does |
|---|---|
| `ashigaru assign` | Picks a seat by lane, sends the brief and records the item. |
| `ashigaru auto` / `ashigaru manual` | Hands an item to Josui or to Scott. |
| `ashigaru wait` | Waits on a commit's CI through the webhook callback, without polling. |
| `ashigaru wrap` | Saves the seat's learnings, confirms it is idle, clears it, and closes the item. |

Seats report usage and turn state by calling the same CLI from their Claude
Code hooks and status line.

### 2. Josui Prime, the dispatcher

Josui reads the Desk, assigns free seats, routes every code story to a second
seat for review, and does all merges (except Dependabot auto-merge),
releases and deploys. WIP limit: at most
5 stories in flight.

### 3. Workers

The workers are the existing seats. Red Hat work goes only to Dolza Vertex.
Host operations go to Dolza Max. Code, builds and research go to Exedore Max.

### 4. Runners on Exedore

- **Isolation: one runner VM.** All runners live in a single libvirt VM on
  Exedore, never in containers beside the seats. bootc and disk builds run
  privileged inside the VM, where privileged means VM root, not host root.
  - Egress is enforced on the host, never inside the VM. The VM's only route
    out is a forward proxy on the host (the runner honours `https_proxy`) with
    a domain allowlist, since nftables cannot match wildcard hostnames.
    Allowed: GitHub (`github.com`, `api.github.com`,
    `codeload.github.com`, `objects.githubusercontent.com`,
    `*.actions.githubusercontent.com`,
    `*.blob.core.windows.net`, `ghcr.io`, `pkg-containers.githubusercontent.com`),
    the container registries we pull from and push to, the Fedora/RHEL
    mirrors, PyPI and npm. No route to the LAN (seats, Dolza, the router).
  - The VM reverts to a golden snapshot nightly. The monthly runner-image
    rebuild refreshes the snapshot.
  - The VM and its units start on boot and restart after a Wi-Fi drop.
    The host reads the VM's runner logs over its serial console and forwards
    them to Gloval's syslog; the VM itself cannot reach Gloval.
- **Registration.** A GitHub App installed on the org with only "Self-hosted
  runners: write". Its key lives on the Exedore host, never in the VM: a
  privileged build is VM root and could read anything in the VM. A host-side
  registrar mints a single-use JIT config per runner start (`generate-jitconfig`,
  with the runner group and labels) and passes it in. Nothing reusable enters
  the VM. Start with 6 concurrent runners. Exedore has no public IP, and none
  is needed: runners poll GitHub over outbound HTTPS.
- **Runner version.** Runners use `--disableupdate`, so the image is rebuilt
  monthly. GitHub stops assigning jobs to runners older than about 63 days.
- **Container jobs.** Jobs that use `container:` or Docker actions need a
  `docker` CLI and API. The VM provides a podman socket plus the
  `podman-docker` shim. Phase 2 audits which workflows use `container:` or
  `docker/*` actions and tests each.
- **Credentials on runners.** No production credentials ever reach a runner.
  Deploys stay with Josui through Trentina, as today. Image pushes use the
  job's own short-lived `GITHUB_TOKEN` or a registry robot account scoped to
  the one repository.
- **Org settings.**
  - One runner group, `exedore`, with selected repositories, public allowed.
    GitHub Team has no "Workflow access" control, and group policy cannot
    tell a PR job from a push job, so a second group would buy nothing.
  - "Require approval for all external contributors" on public repos.
    `GITHUB_TOKEN` read-only by default.
  - Fork-PR workflow settings on private repos stay at their defaults:
    disabling them also stops Dependabot PR checks, and nobody outside the
    org forks our private repos.
- **Routing rule (Scott, 2026-11-17).** Every job runs on Exedore EXCEPT jobs
  triggered by a pull request or PR-comment event in a **public** repository,
  which stay on GitHub-hosted `ubuntu-24.04`. Push to the default branch, tags,
  releases, schedules and dispatches run on Exedore, public or private, because
  they execute only merged code. Private-repo PRs run on Exedore. Hardcoded in
  the shared reusable workflows (the `github` context there is the caller's):
  ```
  runs-on: ${{ (startsWith(github.event_name, 'pull_request')
    || github.event_name == 'issue_comment'
    || (github.event_name == 'workflow_run'
        && (startsWith(github.event.workflow_run.event, 'pull_request')
            || github.event.workflow_run.event == 'issue_comment')))
    && github.event.repository.private == false
    && 'ubuntu-24.04' || vars.CI_RUNNER }}
  ```
  `CI_RUNNER` is an org Actions variable whose default is `exedore`. Falling
  back to GitHub-hosted runners is one variable flip.
- **What routing does not defend.** A fork PR can rewrite its own caller
  workflow to say `runs-on: exedore`. GitHub says plainly that approval is not
  a security control for self-hosted runners, and approval does not gate
  `pull_request_target` at all. The controls are therefore:
  - Gatehouse marks any outside PR that touches `.github/workflows/` as "needs
    Scott" before anyone approves its run.
  - No public repo uses `pull_request_target` with a checkout of the PR head.
  - If a job gets through anyway, the VM contains it: host-enforced egress, no
    LAN, no credentials worth stealing, nightly revert.
- **The `v2` tag** of the shared workflows is protected by a repo-level tag
  ruleset (available on Team). The Ashigaru App, which moves it on release,
  is the ruleset's only bypass actor.
- **Dependabot on Exedore.** Dependabot PR jobs in private repos, and every
  push job after an auto-merge, run `npm install` or `pip install` on Exedore
  with no human in the loop.
  This is an accepted risk, bounded by the VM.
- **Gloval's runners retire** once Exedore's pool is green for 48 hours.
- **Capacity.** The VM gets a fixed vCPU and memory allotment that leaves the
  four seats their share.
- **Availability.** Exedore is one box on Wi-Fi. GitHub cancels jobs queued
  for 24 hours, so a hang longer than that fails CI. Nagios polls GitHub's API
  (it cannot reach Exedore). It alerts when the online runner count is 0, and
  14 days before the runner version's `runtime_deprecates_at`. The response to
  an alert is flipping `CI_RUNNER`.

### 5. One `sdlc` image for every repo (Scott, 2026-11-17)

python-sdlc proved the model: one image carries the tools, the repo carries
only its rules, and laptop and CI run the same verdicts. It generalises to the
whole fleet.

- **One image, `quay.io/crunchtools/sdlc`, with stacks.** The launcher detects
  what a repo is (`pyproject.toml` → python, `package.json` → node,
  `Containerfile` → container, bootc → image build) and runs that stack's
  checks. python-sdlc becomes the python stack; nothing about it changes for
  the 35 Python repos except the image name.
- **Fleet inventory (a195, 2026-11-17).** 73 non-archived repos. Each
  carries 8 workflow files on average (up to 15) and its own constitution
  (17 to 511 lines). Stacks needed: container 61, python 40 (39 already
  on python-sdlc, two of them behind at v0.6.3), node 2, bootc 3. No Rust
  or Go. Six docs or static-site repos match no build stack; a `docs` stack
  (Gourmand, secrets scan, links) covers them. stackrox has the org's
  required checks but no workflows, so any PR there could never merge. The
  private-repo list is incomplete; phase 2 enumerates it with an org token.
- **Four verbs, the same everywhere:** `sdlc commit`, `sdlc push`, `sdlc ci`,
  `sdlc release`. Build order lives in the image: lint, types, tests, scans,
  build, test the built image, then publish only on `release`. Exit codes stay
  0 / 1 / 2 (passed / failed / could not run).
- **Each repo keeps two workflow files, both stubs that never change:** `ci.yml`
  runs `sdlc ci` and, on a tag, `sdlc release`; `gatehouse.yml` calls the
  Gatehouse review, which has to stay a separate `pull_request_target`
  workflow for fork safety. One required check, `sdlc ci`, replaces Gourmand,
  Constitution and the per-repo container workflows. Triage stays required.
- **Deterministic path vs advisory path.** Everything in `sdlc ci` is
  deterministic and blocks. The LLM review is the one non-deterministic part:
  it stays advisory, and only its answered-or-not triage blocks.
- **The constitution shrinks to rules.** It keeps what must be true (license,
  SemVer, secrets, required gates, the host contract) and drops every job
  name, image tag, example file and pin it now spells out. Those live in the
  image, which floats at `latest` and reaches every repo with no PR. The
  validator ships inside the image, so a constitution amendment is an image
  release, not a fleet change. Only a launcher change needs a `rev:` bump.
- **Per-repo constitutions go away.** Today the central constitution and its
  seven profiles run to about 3,500 lines, and each repo adds its own (84 to
  279 lines in a sample of five), written and drifting separately. Profiles
  become sdlc stacks. A repo's machine-checked settings move into its sdlc
  config block, which the image validates; anything a human or agent needs to
  know about the repo goes in a short "What's different here" README section,
  capped at 30 lines. The central constitution targets under 300 lines of
  rules, with no mechanism in it.
- **Fleet changes are scripts, not agents.** After the one-time conversion,
  the only change that touches every repo is a launcher `rev:` bump, which
  should be rare. When one is needed, `scripts/fleet-bump.py` writes the
  same mechanical PR to every repo, and each one merges on its own green
  `sdlc ci` check. No seat works through repos one at a time, and no LLM
  tokens are spent. The one-time conversion runs the same way: a script per
  stack, with a seat handling only the repos the script cannot convert.
- **Fits the runners.** An Exedore runner job becomes `podman run sdlc ci`,
  which also removes most `container:` and Docker-action jobs from the audit.

## Phases (each finished and verified before the next starts; the sdlc image is
built in its own repo during phase 1, since that touches no fleet repo)

1. **Quiet Ashigaru.** Set the org variable `ASHIGARU_ENABLED` to `false`.
   Every Ashigaru job, on every pin, is gated on it and skips; skipped jobs
   satisfy required checks, so nothing blocks a merge. No release is needed.
   Release tagging on merge moves first to one small shared workflow that
   mints the Ashigaru App token (`actions/create-github-app-token`), because
   tags pushed with `GITHUB_TOKEN` start no workflows and image builds would
   silently stop. Josui opens release PRs; the hourly sweep is retired. Verify
   that no Ashigaru agent job runs anywhere for 24 hours and that one release
   tag still triggers its image build. This stops the follow-up churn and the
   minutes burn now, and depends on nothing else.
2. **sdlc image.** Rename python-sdlc to sdlc and add the container and node
   stacks, then bootc. Move the constitution validator in. Convert repos stack
   by stack to the two stub workflows, one fleet PR per repo, once; the same PR
   deletes the repo's constitution and adds its config block and README
   section. Cut the central constitution down to rules. Done when every repo's required checks are
   `sdlc ci` plus triage, and a constitution change ships with zero repo PRs.
3. **Runners.** Build the VM, the host egress rules, the registrar and the App;
   set the runner group and org settings; audit `container:` and Docker usage;
   wire the routing expression into the shared workflows; move the
   crunchtools/workstation build workflows (now disabled) to Exedore; retire
   Gloval's runners. Done when private CI and the bootc builds run on Exedore,
   private-repo Actions spend goes flat, and both Nagios checks are live.
4. **Desk MVP.** GitHub webhook intake, queue, tools, ledger import. Done when
   Josui dispatches only from the Desk for a full day. 4b adds RT and Nagios
   intake.
5. **CI streamlining.** What the sdlc move leaves: retriage only on a reply
   that answers a finding, and a rule for Dependabot PRs whose Gatehouse
   findings would otherwise keep triage red forever; Dependabot grouped weekly;
   dead Ashigaru callers and the Kagetora sweep dispatch removed.

## Risks

- A self-hosted runner executing untrusted code. Mitigations: the PR-event
  routing rule, the workflow-change flag, ephemeral JIT runners, and the
  host-enforced VM boundary.
- Exedore as a single point of failure for CI. Mitigations: the `CI_RUNNER`
  flip inside GitHub's 24h window, and the Nagios API checks.
- Operating load on a one-person org: the monthly runner-image and snapshot
  rebuild, the proxy allowlist, and the App key (one rotation touches all CI).
  The rebuild is a scheduled job with the Nagios deprecation check as its
  backstop.
- Exedore contention between seats and runners. Mitigation: the VM's fixed
  allotment.
- The GitHub App key on the Exedore host. It can register runners only; it
  cannot read code or push. The host is root-only and runs no jobs.
- The Desk as a new service on Gloval. It is small, holds no secrets beyond
  the webhook secret (and an RT token in 3b), and follows the constitution
  (web-application profile).

## To verify in phase 3 (not assumed)

The default of "Run workflows from fork pull requests" on a private repo
(Dependabot PR checks depend on it); the `podman-docker` shim with a real
`container:` job; which repos name Gatehouse or Ashigaru jobs as required
checks.

## Open product question for Scott

RT stays the human/ops intake, side by side with GitHub issues (recommended),
versus migrating RT into GitHub issues.
