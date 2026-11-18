# Desk API contract

The Desk is the 3.x service (`docs/design/ashigaru-3.md`, "The Desk"). The CLI,
the MCP server, the Claude Code hooks and Josui are all clients of this API.
The machine contract is [`desk-api.json`](desk-api.json) (OpenAPI 3.1),
generated from the code by `ashigaru-desk openapi`; a test fails when the two
differ.

Stack: FastAPI over the standard library's `sqlite3`, because its pydantic
models are both the validation and the published OpenAPI, so the contract
cannot drift from what the service enforces.

## Auth

Every route but `/healthz` takes `Authorization: Bearer ash_<key_id>_<secret>`.
A key belongs to one identity. Only its SHA-256 digest is stored; the token is
returned once, by the mint call. Keys never travel in a URL or a query string.

| Role | Who | Reads | Writes |
|---|---|---|---|
| `admin` | Josui, Scott | everything | every item, seat, identity and key |
| `seat` | one worker; its identity name is its seat name | everything | open items whose `seat` is its name, claims, `turn`/`task` and usage on its own seat row, its own keys (revoke) |
| `ci` | a job | everything | its own keys (revoke) |

A seat may set only `state`, `result`, `next`, `learned`, `note`,
`blocked_reason`, `memorized` and `check_after`, and may move an item only to
`working`, `review` or `blocked`. Josui verifies and closes. Once an item is
closed (`done`, `wontdo`, `abandoned`) or its wrap is `cleared`, only an admin
may change it or file under it. A seat row's `lane`, `status`, `owner` (Scott's
manual hold) and `item` are admin-only.

The first admin key comes from the host, since every API call needs one:
`ashigaru-desk mint-key "Josui Prime" --role admin --db <file> --out <new key file>`,
which writes the token to a new file, mode 0600, and never prints it. If the
identity exists with another role it refuses, unless `--change-role` is given.

Every 4xx body is `{"detail": "..."}`; each route's contract lists the codes it
can return.

## Routes (all under `/v1` but `/healthz` and `/webhook/`)

