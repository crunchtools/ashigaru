# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- CI is the fleet's one `ci.yml` (sdlc 0.15). The Python 3.15 pytest run that `ci.yml`'s `py315` job did is now declared in `[tool.sdlc.ci]`: an `image-build` of `ci/py315.Containerfile` (Fedora 45, its python3, uv 0.13.7) and a `command` running the locked pytest in it. The image's build and boot gate moved from `container.yml` to `[tool.sdlc.image]`. The contract test (`packaging/container-test.sh`) did not: sdlc does not run `contract-test` until sdlc#118 (sdlc#168), so the image ships without it until then. The key stays in `[tool.sdlc.image]` so the gate returns with that release. The tests that read `container.yml` now check that contract and that a tag push builds nothing. Dependabot is dropped with its auto-merge workflow; Renovate keeps the pins current, as across the fleet.

### Fixed

- The app's bot is trusted under its real login, `ashigaru-crunchtools-com[bot]` (GitHub id 340727104; a1409). The image set `ASHIGARU_FORGE_AUTHORS_GITHUB` to `ashigaru-crunchtools[bot]`, a login GitHub does not have, so the Desk sweep listed the app's own release PRs as outsiders in NEEDS YOU instead of queuing them. The scripts' defaults carried the same wrong name: commits now go out as `ashigaru-crunchtools-com[bot]` with its `340727104+` noreply address, a review result finds its earlier comment to update, and the `--bot` defaults of `findings.py`, `release.py`, `release_publish.py` and `sweep.py` name the app `ashigaru-crunchtools-com`.

- A closed subject drops off the sweep's outsiders (a1409). A run no longer lists an outsider's subject the search finds closed, and removes it from the earlier runs' lists, so NEEDS YOU stops showing PRs and issues already closed.

- `ashigaru wrap` (and `desk_wrap`) no longer records `cleared` when the /clear never reached the seat's pane (a1385, #261). The `cleared` stage now requires the item's seat to have started a session since it took the item, which every landed /clear does through its SessionStart hook (the seat's epoch is past the item's and it names a session). Until then it is 409 `not-ready` ("the /clear has not landed") and the item stays `wrapping`, `--force` included: `--force` still skips the CC-DONE and idle checks, and passes the clear check only for a seat that cannot show it, one that is missing or never registered a session, or an item recorded before 5.0. An item `POST /v1/items` records on a seat now takes that seat's epoch, as a `PATCH` move to one already did, so it is not left in that last case. A forced close without the clear ends the seat's session, so a seat back from a lapse in its old context is fenced out (`stale-session`) instead of taking new work that the late /clear would then block. Before, a wrap after a /clear that never fired, or a forced one, closed the item and freed a seat that still held the old context. The skill's wrap step says so; its pane recipe already falls back from the claude-agent socket to the default `dev` session, which is where every Dolza and Exedore Max seat runs today.

- A fix round pushes with `git push --force-with-lease=<branch>:<head> origin HEAD:<branch>`, where `<head>` is the pull request head the round collected its findings and checks on (a1109). A branch that moved since then is refused: the round pushes nothing and answers nothing, and the push that moved it starts a fresh round on the new head. The round also yields, before applying anything, when the branch it checked out is no longer that head, so a patch that conflicts with code the agent never saw no longer hands the PR to a person; and a round with no patch checks the remote head before it answers a finding. Before, a plain push could land a fix written against code the agent never saw. The fleet CI design (sdlc `docs/marketplace-free-ci.md`) requires the leased push, and sdlc's write step calls this script.

## [5.4.0] - 2026-11-23

### Changed

- The `assign` brief's Scope line says "One PR per repo; one commit per step." (a872): a change ships as one pull request in each repo it touches, never a PR per part.

### Security

- The code and fix publish jobs refuse a patch that touches CI or gate configuration, not only `.github/` (a1072). The check runs after `git apply --index` on what git staged against HEAD, so a quoted path, a rename, a binary diff or a diff prefix setting hides nothing. Refused: `.sdlc/`, `.semgrep/`, `.gourmand-exceptions.d/`, `.specify/memory/constitution.md`, the root `AGENTS.md`, `CLAUDE.md` and `.gemini/styleguide.md`; any file named `gatehouse.toml`, `gatehouse-findings.toml`, `.gatehouse-ignore`, `gourmand.toml`, `gourmand-exceptions.toml`, `gourmand-patterns.toml`, `gourmand-audit-ledger.toml`, `sdlc.toml`, `lychee.toml`, `.semgrepignore`, `.hadolint.y*ml`, `.markdownlint*`, `.gitignore`, `.ignore` or `.gitattributes`; a symlink onto any of them or out of the tree; and any change to `pyproject.toml`'s parsed `tool.sdlc`, or a pyproject that no longer parses. The refusal names what it hit. `[tool.ruff]` per-file-ignores and coverage `fail_under` stay open by design (docs/authority.md).

- `examples/ashigaru.yml` audits clean under zizmor (a860). Its `issue_comment`, `workflow_run` and `pull_request_target` triggers were dangerous-triggers highs in every enrolled repo once constitution 1.58.0 began running zizmor; each now carries an inline `zizmor: ignore[dangerous-triggers]` with the reason it is safe: the router checks out nothing, and the only jobs that check out code act on this repository's own `ashigaru/` branches. The events are unchanged. This repo's Gatehouse callers carry the same.

## [5.3.0] - 2026-11-21

### Added

- A Fedora 45 RPM. Each release now attaches `ashigaru-X.Y.Z-1.fc45.noarch.rpm` (Python 3.15) next to the fc43 and el10 ones, with the `ashigaru-desk` and `ashigaru+desk` subpackages and the source RPM. CI runs the test suite on Fedora 45's Python 3.15 against `uv.lock`; it passed with no code change.
- The Desk treats Renovate's PRs (`renovate[bot]`) as it does Dependabot's
  (a852): not queued on first sight, never listed under NEEDS YOU as an
  outsider, and one still open `ASHIGARU_DEPENDABOT_AFTER` (a day) after it was
  opened is queued `auto` on exedore-max: "Land or close Renovate <key>".
  Renovate opens a PR a week after a release and automerges it when green, so
  one still open is stuck. The backfill mark is now `sweep-bot-backfill`, so the
  first run reads old runs' outsiders back once more and records any Renovate PR
  they listed.

## [5.2.0] - 2026-11-21

Upgrade note: deploy the Desk before the clients. `ashigaru assign --task` now
sends `assign` with the create, which a 5.1.0 Desk answers 422, recording
nothing. There is no fallback to the old two requests: it would bring the race
back.