| Route | Does |
|---|---|
| `GET /healthz` | 200 with the schema version; no key |
| `POST /webhook/{forge}` | a forge's webhook delivery (`/webhook/github`); its signature, not a key, authenticates it (see Webhooks) |
| `GET /me` | the caller's identity and role |
| `GET /items` | list, newest first; filters `state` (repeat it for any of several), `seat`, `lane`, `kind`, `mode`, `parent`, `subject_key` (normalised the same way), `ref`, `learned` (true: a Learned line other than `none` is recorded) and `memorized`; pages of `limit` (200, at most 1000) from `offset` |
| `POST /items` | admin: create; `id` defaults to the next `a<N>`, `state` to `queued`; 409 on a taken id or `subject_key`, whose detail names the item holding it (`subject_key RT#9 is taken by a3`). A `subject_key` that is a ref is stored as its key: a GitHub issue or PR URL or `owner/repo#N` becomes `owner/repo#N` in lowercase, an RT URL or `RT #N` becomes `RT#N`; any other key is stored as given. An admin may set `filed_by` |
| `GET /items/{id}` | one item |
| `PATCH /items/{id}` | the fields sent change; `null` clears; a `subject_key` is stored as `POST /items` stores it |
| `POST /items/{id}/report` | the report contract: `DONE` to `review` with `text` as the result, `BLOCKED` to `blocked` with `text` as the reason, `WORKING` to `working`, `CC-DONE` sets `memorized`; a `learned` of `none` is not recorded |
| `POST /items/{id}/file` | `desk_file`: a `proposed` child under the story's outcome; one per assignment (409 on the second); abandoned 14 days later unless Josui promotes it |
| `POST /items/{id}/assign` | admin: `{seat}` names one; `{}` or `{lane}` picks the seat in the lane (default: the item's) free longest and not held by Scott. 409 when the item is closed or waits on an `after` item not done, or the seat is busy, held or unknown |
| `POST /items/{id}/wrap` | admin, `wrap` in the CLI and MCP: `{stage: wrapping}` records that `/cc seat` was sent. `{stage: cleared}` (the default) is 409 until the item is memorized (CC-DONE) or `learned` is sent, and while its seat's turn is active or its seat never reported, unless `force`; then it clears the wrap and closes an open item in `state` (`done`, `wontdo`, `abandoned`), which frees the seat |
| `POST /items/{id}/claim` | `desk_claim`, seats only: take a `queued`, seatless, `auto` item in the seat's lane whose `after` items are done. Atomic; the loser of a race gets 409. The seat must be free, unheld and have a lane: a seat with no lane claims nothing, and an item with no lane is claimed by no seat (Josui sets its lane or assigns it) |
| `GET /items/{id}/events` | the item's history, ledger lines included, oldest first; pages of `limit` (200, at most 1000) after `after` (a seq) |
| `GET /ci/wait` | `desk_wait`: `repo` (owner/name), full `sha` and optional `branch` (default the repo's); the commit's CI result as soon as it is not `pending`, which needs every check that branch requires to have a conclusion (see CI below), else as it stands after `timeout` seconds (default and most 90; 0 reads now). A client waits again on `pending` |
| `GET /work` | `ashigaru work`, the whole check round: `items`, the check table (id, seat, mode, task, state, wrap, result or blocker, next) of open items, blocked first, then newest, with `?all=true` adding done, wontdo, abandoned and cleared ones, at most 1000 (`truncated`); `seats` as `GET /seats`; `summary` (`up`: seats not missing, `seats`, `open` items); `queue` as `GET /queue`; and `catch_up`, the issues and PRs intake recorded from the org webhook after a cursor: `new`, first seen, with title, `filed_by` (the author), lane, mode and `held`, and `reopened`, the delivery that took the source from closed to open while the item is closed, each item once. The cursor is an event seq (`after`): the `sweep` mark, or with `?since=` (or a mark that is a time) the event before the first stamped then; with neither, a day back; 422 for a time that is not one. `until` is the last event covered: the last committed when the round began, or the 1000th read when `truncated`. Events are numbered in commit order, so nothing is lost or repeated between rounds. It leaves the mark alone, though like `GET /queue` it abandons proposed children past their expiry |
| `POST /work` | admin: the same round, then the `sweep` mark becomes its `until` (`advanced` is true), so the next round reads on from there. Only forward: when it is already there, or a concurrent round moved it further, it stays and `advanced` is false. `?since=` is 422 here, since a round read from a time could move the mark past events it never read |
| `GET /check` | `items` of `GET /work` alone, for clients older than 3.5 |
| `GET /marks/{name}` | a named watermark (`sweep`: the event `GET /work`'s catch-up starts after; a time is read too): value, updated_at, updated_by; 404 until first set |
| `PUT /marks/{name}` | admin: `{value}` sets it; names are `[a-z][a-z0-9-]*` |
| `GET /queue` | `ashigaru queue`: the newest 1000 `queued` items, each with `waits_on` (every `after` id and its state, `unknown` for no such item), and the newest 1000 `proposed` children; `truncated` is true when either list was cut |
| `GET /seats`, `GET /seats/{name}` | seat state: free/busy, owner, `hold` (`free`, `busy-auto` or `busy-manual`), turn, task, item, since, and the last usage report. `missing` is true for a seat whose row has not changed in 24 hours; the list ends with each seat identity that never checked in, `missing` with a null `updated_at` |
| `PATCH /seats/{name}` | created on first call; a seat sends `turn` and `task`. `since` is server-set when `status` changes, `turn_started_at` when the turn goes active. The Desk infers Scott's hold (below) |
| `POST /seats/{name}/usage` | the seat or an admin: `session_id`, `model`, `billing` (max / vertex / openrouter; else the seat's lane's, else what the row had; from a seat key the lane's comes first), `context_used`, `context_window`, `session_tokens`, `cost_usd` (the session's running total; what of it is not yet booked goes to today's spend, held until billing is known) |
| `GET /usage` | `ashigaru usage`: per seat model, context, session tokens and cost, turn and seconds in it; spend by billing source today and this month (UTC) with limits |
| `PUT /billing/{billing}` | admin: `daily_usd`, `monthly_usd` limits |
| `GET /outcomes` | `ashigaru outcomes`: each outcome item with its stories' total, done, blocked and open counts |
| `GET`, `POST /identities` | admin |
| `GET /identities/{name}/keys` | admin or the owner; metadata only |
| `POST /identities/{name}/keys` | admin: mint; the token is in this response only |
| `DELETE /identities/{name}/keys/{key_id}` | admin or the owner: revoke, effective on the next request |
| `GET /identities/{name}/mcp` | admin or the owner: the identity's MCP codes, metadata only |
| `POST /identities/{name}/mcp` | admin: a new hosted MCP URL, `https://<host>/mcp/<code>`, in this response only |
| `DELETE /identities/{name}/mcp/{code_id}` | admin or the owner: revoke, effective on the next MCP request |

## Hosted MCP

The service serves MCP (Streamable HTTP, stateless) at `/mcp/<code>`. The code
is `<code_id>_<secret>`, minted like a key and stored the same way, as a
digest compared timing-safe; it names the one identity calling, and nothing
in a tool's arguments does. An unknown, revoked or disabled code is a 404. The
service's logs (uvicorn's access log, and its ASGI scope at TRACE) record
`/mcp/<code>` in its place, the code is gone from the scope before MCP sees the
request, and no path under a code answers with a redirect. A proxy in front must
redact the path the same way.

The tools are the shared command set: each `ashigaru` command but `mcp` and
`hook` is the tool `desk_<command>` with the command's arguments, from
`desk_usage` to `desk_file` (`tests/test_skill.py` fails when they drift).
`desk_wait` takes an `item_id`, polling it until it settles, or a `repo` and
`sha` (the `GET /ci/wait` long-poll), and returns `{"result": ...}`. Each calls the REST route's own
function with the code's identity, so authority is the API's: a refusal comes
back as a tool error carrying the same status and detail.

## Scott's hold

The Desk infers it from a seat's own reports, so no seat can claim or drop one.
A seat holding no item that reports `turn: active` with a `task` (its
session's first typed prompt) becomes busy, `owner: Scott`, holding a new
`manual` item in `working` with that task and the seat's lane. Turns without a
task (an assignment arriving, a background task's result) start nothing, and a
seat on an assigned item stays on it. A `/clear` (`task: null`) clears the held
item; a wrap or close of it does too; either frees the seat. The hold follows
the held item's mode: `auto` hands it to Josui, `manual` back to Scott.

## Item

States: `proposed`, `queued`, `assigned`, `working`, `review`, `done`,
`blocked`, `wontdo`, `abandoned`. Wrap-up is separate, in `wrap`: `wrapping`
when `/cc seat` has been sent, `cleared` once the seat is cleared. Setting a
state stamps `assigned_at`, `queued_at` or `closed_at`; `wrap: cleared` stamps
`cleared_at`. Closing an item, clearing it, or moving it to another seat frees
the seat that held it.
Times are UTC, `YYYY-MM-DDTHH:MM:SSZ`.

## Webhooks

Each forge posts to `POST /webhook/<forge>` (`/webhook/github`), the only path
Cloudflare and the proxy let in from the internet. That forge's adapter checks
the signature against each of its configured secrets, timing-safe. None
verifies, or the forge has no adapter or no secret: 401, `{"detail":
"unverified delivery"}`, nothing echoed and nothing stored. A verified delivery
the adapter cannot read is 400; one over 2 MiB is 413. The answer is
`{"duplicate", "handled"}` and nothing else. A delivery is remembered for 7
days by a SHA-256 of the forge, the event and the signed body, never by the
delivery id, which the signature does not cover. It is recorded in the
transaction that applies it, so a replay or redelivery changes nothing and a
delivery that failed is applied whole when the forge retries.

Adapters live in `src/ashigaru/desk/forges/`, and nothing outside them names a
forge. GitHub's: `X-Hub-Signature-256`, `X-GitHub-Event`, content type JSON;
subscribe the org webhook to Workflow runs, Check suites, Issues and Pull
requests. Its permission checks call the collaborator permission API with
`ASHIGARU_FORGE_TOKEN_GITHUB`, which needs read access to the repos'
collaborators, metadata (rulesets, branches), checks and commit statuses, and
nothing more. GitHub never calls an app's bot a writer, so
the accounts in `ASHIGARU_FORGE_AUTHORS_GITHUB` (the image sets
`ashigaru-crunchtools[bot]`) are trusted as authors of their own issues and
PRs, so listing an account makes its own subjects auto-claimable: a `lane:*`
label on one sets its lane and mode `auto`, as for any writer's. Their lane
labels on anyone else's subject approve nothing. The record issues the sweep
keeps (labeled `ashigaru-inbox`, `ashigaru-releases`, `ashigaru-red` or
`ashigaru-observe`) are
never queued, and one queued before is held on its next delivery.

**CI.** One result per (repo, commit): each check's name and verdict
(`pending`, `success`, `failure`, `cancelled`, `skipped`, `neutral`), and a
status, `pending` while any check is or none has reported, else `failure` when
any check failed or was cancelled, else `success`. A forge conclusion the
adapter does not know counts as `failure`. A newer run of a check (a re-run)
always replaces an older one, which never comes back; within one run the newest
forge time wins, so a late `in_progress` never undoes a `completed`. A commit's
checks are kept 30 days after their last delivery. GitHub: each workflow on each triggering event is a check
(`CI (push)`, `CI (pull_request)`), from `workflow_run`; another App's
`check_suite` completed is a check named for the App (an org webhook gets no
`requested` for those, so they appear once they finish); the Actions App's
suites are left to `workflow_run`.

**Required checks.** A branch requires jobs (`sdlc ci`) where the Desk's
checks are workflows, and a workflow the forge never started sends no delivery
at all. So before a commit is `success` the Desk asks the forge which checks
the target branch requires and what each has reported on the commit: one with
no conclusion yet (never started counts) keeps it `pending` and is named in
`pending`; one that failed makes it `failure`. When the commit is still
`pending` at the end of a wait, the required ones are named too. The result
carries `branch` and `required`; when the forge cannot say (no token, an
error), `unverified` says why and the status is never `success`. GitHub: the
union of `GET /repos/{repo}/rules/branches/{branch}` (repo and org rulesets)
and the branch's classic protection, held 60 seconds; each matched against the
commit's check runs and commit statuses, read fresh, by exact name (`CI / sdlc
ci` is not `sdlc ci`). A check required from an App (the ruleset's
`integration_id`, protection's `app_id`) counts only that App's check runs, as
GitHub's merge gate does; without one, any run or status of that name counts.

**Intake.** An issue or PR becomes an item keyed `owner/repo#N`, lower case as
GitHub's names are case-insensitive (`crunchtools/ashigaru#190`): an issue a
queued `outcome`, a PR a queued `story`. Its task is the Desk's own words
(`Triage owner/name#N (from github; read it at the ref)`, `Review ...` for a
PR), never the source's title, which is data under `extra`. An item is
claimable (mode `auto`) only when the forge says its **author** may write to the
repo (GitHub: write, maintain or admin), with a `lane:<lane>` label as its lane,
or once a maintainer approves it: a `labeled` event adding a `lane:<lane>`
label, from a sender who may write. Anything else (an outsider, a bot, a
failed check, a missing token, or a writer editing, reopening or commenting on
an outsider's issue) leaves it held, no lane and mode `manual`, for Josui.
While it is held, a delivery at the head it was held at (`held_head`, for a PR)
asks about the author again, so a check that failed is retried; a writer's push
from `held_head` moves it, and a delivery at any other head, or with none, does
not retry: a maintainer's label is needed. A PR held before heads were recorded
has none, so only a label trusts it. An approval is recorded under `extra` as `approved_by`,
`approved_at`, `approved_lane` and, for a PR, `approved_head`: approval is tied
to that commit. A PR trusted because its author writes is tied to its head the
same way, as `approved_head`, and once withdrawn only an approval restores it. It is withdrawn (held again, `trusted` false, with `held_by`,
`held_at` and `held_for`: `lane removed` or `head moved`) when a writer removes
the approved `lane:` label, or when any delivery, a writer's included, carries a
head other than `approved_head` (a PR approved before heads were recorded has
none, so it is held on its next delivery). The one exception is a writer's push
whose `before` is `approved_head`: that moves `approved_head` to the new head.
A lost or late delivery therefore errs toward holding. An outsider editing the
title or body withdraws nothing: seats read it as data. Deliveries are judged
against the row as it was before the transaction that applies them; if the row
changed in between, whatever the judging granted (an approval, a writer's
author, a writer's push) is dropped, and a maintainer labels it again. A
writer's lane removal whose permission check fails is lost, since its delivery
is already recorded: Josui holds the item by hand, or the writer re-adds and
removes the label. Approval, retry and
withdrawal set lane and mode only while the item is queued, seatless, and
nobody but intake has set its lane or mode; once Josui has (a manual hold, a
lane), Josui's choice wins and the change is recorded only. `extra` also
records the `author` and whether the item is `trusted`. Later deliveries refresh `ref` and, under
`extra`, the source's `title`, `labels` and `source_state` (open or closed);
nothing else changes task, lane, mode, state or seat. A subject first seen
closed is not queued.

## Ledger import

`ashigaru-desk import <ledger.jsonl>` folds Josui's agents ledger in, in one
transaction. A ledger line is a patch, so the newest value of each field wins,
and every line is also kept as an event. A line already imported (same number,
same hash) is skipped, so a re-run changes nothing and a grown ledger applies
only its new lines. `assigned`, `queued`, `blocker`, `wrapped`, `cleared`,
`closed` and the old `wrapping` state map onto columns; anything else, and any
value outside the Desk's enums, is kept in `extra`.

## Deploy

The service ships two ways, both built on GitHub:

- **Image:** `quay.io/crunchtools/ashigaru` (`Containerfile`, `.github/workflows/container.yml`).
  `:main` follows the default branch; `:latest` and `:X.Y.Z` the 3.x release tags.
  It runs as uid 65532 with a read-only root; the SQLite file is on the `/data`
  volume. Its HEALTHCHECK is `ashigaru-desk health`.
- **RPMs:** `ashigaru` (the CLI) and `ashigaru-desk` (the service), for Fedora
  and EL10, attached to each 3.x GitHub release (`.github/workflows/rpm.yml`).
  The RPM Desk has no hosted MCP: fastmcp is the image's `mcp` extra, so there
  `/mcp` is not served and minting an MCP code is a 409.

Settings are flags on `ashigaru-desk serve`, else these environment variables:

| Variable | Means | Default (image) |
|---|---|---|
| `ASHIGARU_DESK_DB` | the SQLite file | none (`/data/desk.db`) |
| `ASHIGARU_BIND` | the address to listen on | `127.0.0.1` (`0.0.0.0`) |
| `ASHIGARU_PORT` | the port | `8080` |
| `ASHIGARU_TRUSTED_PROXIES` | comma-separated addresses or CIDRs of the reverse proxies in front | none |
| `ASHIGARU_WEBHOOK_SECRET_GITHUB` | GitHub's webhook secret; each forge has its own `ASHIGARU_WEBHOOK_SECRET_<FORGE>` | none: its deliveries are refused |
| `ASHIGARU_WEBHOOK_SECRET_GITHUB_FILE` | a file of GitHub's secrets, one per line, all accepted while one rotates | none |
| `ASHIGARU_FORGE_TOKEN_GITHUB` | a GitHub token that can read collaborator permissions, rulesets, branches, checks and commit statuses; each forge has its own `ASHIGARU_FORGE_TOKEN_<FORGE>` | none: all intake is held for Josui, and no commit's CI is `success` |
| `ASHIGARU_FORGE_TOKEN_GITHUB_FILE` | a file holding that token | none |
| `ASHIGARU_FORGE_AUTHORS_GITHUB` | comma-separated accounts, such as the app's bot, whose own issues and PRs are trusted as a writer's, so a lane label on one makes it auto-claimable; each forge has its own `ASHIGARU_FORGE_AUTHORS_<FORGE>` | none; the image sets `ashigaru-crunchtools[bot]` |
| `ASHIGARU_WEBHOOK_SECRET` | GitHub's secret under the bare name the first deploy used; added to `_GITHUB` | none |
| `ASHIGARU_WEBHOOK_SECRET_FILE` | the same, as a file | none |

A webhook secret is read from the environment or its file at start, never from
the database, a flag or a log; mount the file as a podman secret.

Behind Cloudflare and Gloval's proxy, set `ASHIGARU_TRUSTED_PROXIES` to the
address the proxy connects from. The client address and scheme then come from
`X-Forwarded-For` and `X-Forwarded-Proto`, but only on requests from those
addresses; a client that connects directly cannot forge either.

First key, on the host:
`podman exec ashigaru ashigaru-desk mint-key "Josui Prime" --role admin --db /data/desk.db --out /data/josui.key`,
then move the key file off the volume into the client's secret store.