### Fixed
- `assign` no longer writes over a claim it did not read (#245, a843):
  `POST /items/{id}/assign` is 409 `taken` for an item another seat holds unless
  the caller names the version it read (`If-Match`, `--if-version`,
  `if_version`), and no version takes Scott's held item (409 `held`) or one
  being wrapped (409 `wrapping`); with the version the item moves and, if it
  was `assigned` or `working`, the losing seat's session is ended, so its next
  request is 409 stale-session.
  `POST /items` takes `assign: {seat}|{lane}|{}` and records and assigns in one
  transaction, so the item is never seen `queued` and a refused assign records
  nothing; `ashigaru assign --task` and MCP `desk_assign` with `record` use it
  (`desk_assign` refuses `if_version` with a record), and the "recorded aN, not
  assigned" outcome is gone.
- The `stop` hook, answered 409 stale-session, prints the stale notice on stderr
  and exits 2, which tells the model before it stops, except on a Stop with
  `stop_hook_active` or when the Desk names a newer session.

## [5.1.0] - 2026-11-20

### Changed
- The seat-side CLI keeps no local state (a837): `~/.local/state/ashigaru/epoch`
  and `task-session` are gone, so a seat whose `~/.local` it cannot write (podman
  makes it root's as the parent of a bind mount) no longer goes `missing`. Every
  request names its Claude Code session as `X-Ashigaru-Session`, a hook's from
  its event, the CLI's from `CLAUDE_CODE_SESSION_ID`, and the Desk fences on that
  alone, taking the epoch from the seat's row; `X-Ashigaru-Epoch` from a 5.0
  client is still honoured. The Desk keeps a seat's task from its session's
  first typed prompt (startup now drops the last one's, as /clear did), and
  `hook next` tells a newer session taking over from an ended one by the
  `session` a stale-session 409 now carries.
  Deploy the Desk before any 5.1 client: a 5.1 client sends no epoch, which a
  5.0.1 Desk refuses as a stale session.

### Fixed
- A session the Desk ended after its lease lapsed is fenced out by its session
  id too: ending it clears the seat's `session_id` as well as moving the epoch,
  and migration 13 clears the id of every session a 5.0 Desk ended.
- A usage report no longer writes a registered seat's `session_id`: one naming
  another session than the seat's is 409 `stale-session` from anyone, and a
  seat's own whose header and body sessions differ is refused.
- `ashigaru` outside a seat's session (no `CLAUDE_CODE_SESSION_ID`) says so on a
  stale-session refusal.
- A sweep test no longer fails once its fixed timestamp is a day old.

## [5.0.1] - 2026-11-20

### Fixed
- An idle seat, and Josui Prime while it works, stay on the board instead of
  showing `missing` (a831). `ashigaru hook next` no longer exits when the Desk
  says the seat cannot take work (a seat outside autodispatch, no lane, a
  refused key): it checks in every 3 minutes, inside the 10-minute idle lease,
  and exits only to hand over a brief or on a stale session. The Desk renews a
  seat's lease on any authenticated request from the identity the seat is named
  for, so Prime's CLI calls on its admin key count, where only a `seat` key's
  own reports did; a request from an ended session still renews nothing.

## [5.0.0] - 2026-11-20

### BREAKING
- Seats have sessions, epochs and leases. A seat registers each session with
  `POST /v1/seats/{name}/session`, and every request it makes carries
  `X-Ashigaru-Epoch`, and each hook its Claude Code session as
  `X-Ashigaru-Session`; one from a session the Desk has ended, or another
  session's hook on the seat, is 409 `stale-session` and changes nothing. A
  seat is `missing` (never picked or assigned) when its lease is not live (10
  minutes idle, 60 mid-turn), where it was "no write for 24 hours"; that alone
  keeps its session and its work. A seat that never registered a session (a 4.x
  seat, epoch 0) is still served without an epoch, and its first resume adopts
  its session and keeps its work; once it has registered one, a REST request
  without an epoch is 409 `stale-session`. MCP tool calls name no session and
  are not fenced.
- Rollout: the Desk and Prime's skill together, then the seat images
  immediately; a seat on a 4.x image is refused once its first 5.0 session
  registers. Rolling back to 4.3 keeps migration 12's unique index, so a 4.3
  reopen or seat change onto an occupied seat is a 409; leases recorded under
  5.0 are stale on the roll forward, so every seat is `missing` until its next
  request.
- Seat occupancy is derived. A seat holds at most one open item, and its row's
  `status`, `item` and `owner` are recomputed from that item on every item
  write. An admin `PATCH /v1/seats/{name}` that would change one of them is 409
  `derived`: assign, claim, update or wrap the item instead.
- `desk_wrap` and `desk_report` take their body as `fields` (a WrapUp or a
  Report object), not as loose arguments.
- Migration 12 changes stored rows: it drops the seat of queued and proposed
  items, and where a seat held several open items keeps the one its row named
  (else the newest) and requeues or unseats the others, each with an event from
  source `migration`: Josui's `assigned` or `working` item goes back to the
  queue, any other goes `blocked` with the reason, under NEEDS YOU.

### Added
- PRs are linked by construction. A brief now tells the seat to branch
  `fix/aN-<short-slug>` (the gateways take only typed prefixes) and end the PR
  body with `Ashigaru-Item: aN`. Intake, webhook and sweep alike, links a
  writer's PR to the open, pr-less item in its repo that it names (trailer, else
  a head branch `aN-` or `aN/` after one type prefix or none, else `(aN)` in the
  title or body) and inserts no item. A PR an item already names never queues
  another. A writer's PR that names nothing while a seat holds an open pr-less
  item in its repo is recorded with `unlinked_at` and listed under NEEDS YOU as
  `unlinked` until `ashigaru update aN --pr <url>` names it.
- Versioned writes. Items carry `version` (their last event's seq), and PATCH,
  report, file, wrap, assign and claim take `If-Match: <version>`, answering
  412 `version` when the item moved. The CLI takes `--if-version N` on update,
  report, wrap, assign, claim and file, prints the Desk's reason and the item
  as it now stands, and exits 1; item tables show VERSION. The MCP tools take
  `if_version`.
- 409 and 412 bodies carry a `code`: `stale-session`, `busy`, `held`, `unseen`,
  `no-lane`, `missing`, `derived`, `not-ready`, `version`.
- A seat's turn starting on Josui's item still `assigned` moves it to
  `working`. When that turn is Scott's first typed prompt instead, the item goes
  back to the queue and the seat becomes his, with a new manual item.
- NEEDS YOU lists blocked items no seat holds, such as one cleared before its
  wrap.

### Changed
- `session-start` registers the session and writes the epoch to
  `~/.local/state/ashigaru/epoch`; on a 4.x Desk it falls back to the old PATCH,
  and on a stale resume it prints a notice on stdout. `next` retries `busy` and
  `held` with backoff instead of stopping, wakes the seat on `stale-session`
  (silently stops when a newer session superseded it), and stops on any other
  409 as before.
- Startup and `/clear` end the seat's previous session: Josui's `assigned` or
  `working` item leaves the seat `blocked`, any other state as it stands; a
  wrapping item stays for `wrap`; Scott's manual item is wrap-cleared on clear
  and left open and seatless on startup. A resume of the seat's current
  session keeps its epoch and its work however long it was idle. Reconcile ends
  a session whose lease ran out a day ago (`STALE_SEAT`, counting no silence
  from before the Desk came up) and requeues Josui's `assigned` and `working`
  items.
- Assign never picks a missing, held, busy or mid-turn seat; naming a missing
  one is 409 `missing`. Moving an item to another seat takes that seat (409
  `busy`, 409 `unseen`) and frees the old; a move to queued or proposed frees
  the seat, and giving a queued item a seat makes it `assigned`.
- An item `next` handed a seat that is still `assigned` after 10 minutes goes
  back to the queue. An item Josui assigned is never handed back.
- Intake judges a delivery against the row as it was; one that moved before it
  applied is rolled back and judged again, up to 3 times, instead of dropping an
  approval. An older delivery by the forge's `updated_at` no longer rewrites the
  item's source title, labels, state or ref.
- The ledger importer keeps a seat on one open item; a later line that would seat
  a second leaves the name in `extra.ledger_seat`.

### Removed
- The sweep's `(aN)` LINK block; intake links PRs for the sweep and the webhook.

## [4.3.2] - 2026-11-20

### Fixed
- A brief the Desk hands a seat on autodispatch now ends with the standing
  limits a hand-written brief carries: don't merge, tag, release or deploy;
  don't message other seats; fan out with Sonnet subagents; report DONE or
  BLOCKED to Josui Prime with Filed and Learned lines; run /cc seat when
  asked; answer Gatehouse findings in the triage format. Before, a seat given
  a Desk item merged and released it itself.

## [4.3.1] - 2026-11-20

### Fixed
- A seat cleared at the end of an auto wrap now gets its next item. The /clear
  lands before `ashigaru wrap` closes the item, so the seat's `next` hook found
  it busy, took the 409 as final and never asked again. The Desk now answers
  204 while the seat is idle and busy only with an item at wrap stage
  `wrapping`, and the hook claims once the item closes; a wrapping seat still
  mid-turn gets the 409 as before.

## [4.3.0] - 2026-11-19

### Changed
- The sweep no longer lists Dependabot PRs under NEEDS YOU as outsiders. It
  records them as subjects, reads them back every run while open, and queues
  one still open `ASHIGARU_DEPENDABOT_AFTER` seconds (a day) after it was
  opened as an `auto` exedore-max item: "Land or close Dependabot <key>". Old
  runs' Dependabot outsiders are read back once and dropped from NEEDS YOU.
- A brief handed to a seat carries the item's `done_when`, `ref`, `repo` and
  `note` when set, not only its task.

### Fixed
- A seatless item nobody has started (`manual` and still `queued` or
  `proposed`, such as a held PR) now closes when its subject or PR merges or
  closes, as a seatless `auto` one did; before, it stayed queued.
- A typed prompt on a free seat that still carries an open `manual` item (an
  imported one, or one left `blocked`) takes that item back instead of opening
  a second one.
- The prompt-submit hook no longer takes a scheduled task's prompt (CronCreate,
  `/loop`, a routine) for the session's first typed prompt. Claude Code submits
  those bare, so the hook looks for the `scheduled_task_fire` entry it writes
  to the transcript.

## [4.2.0] - 2026-11-19

### Added
- `ashigaru work` with no flags is the seat board: one GET of `/v1/status`,
  three sections (On the seats, Needs you, Changed since you looked) in one
  column layout, one line per row. `--markdown` prints Markdown tables fitted
  to the terminal, with state marks and OUTCOME linked to GitHub or RT;
  `--wide` shows every column. `--all`, `--since` and `--advance` run the
  round as before. Board rows carry pr, done_when, check_after and step;
  NEEDS YOU rows carry task, outcome and since.
- `POST /v1/changes` takes `{"until": N}` and acks no further than that
  event, so the board acks only the changes it printed.
- The Desk hands out work itself. `POST /v1/seats/{name}/next` claims the
  oldest queued auto item in a free seat's lane whose `after` items are done,
  long-polling up to 90 seconds, and returns the item and its brief.
  `ashigaru hook next`, on SessionStart (startup, clear) as an async
  `asyncRewake` hook, waits on it and wakes the idle seat with the brief by
  exiting 2. The Desk claims only for an idle seat, and the hook rides out a
  Desk restart with backoff, waiting up to a day. The `autodispatch` mark is
  the switch: nothing flows until an admin sets it to `on`.
- The Desk flags a busy seat that has gone silent (migration 11, `silent_since`):
  seat reconcile marks one with no write for `ASHIGARU_SILENT_AFTER` (an hour),
  after the Desk has been up that long, and the board lists it under NEEDS YOU as
  `silent`. Any write to the seat clears the flag.

## [4.1.0] - 2026-11-19

### Added
- Seat reconcile (migration 10): at startup and every 5 minutes, `serve` ends a
  seat turn active past `ASHIGARU_TURN_MAX` (3 hours) with no write for 10
  minutes (counted from no earlier than the Desk's own start, so a restart after
  an outage spares live turns), marking `turn_reconciled_at`, and drops the task
  of a seat idle a day with no hold of Scott's. A hold of Scott's on a seat
  silent for a day shows under NEEDS YOU as `stale`; it is never closed for him.
- `ashigaru changes [--since T] [--ack]` (`GET`/`POST /v1/changes`,
  `desk_changes`): what others changed since you last looked, each item once,
  last changed first, with who wrote it, the fields that moved and its NEXT.
  Each identity keeps its own `seen:<identity>` mark; `--ack` moves it, only
  forward. `status` lists the same under CHANGED SINCE YOU LOOKED without
  moving it.
- Outcomes: `create`, `update` and `assign` take `--outcome <id, RT#N or issue>`
  (found or recorded as an outcome) or `--outcome-text "<instruction>"` for
  Scott's one-off instruction with no ticket (MCP and REST: `outcome`,
  `outcome_text`); either sets `parent`. A review or fix-up of a story's PR
  inherits the story's outcome. An outcome is never a work row: it closes
  itself once all its stories have. A PR or blank instruction text is refused.
- `ashigaru work` shows exactly WORK, TIME, SEAT, STATE, OUTCOME, TASK, NEXT,
  grouped by outcome with groups needing Scott first and work serving none
  (`-`) last. STATE carries the mode (`manual · blocked`; auto bare); NEXT is the
  one next step, folding in the blocker, `landed: wrap it`, `overdue since
  HH:MM` and a PR's red CI. `ashigaru status` leads with OUTCOME, then MODE.
- A task, never cut, now word-wraps inside its own column to fit the terminal
  (120 columns when there is none); the other cells stay on the row's first
  line. NEXT takes the width left and is clipped only when the row overflows.

- Server-side sweep: with `ASHIGARU_FORGE_TOKEN_GITHUB` and
  `ASHIGARU_FORGE_ORG_GITHUB`, `serve` re-reads every open item's subject and
  PR, searches the org for updated subjects (queuing only members' and trusted
  authors', listing others' as outsiders for Scott), links a writer's PR that
  names `(a676)`, and settles items, at startup, every `ASHIGARU_SWEEP_EVERY`
  seconds (600) and on request. It never writes to GitHub; the token now also
  needs issues and pull requests read access and the search API. Runs are
  recorded in `sweeps` (migration 9). One runs at a time under a renewed
  60-second lease, so a run killed by a restart frees it within a minute; a run
  that finds a live lease is recorded as skipped, and the timer retries.
- `GET /v1/sweep`, `POST /v1/sweep` (admin), `ashigaru sweep [--run]`, MCP
  `desk_sweep`, and a sweep line on the `ashigaru status` board.
- Subjects (migration 8, `GET /v1/subjects/{key}`): the forge's own state of
  each issue and PR, from the webhook, now also `pull_request_review`.
- Items carry `pr` (`--pr`, `--clear pr`; a seat may set it) and server-set
  `landed_at`.
- The Desk settles items on delivery (and in `work`/`status`): seatless auto
  items close when their subject merges or closes and requeue on reopen; seat
  and manual items only get `landed_at`.
- `ashigaru status` (`GET /v1/status`, MCP `desk_status`): the board a person
  reads. Busy seats with their whole task, free and missing seats, `X of Y up,
  Z open`, and NEEDS YOU: idle manual seats with their question, Scott's
  blocked manual work and subjects held for approval.
- Seats carry `waiting_on`, the last thing they asked, captured by the `stop`
  hook and dropped on a new turn or `/clear` (migration 7; a seat may PATCH it).
- In `agents` and `work`, ITEM is now WORK and FOR is now TIME, TASK is never
  clipped, and the tally reads `N of M up, K open`.

### Changed

- The `/ashigaru` skill's round reads the Desk instead of panes and GitHub:
  an overdue item is judged by its seat's TURN and SEEN, `landed: wrap it` is
  wrapped, `changes --ack` gives the deltas between rounds, and `queue` no
  longer re-reads issues the Desk already closed (only an RT ticket is checked).
- Intake (webhook and sweep) no longer queues a subject authored by a bot
  that is not a trusted author (dependabot, say); it used to be held.
- Promote signs the image with the fleet cosign key (`sign: true`), because
  bootc hosts are moving to trust only signed `quay.io/crunchtools` images
  (crunchtools/rhel10-bootc-server#47).

### Changed
- Gatehouse triage gets `review_result` from the review job, so a pull request
  whose review died before posting anything is held instead of passing.
- The constitution's workflows are called at `@v1`, the major tag each release
  moves (XV rule 5, constitution 1.45.0), and `Inherits:` is v1.51.0: that tag
  alone is the version this repo is judged by, so the repo stops sitting
  releases behind. `container-release.yml` was pinned at v1.41.0 and the other
  three at v1.46.0. The container job also grants `issues: write`, so a failed
  weekly rebuild opens an issue (constitution 1.51.0) instead of only warning.
  A `v*` promote now also refuses an image whose `version` label names a
  different release of this repo (constitution 1.50.0); this image sets no
  `version` label, so the check is inert here.

### Fixed
- Publishing a release no longer leaves a red Container run. `major-tag.yml`
  moves `v4` on every release, `container.yml` ran on it, and promote refused
  it with "tag 'v4' is not vX.Y.Z" (most recently run 44427128848). The push
  trigger is now `v*.*.*`, which the major tag does not match.
- Every timed sweep failed with HTTP 422: GitHub's issue search refuses a query
  without `is:issue` or `is:pull-request`. The sweep now searches `is:issue`
  and `is:pr` separately, merges the hits, and when either is cut at the cap
  moves the mark only to the lower point the two reached.

## [4.0.2] - 2026-11-19

### Fixed
- `ashigaru wait <repo> <sha>` (and `desk_wait`) no longer says `success` while
  a check the target branch requires has not reported (crunchtools/ashigaru#215).
  The Desk reads the branch's required checks from GitHub (repo and org
  rulesets, and classic protection) and the commit's check runs and statuses;
  one that never started keeps the commit `pending`, one that failed fails it,
  and when they cannot be read the result is never `success`. Names match
  exactly, and a check required from an App (`integration_id`, `app_id`) counts
  only that App's runs. At its timeout,
  `wait` prints `pending: <names>` and exits 1. New `--branch` (default the
  repo's). The Desk's GitHub token now also needs read access to metadata,
  checks and commit statuses.

## [4.0.1] - 2026-11-18

### Fixed
- Usage cost reported before billing was known is no longer lost (a355). The
  Desk keeps `session_booked_usd`, the part of the session's cost already in
  spend, and books the rest on the first report that carries billing; a late,
  lower report never rewinds it. Billing now comes from the seat's lane, else
  the report, else the row (an admin's report outranks the lane); the client no
  longer hardcodes a lane table or looks up its own seat on every post.
- The documented status-line wiring skips starting Python (~75 ms) while the
  session's throttle state file is under 30 seconds old (and runs it
  when the file's age is unknown or negative), and the docs say
  session tokens include cache reads, so they read in the millions.
- The catch-up's `reopened` is the delivery that took a subject from closed to
  open at the source, not every later delivery on an item closed on the Desk
  and open there (a374).
- `--advance` only moves the `sweep` mark forward, so a round that lost a race
  to a newer one never moves it back; `advanced` says whether it moved.
  `--since` with `--advance` is refused, since that round could move the mark
  past events it never read (a374).
- The record issues the sweep keeps (`ashigaru-inbox`, `ashigaru-releases`,
  `ashigaru-red`, `ashigaru-observe`) are never queued: the inbox and the red
  issue carry strangers' text and arrived trusted and `auto` as the bot's own
  issues; one queued before is held on its next delivery (a374).
- The CLI-to-MCP drift test compares choices on either side, required-ness of
  carried fields, nullability and defaults, and checks `docs/desk-api.md`'s
  tool names (a374).

## [4.0.0] - 2026-11-18

### BREAKING
- The hosted MCP tools are renamed to `desk_<command>` for each `ashigaru`
  command: `usage`, `agents`, `work`, `queue`, `outcomes`, `assign`, `auto`,
  `manual` and `wrap` are now `desk_usage` ... `desk_wrap`. The old names are
  gone; an MCP client calling them gets an unknown tool.
- `desk_wait` returns `{"result": ...}` around the item or the CI result, so a
  client reading `structuredContent` unwraps it.

### Added
- `ashigaru work` runs the whole check round in one call (a335): `GET /v1/work`
  returns the open items, every seat with `N of M up, K items open`, the queue
  with `waits_on`, and a GitHub catch-up from the org webhook's intake after
  the `sweep` mark (`new`, queued or held, and `reopened`), so Prime runs no
  `gh` sweep. `--advance` (admin, `POST /v1/work`) moves the mark to the last
  event the round covered: events are numbered in commit order, so a round cut
  at 1000 reads on next time and a delivery committed mid-round is the next
  round's. `--since` reads from a time. The check table stops at 1000 rows
  (`truncated`).
- `ashigaru claim` and `ashigaru file`, the CLI side of the seats' MCP tools.
- `tests/test_skill.py` fails when an MCP tool drifts from its CLI command: in
  name, in arguments (a carried model's fields too), their types, choices and
  whether each is required; or when a doc names a tool that does not exist.

### Changed
- `desk_work` returns the round; `desk_get` takes `subject_key` or `ref`;
  `desk_assign` takes a `record` (`assign --task` and its fields) to record
  the item first; `desk_wait` takes an `item_id` as well as a `repo` and `sha`.
- Desk intake trusts the accounts in `ASHIGARU_FORGE_AUTHORS_<FORGE>` as
  authors of their own issues and PRs (the image sets
  `ashigaru-crunchtools[bot]`), so Ashigaru's own issues are queued, not held,
  as the old `gh` sweep recorded them; their lane labels approve nothing.
- `ashigaru work` and `GET /v1/check` list only open items: `done` rows that
  were never wrapped (old imports) no longer show without `--all`.

## [3.4.0] - 2026-11-18

### Added
- `ashigaru --version` prints the installed package version and exits 0 (#206).
- `ashigaru hook statusline` (a328): run from a seat's status line, it posts
  the model, context, session tokens and running cost to
  `POST /v1/seats/{name}/usage`, so `ashigaru usage` and its billing table show
  real numbers. Billing follows the seat's lane. At most one post per 30
  seconds per session, state on tmpfs, never any output.

### Fixed
- A narrow `single_use_helpers` exception for `CIWaiter._ask`, `_poll`'s twin.
  The existing `_poll` entry only covered `_ask` because Gourmand ignored its
  `pattern` and suppressed the whole file; Gourmand is about to honour `pattern`
  (crunchtools/gourmand#43).

## [3.3.0] - 2026-11-18

### Fixed

- Desk intake: a PR trusted because its author writes now records `approved_head` too, so a push by anyone who may not write (a collaborator on the writer's fork) holds it like an approved PR; once held, only a maintainer's approval restores it, not the author check. A writer-authored PR queued before this release has no head on file and is held on its next delivery.
- Desk intake: a held PR records `held_head`, moved only by a writer's push from it, and the author-check retry trusts the PR only at that head. A non-writer's push to a held PR, or a delivery without a head, no longer lets a later successful author check trust content nobody vouched for.

### Changed

- Desk intake approval (#196): a PR's approval is tied to its head commit, and any delivery at another head holds it again unless it is a writer's push from the approved head; a judgment made against a row that changed before it was applied grants nothing; a writer removing the approved `lane:` label holds it again; a held item's author is asked about again on each delivery, so a timed-out permission check is retried; and a hold or lane Josui set wins over a maintainer's label, which is then recorded only. Withdrawals are recorded under `extra` as `held_by`, `held_at` and `held_for`.

## [3.2.0] - 2026-11-18

### Added

- The `/ashigaru` Claude Code skill (a261) moves here from josui-skills, rewritten for the 3.1.0 CLI: the stopgaps for its old gaps are now `create`, `update`, `report`, `get`, `list` and `mark` calls, and it has a section for every CLI command, `hook` included. The `ashigaru` RPM installs it at `/usr/share/ashigaru/skills/ashigaru/SKILL.md`, and a user links that directory into `~/.claude/skills` (`docs/desk-cli.md`); `packaging/smoke.sh` checks it is installed. `tests/test_skill.py` fails if the skill's commands (headings, argument-hint, description, mcp actions) differ from the CLI's in either direction, or if any command line in the skill does not parse.

## [3.1.0] - 2026-11-18

### Added

- The `ashigaru` CLI closes the gaps the `/ashigaru` skill hit (a253): `create` records an item without assigning it, with `subject_key`, and on a taken key or id prints the item holding it; `assign --task` takes `--subject-key` and prints the item it recorded when the assign is refused; `get` finds an item by id, `subject_key` or ref; `list` filters by several states, `subject_key`, `ref`, `learned` and `memorized`; `update` sets and clears fields; `report` is the CLI's `desk_report`; `wait <repo> <sha>` long-polls `GET /v1/ci/wait`. Desk: `GET /v1/items` takes those filters, `GET /v1/queue` and `POST /v1/items/{id}/wrap` move `queue`'s `waits_on` and `wrap`'s CC-DONE and idle checks to the server so the CLI and MCP give one answer, the MCP gains `desk_create`, an admin may set `filed_by`, a 409 on a taken `subject_key` names the item holding it, and `GET /v1/seats` marks `missing` seats, including seat identities that never checked in. A `learned` of `none` is no Learned line on every route, and the queue view says when it was `truncated`. `ashigaru mark <name> [--set VALUE]` (MCP `desk_mark`, `GET`/`PUT /v1/marks/{name}`) keeps a named watermark on the Desk, such as how far the sweep reached, replacing the ledger's `{"swept": ...}` line; migration 5 adds its table.
- The Desk's webhook receiver (a236): `POST /webhook/<forge>` (`/webhook/github`), the one path the internet reaches. Each forge adapter (`src/ashigaru/desk/forges/`, GitHub first) verifies its own signature against that forge's secrets, timing-safe; anything else is 401 with nothing echoed. Deliveries dedupe on a digest of the forge, event and signed body (not the unsigned delivery id), in the same transaction as what they change, so a replay is a no-op. CI events (`workflow_run`, and `check_suite` from Apps other than Actions) become one normalized result per (repo, commit): status plus each check's name and verdict, a newer run always winning and, within one run, the newest forge time, so a late `in_progress` never undoes a `completed`. Issue and PR events upsert queue items on `subject_key` (`owner/name#N`): an issue a queued outcome, a PR a story, the task in the Desk's words and never the source's title. An item is claimable only when its author may write to the repo (GitHub's collaborator permission API, token `ASHIGARU_FORGE_TOKEN_GITHUB`), with `lane:<lane>` the lane hint, or once a writer approves it by adding a `lane:` label (recorded as `approved_by` and `approved_at`); anything else, including a writer's edit or reopen of an outsider's issue, is held, no lane and mode manual, for Josui; later deliveries refresh only the ref and `extra`, never Josui's task, lane or state. `GET /v1/ci/wait` and the MCP tool `desk_wait` long-poll a commit's result and return the moment its delivery lands, up to 90 seconds a call. Secrets come from `ASHIGARU_WEBHOOK_SECRET_<FORGE>` or its `_FILE` (one per line, for rotation), with the bare `ASHIGARU_WEBHOOK_SECRET` counting for GitHub; never the database or a log.

### Changed

- `wrap` means one thing in the CLI and MCP: `--stage wrapping` (MCP `stage: wrapping`) records that `/cc seat` was sent, and `cleared`, now the default on both, checks CC-DONE and an idle turn before it clears and closes, and refuses for a seat that never reported. The MCP `wrap` tool no longer clears without those checks unless `force` is passed, and the MCP `queue` returns `{queued, proposed}` with `waits_on`, as the CLI's does.

### Fixed

- `ashigaru-desk import` sets `subject_key` from each item's `ref`, so the Desk's 409 dedupe covers work the JSONL ledger already tracks and the first sweep after cutover does not queue it again. Only an open item gets the key, the newest where several share one ref; closed items stay unkeyed, and a key already set or held elsewhere is never touched. Every import runs this pass, so re-importing a Desk filled before this fix backfills it. `POST /v1/items` and `PATCH` store a ref sent as `subject_key` in the Desk's key form, through the same helper (`ashigaru.desk.model.subject_key`): a GitHub issue or PR URL or `owner/repo#N`, in any case, becomes `owner/repo#N` in lowercase; an RT URL or `RT #N` becomes `RT#N`. Any other key is stored as given.

## [3.0.0] - 2026-11-18

### Changed

- BREAKING: Ashigaru 3.0 adds the Desk, Josui's system of record for seat work, and moves the service's web stack (FastAPI, uvicorn) out of the base package into the `desk` extra. Anything that imported the web stack through a plain `ashigaru` install now installs `ashigaru[desk]` or the `ashigaru-desk` RPM. The reusable workflows move to `@v3`: `router.yml` and the workflows it calls name each other at `v3` and fetch their scripts there, and publishing 3.0.0 moves `v3`. The `v2` tag stays at 2.15.0, so an existing caller keeps running 2.15.0 until it is re-copied; re-copy `examples/ashigaru.yml` to follow 3.x.
- Desk `claim` fails closed on an item with no lane: no seat can claim it, and Josui sets its lane or assigns it directly (a232).
- `ci.yml`'s `sdlc ci` job no longer stops at `ASHIGARU_ENABLED`. It runs no model, and as a required check a skipped run counted as passed, which constitution v1.46.0 rejects. Its step now runs `.sdlc/bin/sdlc ci` alone.

### Added

- The Desk infers Scott's hold (a238): a seat with no item that reports its session's first typed prompt (turn active with a task) becomes busy and held, on a new manual item carrying that task, the way /agents ledgered untracked manual work; `/clear` or a wrap releases it, and the hold follows the item's mode. Seats still write only `turn` and `task`, so none can claim or drop a hold. Seats carry a computed `hold` (`free`, `busy-auto`, `busy-manual`), which `ashigaru agents` shows.
- The Desk core (3.x, `src/ashigaru/desk`): the item and seat models with every ledger field and state plus wrap-up, SQLite with numbered migrations, a REST API over items, the report contract, `desk_file`, seats, the `/agents check` view and identities, per-identity API keys (bearer header, SHA-256 at rest, minted by an admin, revoked by an admin or the owner) scoped so a seat writes only its own items, and an idempotent import of Josui's JSONL ledger. Dispatch: `assign` with seat-pick by lane, an atomic `claim` (desk_claim), seat usage and spend by billing source against limits, and the outcomes rollup. A seat cannot reopen or file under a closed item, nor change its seat row's lane, status, owner or item. Contract: `docs/desk-api.md` and `docs/desk-api.json` (a218).
- RPMs, built on GitHub (a231): `ashigaru` (the CLI and the Python package, no web stack) and `ashigaru-desk` (the service, with FastAPI and uvicorn), for Fedora 43 and EL10 (EPEL). `.github/workflows/rpm.yml` builds each target in a fresh container from `packaging/ashigaru.spec`, installs the RPMs in another and runs every command with `--help`, then attaches them to the release; pull requests that touch the package build and test without publishing. The service's web stack moved to the `desk` extra, and the package version is now `VERSION`'s.
- The Desk image, `quay.io/crunchtools/ashigaru` (a231): a Hummingbird Python base, uid 65532, read-only root, the SQLite file on the `/data` volume, `ashigaru-desk health` as its HEALTHCHECK. `.github/workflows/container.yml` builds, gates and promotes it through the constitution's `container-release.yml` (`:main` on the default branch, `:latest` and `:X.Y.Z` on 3.x tags); `packaging/container-test.sh` is the contract test. `ashigaru-desk serve` reads `ASHIGARU_DESK_DB`, `ASHIGARU_BIND`, `ASHIGARU_PORT` and `ASHIGARU_TRUSTED_PROXIES`; forwarded client address and scheme are believed only from a trusted proxy.
- The `ashigaru` CLI (`src/ashigaru/cli`), a client of the Desk API with the command set the `/ashigaru` skill and the MCP tools share: views `usage`, `agents`, `work`, `queue`, `outcomes`; actions `assign`, `auto`, `manual`, `wait`, `wrap`; tables by default, `--json`. Config from `~/.config/ashigaru/config.toml` (0600, refused when looser) or `ASHIGARU_URL` / `ASHIGARU_API_KEY` / `ASHIGARU_API_KEY_FILE`, which win; never a key in argv, https only (http to loopback), redirects not followed. `ashigaru hook session-start|prompt-submit|stop` replace the seat-state.json hooks: bounded at 2s, silent on stdout, exit 0 on any failure. `wait` polls the item with 30-60s backoff until the Desk has its CI callback. `httpx` is now a runtime dependency of the base package, so the `ashigaru` RPM carries it. Docs: `docs/desk-cli.md` (a229).

## [2.15.0] - 2026-11-17

### Changed

- Release tagging no longer stops when `ASHIGARU_ENABLED` is `false`. Switching Ashigaru off skipped `release.yml` and `major-tag.yml` too, so a release pull request that merged afterwards was never tagged, and because a tag pushed with `GITHUB_TOKEN` starts nothing, `container.yml` and `publish.yml` would have stopped without an error (a194). The tagging now lives in a new reusable `release-tag.yml` with the gate `release.yml` carried, minus the switch; it mints the app token with `actions/checkout` and `actions/create-github-app-token` pinned by commit. `router.yml`'s release route calls it ungated, `release.yml` is now a thin call to it for callers copied at 2.13.1, and `major-tag.yml` moves `v2` with the switch off. No caller changes. Triage, code, fix, review and the sweep still stop at the switch.

## [2.14.0] - 2026-11-17

### Added

- A caller is now a thin router: `examples/ashigaru.yml` has one job that calls the new reusable `router.yml@v2`, passing Ashigaru's three secrets by name (not `inherit`, which would hand a moved `v2` every secret in the repository), subscribed to a superset of the events Ashigaru acts on (issues opened, labeled, reopened; issue comments; Gatehouse `workflow_run`; `pull_request_target` opened, synchronize, closed; `workflow_dispatch`). The router picks triage, code, fix or release with the same gates the caller carried, and anything else ends in a `Nothing to do` job with no permissions and no secrets. Ashigaru gaining a job no longer reaches nobody until every repository re-copies its caller, which is how 2.13.1's `release` job missed the fleet (a167). Re-copy `examples/ashigaru.yml` once, after 2.14.0 is out: before `v2` carries `router.yml` a re-copied caller fails to start. An old caller keeps working as before. Each ignored event (a comment, a push) costs one short runner job. The concurrency group now carries the event name, so a burst of ignored comments or pushes cannot replace a routed run that is waiting its turn.

## [2.13.1] - 2026-11-17

### Fixed

- A release pull request is published the moment it merges, not at the next hourly sweep. Only the sweep published, so in auto mode the release pull request merged itself minutes after the sweep opened it and the tag waited up to an hour. Gatehouse 0.26.0 merged at 09:07 and was tagged by hand at 09:35 (developer 1.14.0 the same), and in between every Gatehouse review failed because master already pinned the 0.26.0 image (#166). The caller now runs a new reusable `release.yml` on `pull_request_target: closed`; it tags the merge commit with the app token, so `container.yml` and `publish.yml` start. Re-copy `examples/ashigaru.yml` to get it; until then the sweep still publishes.

## [2.13.0] - 2026-11-12

### Changed

- The code agent can report a fix as `partial`. Its commit and pull request then say `Refs #N` instead of `Closes #N`, and the issue is labeled needs-human with what is left, so a merge no longer closes an issue the fix itself says is unresolved (mcp-systemd#59).

### Fixed

- Ashigaru formats the Python files a patch touched with the repository's ruff (pinned to python-sdlc's 0.17.1) before it commits, in both the first publish and fix rounds. It commits on a runner where no hook runs, and CI's `sdlc ci` has no ruff step, so constitution#130 merged unformatted.

## [2.12.0] - 2026-11-10

### Changed

- The reusable workflows no longer declare or pass the `ASHIGARU_APP_ID` secret. It was dead plumbing: every token is minted from the `ASHIGARU_CLIENT_ID` variable and the `ASHIGARU_APP_KEY` secret, and nothing read the App ID. `code.yml`, `review.yml` and `fix.yml` drop its `required: true` declaration, so a caller may stop passing it; passing it stays a harmless warning until the fleet is swept. `ASHIGARU_APP_KEY` is unchanged (RT#1555).

## [2.11.3] - 2026-11-08

### Fixed

- A fix run no longer goes red when its pull request merged while the findings step was waiting for checks. The step looks at the pull request again after the wait and, if it is merged or closed, reports `none` instead of letting the checkout fail on the deleted branch (#152).

## [2.11.2] - 2026-11-08

### Fixed

- A conflicted pull request keeps its fix rounds. The publish job rebuilt `ashigaru/issue-N` from the first patch alone, dropping every fix-round commit while keeping the description and round count, so the pull request described code it no longer held and arrived at needs-human with its rounds spent (acquacotta#222, #226; #147). It now rebases the branch's own commits onto the default branch first, and rebuilds from the fresh patch only when that conflicts. A rebuild rewrites the title and description to match and resets the round count.

## [2.11.1] - 2026-11-08

### Fixed

- A release GitHub refuses to tag says why in the record. The app cannot create a tag at a commit whose workflow files differ from the default branch's (that needs the `workflows` permission, which it does not hold, on purpose), so a release pull request that merged before a workflow change landed was listed as "the release step failed; the sweep log has the error" every hour (openclaw 1.1.3). It now names the tag and commit and says to publish it by hand.
- `VERSION` says 2.11.0. 2.10.0 through 2.11.0 were cut by hand without moving it, so the sweep's own release of this repository was refused: version 2.11.0 was stated in no tracked file.

- The code agent's hook run skips python-sdlc's `hooks-installed` check (`SDLC_SKIP=hooks-installed`). It asks whether a developer's clone has its git hooks, which a runner checkout never does, so it failed on every repository using python-sdlc ≥ 0.5. Both hook-failure passes went to the agent with nothing it could fix, and every pull request body ended with a note about a missing pre-push hook (acquacotta#221, #227).

## [2.11.0] - 2026-11-06

### Fixed

- The relock runs `uv lock --no-build` with a scrubbed environment. Resolution is not inert: `uv lock` builds the metadata of any requirement it cannot read statically — a source distribution, or a path or VCS requirement with `dynamic` fields — by calling that package's PEP 517 backend, which runs its `setup.py`. The patch being resolved is a model's work on text a stranger may have influenced, and the publish job holds the app token. `--no-build` makes uv refuse such a requirement instead of building it, and the lock command no longer inherits the token. A dependency with no wheel now hands over to a person, which is the right way round. Gatehouse raised this CRITICAL on #138, correctly, against a claim in #132 that resolution does not run build backends.

### Added

- A patch that changes `pyproject.toml` in a repository with a `uv.lock` gets the lockfile regenerated on the publish side (`uv lock` only, no agent shell) before the commit, in both the code and fix publish steps. A failed resolution, a missing `uv`, or a relock that changes any other file hands over instead of pushing. The code prompt tells the agent not to hand-edit the lockfile.

## [2.10.1] - 2026-11-06

### Fixed

- The publish job resolves its `uv` action again. 2.10.0 called `astral-sh/setup-uv@v10`, and that action publishes major alias tags only up to `v7` — `v10.3.3` exists, `v10` does not — so every `Code / Open pull request` job failed at "Unable to resolve action", which is the job that pushes the branch and opens the pull request. Now `@v7`, which is resolvable and is what the organization already calls elsewhere.

## [2.10.0] - 2026-11-06

### Added

- The fix agent collects a second kind of finding: unanswered inline comments, review bodies (including `changes-requested`) and pull-request comments from accounts with write access, plus comments added to the linked issue since the last round (as context). They get `fixed in <sha>` or a single question, never `not a bug`; a question sets `needs-human`. Accounts without write access, bots and Ashigaru itself are not findings.

- A repository with the topic `ashigaru-observe` is watch-only. Triage, code, fix, review, the sweep and release record what they would have done in one issue in that repository ("Ashigaru is watching", labeled `ashigaru-observe`) instead of labelling, commenting, pushing, opening a pull request or releasing. The inbox lists the records under `## Watching`, and the sweep closes the issue when the topic is removed.

### Fixed

- The fix agent is shown the lines around every failure marker in the whole failed-job log (`FAILED`, `error:`, `##[error]`, tracebacks and the like), with omitted lines counted inline, instead of only the last 150 lines. A log with no marker still gets its tail.

- A job the workflow author marked best effort no longer counts as a failure. `continue-on-error: true` still records the job's check run as `failure`; only the check suite concludes `success`, and the listings return that field as null. Both places that read it now ask for it: the fix agent's failed checks, and the red-branch sweep over the GraphQL rollup. A suite that cannot be read, a rollup with checks past the first page, and a `FAILURE` rollup with no contexts read all still count. `developer#7` was held open for a GHCR mirror push that cannot create an org package and had been made non-blocking on purpose, which is how a fleet learns to ignore its own alert.

- `uv` is on the publish runner of `code.yml` and `fix.yml`, pinned to the version inside the python-sdlc image so a regenerated lockfile and `uv sync --locked` use one resolver. The code agent still has no `Bash`: a relock belongs to the job that runs no model and already holds the write credential. Nothing relocks yet — this is the part of #132 that cannot be written from `scripts/`.

### Behaviour

- On existing enrolled repositories, with no opt-in, a maintainer's comments on an Ashigaru pull request are now answered by the fix agent in the next round. Previously they were ignored. Rounds still count against `max_rounds`.

## [2.9.0] - 2026-10-31

### Added

- A repository whose default branch is red gets one issue, labeled `ashigaru-red` and titled "Default branch is red", once the red head is more than an hour old. The sweep rewrites it when the failing checks change and closes it when the branch is green. openclaw was red for four days with only a line in the inbox to show for it.
- The inbox lists dependency-scan alerts (issues by `github-actions[bot]` labeled `security` and `dependencies`) open more than three days. The scan files them with the workflow token, which starts no workflow, and triage reads only what people wrote, so gatehouse#96 sat five days after its fix with nothing looking at it.

### Changed

- A release pull request with auto-merge on whose head has a failed check is listed in the inbox under "Pull requests Ashigaru handed over", and the releases record says "Blocked:" with the check named, where it said "Merges when its checks pass." mcp-metsuke#66 sat four days on one unanswered Gatehouse finding while the record said it would merge on its own.
- Failing checks are judged by the newest run of each check name, as a required check is, so a check that failed and passed on a later run is not reported as failing.

### Fixed

- A run that was still in flight when Ashigaru's own pull request merged no longer marks the already-closed issue `needs-human` ("fix no longer applies"); it does nothing.
- The documented lint-and-test command runs in `python:3.12` rather than `-slim`, which has no `git`, so four release tests no longer fail locally while passing in CI.
- The inbox and releases issues stay one issue each. A label listing that came back empty for an hour-old issue made the sweep open #114 and #115 beside #94 and #90, and every later run edited the newest, so #90 and #94 went stale under the weekend report. An empty listing is now asked again before an issue is created, the oldest open issue is the one edited, and any newer duplicate is closed pointing at it.

## [2.8.0] - 2026-10-20

### Added

- The inbox issue lists what Ashigaru fixed in the past seven days (issues labeled `ready-to-code` that closed as completed) and what it triaged in that time that is still open, so a fix and a verdict nobody acted on both reach the weekend report.
- The inbox issue has a last part, "Red on the default branch": each public repository whose default branch head has a failed or errored check rollup, with links to the failed checks, plus the constitution's `fleet-drift.yml` and `validate-cascade.yml` when their latest completed run failed. One paginated GraphQL query and two REST requests per sweep. It replaces the one check factory.crunchtools.com made that nothing else did. With nothing red it says "Nothing red."
- A fix commit and its pull request say "Reported by @login." when the issue came from someone without write access to the repository, so outside reporters get credit (mcp-trove#52 fixed mcp-trove#7 without naming its reporter). Bots and maintainers are not credited.

### Changed

- Only one sweep runs at a time. Kagetora now dispatches the sweep hourly from a Hermes monitor-mode job, because GitHub throttles the hourly schedule to every few hours; two overlapping sweeps would each start a full hour of fixes.
- The sweep has no GitHub `schedule` any more; Kagetora's Hermes job is its only trigger.

## [2.7.3] - 2026-10-16

### Fixed

- A fix round turns auto-merge back on after it pushes. GitHub turns auto-merge off when the branch is pushed, so every pull request that needed a fix round sat green and unmerged (mcp-gitlab#39).

## [2.7.2] - 2026-10-16

### Fixed

- The hourly sweep runs again. 2.7.1 checked `--retriage-before` as a date but left its default as an empty string, and argparse checks a string default too, so every scheduled run without the flag exited with "invalid cutoff value: ''".

## [2.7.1] - 2026-10-16

### Fixed

- Every triage verdict creates `ready-for-triage` and `ready-to-code` before its comment tells the reader to add one. Labels were created only on first use, so 43 of 62 enrolled repositories lacked `ready-for-triage` and 54 lacked `ready-to-code`, and the next step a triage comment gave failed with "label not found".
- Verdicts given before a triage fix can be revisited. A manual sweep run with `retriage_before` set to a date also queues issues Ashigaru labeled `triaged` or `needs-info` before it. Before, the label hid them from the sweep for good: mcp-pcloud#40, a bug labeled a feature before 2.7.0, never reached the code agent.

## [2.7.0] - 2026-10-16

### Changed

- Triage separates what an issue is from whether Ashigaru can fix it alone. The verdict has a new field, `needs_decision`, set when a maintainer must choose before code is written: several reasonable fixes, a change to an API, schema or visible behaviour, or production systems the agent cannot reach. Such an issue keeps its real category, is labeled `triaged`, and its comment says to answer the question and add `ready-to-code`. Before, the prompt told the agent to call anything involving a design choice a feature, so bugs were relabeled `enhancement` (rotv#770: a stale database flag on 612 places, filed as a bug, came back as "feature (60%)").
- A type label a person put on an issue wins. Ashigaru no longer adds `enhancement` next to a reporter's `bug`; it removes a type label it added itself when a person's disagrees, and replaces its own earlier label on re-triage.

## [2.6.0] - 2026-10-11

### Changed

- Repositories call the workflows at the major tag, `@v2`, and no longer at an exact release. Publishing a release runs the new `major-tag.yml`, which moves `v2` to it when it is the newest release of its major, and every enrolled repository runs that release from then on with no change of its own. Before, each release had to be re-pinned in every repository by a Dependabot pull request; three releases in one day left repositories one and two releases behind. Every release still has its own `vX.Y.Z` tag, so a release is undone by moving `v2` back. The reusable workflows fetch their scripts at `v2` as well. A repository on an exact pin keeps working until it copies the new caller file.
- Releasing Ashigaru no longer edits any pin: `VERSION` and the CHANGELOG are the whole release commit.

### Fixed

- A feature request with an open question was labeled `needs-info`, which took it off the list of work waiting for a maintainer's decision. After the fleet backlog was triaged, 44 of 48 `needs-info` issues were features. `needs_info` now holds back only bug-class work that Ashigaru would otherwise start; a feature is labeled `triaged` and its question stays in the summary.

## [2.5.0] - 2026-10-10

### Added

- Releases. A repository asks for them with a topic: `ashigaru-release-pr` or `ashigaru-release-auto`. Each hour the sweep finds enrolled repositories whose `CHANGELOG.md` has entries under `[Unreleased]`, no Ashigaru pull request open, and a default branch that has been quiet for 20 minutes. It reads the bump from the entries (`BREAKING` or `### Removed` is major; `### Added`, `### Changed` or `### Deprecated` is minor; anything else is patch) and opens `ashigaru/release-X.Y.Z`, which moves the entries under the new version and rewrites the old version on every line that states it. Once that pull request has merged, the next sweep tags `vX.Y.Z` and publishes the GitHub Release with the changelog entry as its notes. With `-pr` a maintainer merges the pull request; with `-auto` GitHub merges it when the required checks pass, unless the bump is above the repository's limit (`ashigaru-release-max-patch`, `-minor` or `-major`; minor by default). No model runs and no repository code runs. See [docs/release.md](docs/release.md).
- A releases record. The sweep keeps one issue in the Ashigaru repository, labeled `ashigaru-releases`: releases published in the last seven days across every enrolled repository, release pull requests that are open, repositories with merged work and no release (whatever their release mode), and repositories it could not release, with the reason.
- The inbox lists release pull requests that wait on a maintainer.

### Changed

- The fix agent does not run on `ashigaru/release-*` pull requests. A release pull request that fails its checks, or that stops merging, is a person's to look at; one that conflicts is closed and reopened from the current default branch.

## [2.4.0] - 2026-10-10

### Added

- The triage and code agents see the screenshots in an issue. Before the agent runs, `scripts/prompt.py` downloads the images from the issue body and from maintainers' comments to the runner and lists the files in the prompt; the agent opens them with `Read`. Until now an image reached the agent as a URL it had no way to open, so a report whose evidence was a screenshot came back `needs-info`. Up to five images of 5 MB each are kept, only from URLs GitHub serves, and only PNG, JPEG, GIF or WebP as judged by the bytes. An image in a comment from someone without write access is not downloaded. The agents get no new tool and no network.

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
