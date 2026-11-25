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
may change it or file under it. A seat row's `lane` is admin-only. Its `status`,
`owner` and `item` are derived from the seat's open item, so no one sets them
(see Seats).

The first admin key comes from the host, since every API call needs one:
`ashigaru-desk mint-key "Josui Prime" --role admin --db <file> --out <new key file>`,
which writes the token to a new file, mode 0600, and never prints it. If the
identity exists with another role it refuses, unless `--change-role` is given.

Every 4xx body is `{"detail": "..."}`; each route's contract lists the codes it
can return. A 409 or 412 body also carries a `code`: `stale-session`, `busy`,
`held`, `unseen`, `no-lane`, `missing`, `derived`, `not-ready` (a wrap before
CC-DONE or mid-turn) or `version`.

A seat's requests carry `X-Ashigaru-Session`, the Claude Code session id its
hook was handed or its shell sees as `CLAUDE_CODE_SESSION_ID`. One that names
another session than the seat's current one, or a session the Desk has ended,
is 409 `stale-session` and changes nothing, its body's `session` naming the
seat's current session (null once the Desk ended it); the request is in that
session's epoch. A 5.0 client sends `X-Ashigaru-Epoch`, the epoch its session registered,
instead or as well, and is held to it. A REST request naming neither is
accepted only from a seat that never registered a session (epoch 0, a 4.x
client); MCP tool calls name neither and are not fenced.

Writes to an item take `If-Match: <version>`, where `version` is the item's last
event's seq. When the item has moved since, the write is 412 `version` and
changes nothing. Without the header the write applies as before.

## Routes (all under `/v1` but `/healthz` and `/webhook/`)

| Route | Does |
|---|---|
| `GET /healthz` | 200 with the schema version; no key |
| `POST /webhook/{forge}` | a forge's webhook delivery (`/webhook/github`); its signature, not a key, authenticates it (see Webhooks) |
| `GET /me` | the caller's identity and role |
| `GET /items` | list, newest first; filters `state` (repeat it for any of several), `seat`, `lane`, `kind`, `mode`, `parent`, `subject_key` (normalised the same way), `ref`, `learned` (true: a Learned line other than `none` is recorded) and `memorized`; pages of `limit` (200, at most 1000) from `offset` |
| `POST /items` | admin: create; `id` defaults to the next `a<N>`, `state` to `queued`; 409 on a taken id or `subject_key`, whose detail names the item holding it (`subject_key RT#9 is taken by a3`). A `subject_key` that is a ref is stored as its key: a GitHub issue or PR URL or `owner/repo#N` becomes `owner/repo#N` in lowercase, an RT URL or `RT #N` becomes `RT#N`; any other key is stored as given. An admin may set `filed_by`, and `pr`, the PR carrying a story's work as `owner/repo#N` (a seat may set it on its own item); `landed_at` is server-set when that subject merges. `outcome` (an outcome's id, or an issue or RT ref: the item of kind outcome for it, recorded if none, never a story holding the key; a PR's ref is 422) or `outcome_text` (with or without `outcome` as its short name: a new one-off outcome keeping the instruction as its note; blank is 422) sets `parent`; an outcome neither found nor given text is 422. `assign` (`{seat}`, `{lane}` or `{}`, as `POST /items/{id}/assign`) records and assigns in one transaction: the item is never seen `queued`, and a refused assign (409) records nothing |
| `GET /items/{id}` | one item |
| `PATCH /items/{id}` | the fields sent change; `null` clears; a `subject_key` is stored as `POST /items` stores it; `outcome`/`outcome_text` as `POST /items`; `pr` is settable by an admin and by the item's seat; takes `If-Match`. A `seat` moves the item: the new seat must hold no other open item (409 `busy`) and must have reported (409 `unseen`), and the old seat is freed; moving to `queued` or `proposed` drops the seat, and a seat given to a queued item makes it `assigned`. A seat's write to an item that another session took (its `epoch`) is refused |
| `POST /items/{id}/report` | the report contract: `DONE` to `review` with `text` as the result, `BLOCKED` to `blocked` with `text` as the reason, `WORKING` to `working`, `CC-DONE` sets `memorized`; a `learned` of `none` is not recorded; takes `If-Match` |
| `POST /items/{id}/file` | `desk_file`: a `proposed` child under the story's outcome; one per assignment (409 on the second); abandoned 14 days later unless Josui promotes it; takes `If-Match` |
| `POST /items/{id}/assign` | admin: `{seat}` names one; `{}` or `{lane}` picks the seat in the lane (default: the item's) free longest and not held by Scott. 409 when the item is closed or waits on an `after` item not done, or the seat is busy (`busy`), held (`held`), missing (`missing`) or unknown; the pick skips a missing, held, busy or mid-turn seat. 409 `taken` when another seat holds the item, unless `If-Match` names the version the caller read: then the item moves, and if it was `assigned` or `working` the losing seat's session is ended (next epoch, session and lease cleared), so that seat's next request is 409 `stale-session`. No version takes Scott's item while a seat holds it (409 `held`) or an item being wrapped (409 `wrapping`). Takes `If-Match` |
| `POST /items/{id}/wrap` | admin, `wrap` in the CLI and MCP: `{stage: wrapping}` records that `/cc seat` was sent. `{stage: cleared}` (the default) is 409 until the item is memorized (CC-DONE) or `learned` is sent, and while its seat's turn is active or its seat never reported, unless `force`; then it clears the wrap and closes an open item in `state` (`done`, `wontdo`, `abandoned`), which frees the seat; takes `If-Match` |
| `POST /items/{id}/claim` | `desk_claim`, seats only: take a `queued`, seatless, `auto` item in the seat's lane whose `after` items are done. Atomic; the loser of a race gets 409. The seat must be free, unheld and have a lane: a seat with no lane claims nothing, and an item with no lane is claimed by no seat (Josui sets its lane or assigns it). Takes `If-Match` |
| `GET /items/{id}/events` | the item's history, ledger lines included, oldest first; pages of `limit` (200, at most 1000) after `after` (a seq) |
| `GET /ci/wait` | `desk_wait`: `repo` (owner/name), full `sha` and optional `branch` (default the repo's); the commit's CI result as soon as it is not `pending`, which needs every check that branch requires to have a conclusion (see CI below), else as it stands after `timeout` seconds (default and most 90; 0 reads now). A client waits again on `pending` |
| `GET /work` | `ashigaru work`, the whole check round: `items`, the check table (id, seat, mode, task, state, wrap, result or blocker, next, `since` the state was entered, `outcome` and its `outcome_label`, `step`: the one next step, and `needs_you`) of open items, never outcomes (an auto outcome closes once its stories all have), grouped by outcome: groups holding `needs_you` work first, then by newest, then work serving none; an item's outcome is its parent, else that of the story whose `pr` is its subject (a review), else that of the story it was filed under; with `?all=true` adding done, wontdo, abandoned and cleared ones, at most 1000 (`truncated`); `seats` as `GET /seats`; `summary` (`up`: seats not missing, `seats`, `open` items); `queue` as `GET /queue`; and `catch_up`, the issues and PRs intake recorded from the org webhook after a cursor: `new`, first seen, with title, `filed_by` (the author), lane, mode and `held`, and `reopened`, the delivery that took the source from closed to open while the item is closed, each item once. The cursor is an event seq (`after`): the `sweep` mark, or with `?since=` (or a mark that is a time) the event before the first stamped then; with neither, a day back; 422 for a time that is not one. `until` is the last event covered: the last committed when the round began, or the 1000th read when `truncated`. Events are numbered in commit order, so nothing is lost or repeated between rounds. It leaves the mark alone, though like `GET /queue` it abandons proposed children past their expiry |
| `POST /work` | admin: the same round, then the `sweep` mark becomes its `until` (`advanced` is true), so the next round reads on from there. Only forward: when it is already there, or a concurrent round moved it further, it stays and `advanced` is false. `?since=` is 422 here, since a round read from a time could move the mark past events it never read |
| `GET /status` | `ashigaru status` (`desk_status`), the `Board`: `busy` seats (seat, mode, item, `since`, last check-in, whole task, `outcome_label`), `free` and `missing` seat names, `summary` (`up`, `seats`, `open`), `needs_you`: manual seats idle with their `waiting_on`, Scott's holds on seats silent for a day (`stale`), Scott's blocked manual items, blocked items no seat holds (`blocked`: cleared before the wrap), a writer's PR that names no item while a seat holds an open pr-less item in its repo (`unlinked`), subjects intake holds for approval, and `outsider` subjects the sweep listed in the last day without storing them (at most 50); and `sweep`, the last run, for the board's last line; and `changes`, the caller's `GET /changes`, read without moving its mark |
| `GET /changes?since=` | `ashigaru changes` (`desk_changes`), the `Changes`: each item an event touched after the caller's own `seen:<identity>` mark (else the last day, or from `since`), written by anyone but the caller (an item whose writes set only `updated_at`, `version` or `extra` is left out), once, last changed first: `new`, the `fields` its writes set, `by` (writers in order), `writes`, `at`, its outcome and `step` (NEXT); `seen_at`, `after`, `until` and `truncated` (at 1000 events) as in the catch-up. It leaves the mark alone |
| `POST /changes` | `ashigaru changes --ack`: the same look, then the caller's seen mark moves to its `until`, only forward; `acked` is false when it was already there. Any identity, each its own mark |
| `GET /sweep` | `ashigaru sweep` (`desk_sweep`), anyone: `configured`, `every` (seconds), `running`, and `last`, the latest run with `trigger`, `started_at`, `finished_at`, `subjects`, `new`, `closed` and `error` |
| `POST /sweep` | admin, `ashigaru sweep --run` (`desk_sweep` with `run`): wakes the timer and returns at once; 409 when the sweep is not configured |
| `GET /subjects/{key}` | a `Subject`: the forge's own state of one issue or PR (`owner/repo#N`, `#` URL-encoded as `%23`): open, closed or merged, head, draft, review, title, labels, source and `source_updated_at`; 404 if no delivery has named it (migration 8) |
| `GET /check` | `items` of `GET /work` alone, for clients older than 3.5 |
| `GET /marks/{name}` | a named watermark (`sweep`: the event `GET /work`'s catch-up starts after; a time is read too): value, updated_at, updated_by; 404 until first set |
| `PUT /marks/{name}` | admin: `{value}` sets it; names are `[a-z][a-z0-9-]*` |
| `GET /queue` | `ashigaru queue`: the newest 1000 `queued` items, each with `waits_on` (every `after` id and its state, `unknown` for no such item), and the newest 1000 `proposed` children; `truncated` is true when either list was cut |
| `GET /seats`, `GET /seats/{name}` | seat state: free/busy, owner, `hold` (`free`, `busy-auto` or `busy-manual`), turn, task, item, since, and the last usage report. `missing` is true for a seat whose lease is not live, with its `epoch` and `lease_until`; the list ends with each seat identity that never checked in, `missing` with a null `updated_at` |
| `PATCH /seats/{name}` | created on first call; a seat sends `turn`, `task` and `waiting_on` (what it last asked before going idle; the `stop` hook sets it, best effort, a new turn or `/clear` clears it; migration 7 adds the column). Only an admin and the seat itself read `waiting_on`: every other caller gets null from `GET /seats`, `GET /seats/{name}` and `GET /work`, and no `waiting` rows in `GET /status`. `since` is server-set when `status` changes, `turn_started_at` when the turn goes active. The Desk infers Scott's hold (below). Any authenticated request from the identity a seat is named for renews its lease, Josui Prime's CLI calls as much as a worker's hooks. An admin PATCH that would change `status`, `item` or `owner` is 409 `derived`; `lane` stays Josui's |
| `POST /seats/{name}/session` | the seat or an admin: `{session_id, source}` (`startup`, `resume` or `clear`) returns `{epoch, seat}`. `startup` and `clear` take the next epoch and end the previous session (see Seats). `resume` keeps the epoch and the session's work for the seat's current session, however long it was idle, unless the Desk ended it, which is 409 `stale-session`; a seat that never registered a session (epoch 0) adopts the resuming one at epoch 1, its work untouched |
| `POST /seats/{name}/next?wait=S` | the seat or an admin, for `ashigaru hook next`: when the `autodispatch` mark is `on`, claim the oldest queued auto item in the seat's lane, not being wrapped, whose `after` items are done, as `desk_claim` would, and return `{item, brief}` (the brief: the task, then `done_when`, `ref`, `repo` and `note` when set, then the standing limits a hand brief ends with: no merge, tag, release or deploy, no messaging other seats, the report contract, /cc seat, Gatehouse replies); waits up to `wait` seconds (at most 90) for one, then 204. Claims only while the seat's turn is idle, and never once the client has gone. 204 also while the switch is off, and while the seat is busy only with an item at wrap stage `wrapping` (the /clear that starts the hook comes before `wrap` closes it); 409 when the seat is otherwise busy (`busy`), held (`held`), unseen (`unseen`) or has no lane (`no-lane`), or the epoch is stale. An item handed to a seat that is still `assigned` after 10 minutes (`ACK_AFTER`) goes back to the queue; an item Josui assigned is never handed back |
| `POST /seats/{name}/usage` | the seat or an admin: `session_id`, `model`, `billing` (max / vertex / openrouter; else the seat's lane's, else what the row had; from a seat key the lane's comes first), `context_used`, `context_window`, `session_tokens`, `cost_usd` (the session's running total; what of it is not yet booked goes to today's spend, held until billing is known). Once the seat has registered a session, a report for another is 409 `stale-session` from anyone and never moves the seat's `session_id`; a seat's own must name in its body the session its header does |
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
A seat's own `task` is set once a session: a seat that already has one
reporting another is reporting only its turn, and `startup` and `clear` drop
it. A seat holding no item that reports `turn: active` with a `task` (its
session's first typed prompt) becomes busy, `owner: Scott`, holding a new
`manual` item in `working` with that task and the seat's lane, unless an open
`manual` item already names the seat (imported, or left `blocked`): it takes
that one back in whatever state it is. Turns without a
task (an assignment arriving, a background task's result) start nothing. A turn
that starts on Josui's item still `assigned` moves it to `working`; when that
turn carries a task, Scott's first typed prompt, the unstarted item goes back to
the queue and the seat becomes his, with a new `manual` item. A `/clear` (`task: null`) clears the held
item; a wrap or close of it does too; either frees the seat. The hold follows
the held item's mode: `auto` hands it to Josui, `manual` back to Scott.

## Seats

A seat holds at most one open item (a unique index), and its row's `status`,
`item` and `owner` are recomputed from that item on every item write. Assigning,
claiming, updating or wrapping the item is the only way to change them.

A session registers with `POST /seats/{name}/session` and gets an epoch; the
items it takes record it. `startup` and `clear` take the next epoch and end the
previous session. Josui's item the session held leaves the seat: `blocked` ("...
was cleared before the wrap: its context is gone") when it was `assigned` or
`working`, else its state as it stands; a `wrapping` item stays for `wrap`. A
manual item of Scott's is wrap-cleared on `clear` and left open and seatless on
`startup`.

A lease is `lease_until`, renewed by any authenticated request from the identity
the seat is named for, whatever it asks (an admin's key too, so Josui Prime's
CLI calls keep it up): `LEASE` (10 minutes) while idle, `LEASE_ACTIVE` (60
minutes) mid-turn. A request from a session the Desk has ended renews nothing,
nor does any request once the Desk has ended the seat's session; a seat's reads
write the renewal at most once a minute. A seat is
`missing` when its lease is not live: never picked or assigned, its session and
work kept. Seat reconcile ends the session of a seat whose lease ran out
`STALE_SEAT` (24 hours) ago (the epoch moves on, and the session id and the
lease clear): Josui's
`assigned` and `working` items go back to `queued` with the note "seat lease
lapsed: its session ended", other `auto` items leave the seat as they stand, and
`wrapping` and `manual` items stay. It counts no silence from before the Desk
came up, so an outage lapses nothing. Migration 12 adds `seats.epoch`,
`seats.lease_until`, `items.epoch` and `subjects.unlinked_at`, drops the seat of
queued and proposed items, keeps on a seat that held several open items the one
its row named (else the newest) and takes the others off it with an event from
source `migration` (Josui's `assigned` or `working` items back to `queued`, any
other `blocked` with the reason, under NEEDS YOU), then recomputes every seat
row.

## Item

States: `proposed`, `queued`, `assigned`, `working`, `review`, `done`,
`blocked`, `wontdo`, `abandoned`. Wrap-up is separate, in `wrap`: `wrapping`
when `/cc seat` has been sent, `cleared` once the seat is cleared. Setting a
state stamps `assigned_at`, `queued_at` or `closed_at`; `wrap: cleared` stamps
`cleared_at`. `version` is the seq of the item's last event, `epoch` the session that took it.
Closing an item, clearing it, moving it to another seat, or moving it to
`queued` or `proposed` frees the seat that held it.
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
subscribe the org webhook to Workflow runs, Check suites, Issues, Pull
requests and Pull request reviews (`pull_request_review`: submitted or dismissed
sets the subject's review to approved, changes_requested or dismissed). Its permission checks call the collaborator permission API with
`ASHIGARU_FORGE_TOKEN_GITHUB`, which needs read access to the repos'
collaborators, metadata (rulesets, branches), checks and commit statuses, issues and pull requests, and the search API, and
nothing more. GitHub never calls an app's bot a writer, so
the accounts in `ASHIGARU_FORGE_AUTHORS_GITHUB` (the image sets
`ashigaru-crunchtools[bot]`) are trusted as authors of their own issues and
PRs, so listing an account makes its own subjects auto-claimable: a `lane:*`
label on one sets its lane and mode `auto`, as for any writer's. Their lane
labels on anyone else's subject approve nothing. The record issues the sweep
keeps (labeled `ashigaru-inbox`, `ashigaru-releases`, `ashigaru-red` or
`ashigaru-observe`) are
never queued, and one queued before is held on its next delivery. A subject
authored by a bot that is not in that list (Dependabot or Renovate, say) is not
queued when first seen, by webhook or sweep; it used to be held. The sweep queues
a Dependabot or Renovate PR that is still open a day on (see Sweep).

**Seat reconcile.** `serve` runs it at startup and every 5 minutes; it needs no
forge. It ends the session of a seat whose lease ran out (see Seats). A seat turn active longer than `ASHIGARU_TURN_MAX` (3 hours) whose row
has had no write for 10 minutes (the `statusline` hook posts every 30 seconds)
goes `idle` with `turn_reconciled_at` set: its `stop` hook never arrived. A
seat idle for a day with no hold of Scott's loses its `task`, as a `/clear`
would. Both update the row without moving `updated_at`, so a dead seat still
goes `missing`. It never closes an item: a hold of Scott's on a seat silent for
a day shows under NEEDS YOU as `stale` (migration 10 adds the column). A busy
seat with no write for `ASHIGARU_SILENT_AFTER` (an hour) gets `silent_since`
and shows as `silent`; any write clears it (migration 11).

**Sweep.** With `ASHIGARU_FORGE_TOKEN_GITHUB` and `ASHIGARU_FORGE_ORG_GITHUB`
set, `serve` runs a sweep at startup, every `ASHIGARU_SWEEP_EVERY` seconds and
on `POST /sweep`. A run re-reads every open item's subject and PR and settles
them as a delivery does; searches the org for subjects updated since the
`sweep-search` mark less 10 minutes (the search index lags), queuing only an
OWNER's, MEMBER's or trusted author's new subject (claimable when the forge says
its author writes to that repo, else held: org membership is not write access;
a PR links to an item only from such a writer) and listing anyone else's in
the run's `outsiders` (shown under NEEDS YOU for a day) without storing it,
except Dependabot's and Renovate's (`dependabot[bot]`, `renovate[bot]`): their PRs are recorded as subjects, re-read every run while
open, and one still open `ASHIGARU_DEPENDABOT_AFTER` seconds after it was
opened is queued, auto on exedore-max, to land or close (Renovate waits a week
after a release and automerges green minor and patch bumps, so one still open is
stuck: red CI, a major, or a conflict);
links a writer's PR as Linking says; then settles everything. The mark moves to the
run's start once the search was read whole, or to the last hit's time when the
forge's cap of 1000 cut it short, but never past a hit it could not judge (its
author's write access unknown), so the next run reads that hit again; a hit
deferred for over a day stops holding it. A run that cannot renew its lease stops at
once, without moving the mark. One
run at a time holds the `sweep-lock` mark as a 60-second lease it renews every
15 seconds, so a run killed mid-way frees it within a minute; a run that finds a
live lease writes a row with `skipped` saying whose, and the timer retries once
it could lapse. A subject it cannot read, or whose author's write access the
forge cannot say, is one error and the run goes on, and its row always finishes. It never writes to GitHub. Runs are rows of the
`sweeps` table (migration 9).

**Settling.** Each delivery records the subject; one older by `updated_at`
never overwrites, and merged is sticky. It then settles items whose
`subject_key` or `pr` names it. A seatless item nobody has started (`auto`, or
`manual` still `queued` or `proposed`) goes `done` ("merged at
<sha12>") when merged, `done` ("closed at the source") for a closed issue, or
`wontdo` ("closed unmerged at the source") for a closed-unmerged PR, with
`verified_by` desk. A seat-held item, or a `manual` one Scott has started, is
never closed, only given `landed_at` on merge. An item settling closed (by its events, not by
`verified_by`, which an admin can write) is queued again when its own subject
reopens and nothing else it names is closed or merged. A `pr` link acts only on
merge. `GET /work` and `GET /status` run the same pass as a fallback, as they do
proposed expiry; the pass matches only items it will change, so a read with
nothing due takes no write lock.

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

**Linking.** A seat's brief says to branch `fix/aN-<short-slug>` (or another
type prefix) and end the PR body with `Ashigaru-Item: aN`. Intake, by webhook
and by sweep alike, links a writer's PR to the open, pr-less item in its repo
that it names, by the trailer line, else a head branch `aN-` or `aN/` after one
type prefix (`fix/aN-...`) or none, else `(aN)` in the title or body, by setting
that item's `pr` and inserting no item. A PR an item already names in `pr` never
queues one again, and a stranger's PR never links. A writer's PR (not a bot's)
that names nothing while a seat holds an open pr-less item in its repo is not
queued: it is recorded as a subject with `unlinked_at` and listed under NEEDS
YOU as `unlinked` until an item names it (`ashigaru update aN --pr <url>`). The
app's own bot PRs queue as before.


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
moved in between, the delivery is rolled back and judged again, up to 3 times,
so an approval is not lost. A
writer's lane removal whose permission check fails is lost, since its delivery
is already recorded: Josui holds the item by hand, or the writer re-adds and
removes the label. Approval, retry and
withdrawal set lane and mode only while the item is queued, seatless, and
nobody but intake has set its lane or mode; once Josui has (a manual hold, a
lane), Josui's choice wins and the change is recorded only. `extra` also
records the `author` and whether the item is `trusted`. Later deliveries refresh `ref` and, under
`extra`, the source's `title`, `labels` and `source_state` (open or closed), unless
the delivery is older by `updated_at` than `extra.source_updated_at`;
nothing else changes task, lane, mode, state or seat. A subject first seen
closed is not queued.

## Ledger import

`ashigaru-desk import <ledger.jsonl>` folds Josui's agents ledger in, in one
transaction. A ledger line is a patch, so the newest value of each field wins,
and every line is also kept as an event. A seat stays on one open item: a later line that would seat a second leaves the name in `extra.ledger_seat`. A line already imported (same number,
same hash) is skipped, so a re-run changes nothing and a grown ledger applies
only its new lines. `assigned`, `queued`, `blocker`, `wrapped`, `cleared`,
`closed` and the old `wrapping` state map onto columns; anything else, and any
value outside the Desk's enums, is kept in `extra`.

## Deploy

The service ships two ways, both built on GitHub:

- **Image:** `quay.io/crunchtools/ashigaru` (`Containerfile`, built by ci.yml's `image` job).
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
| `ASHIGARU_FORGE_TOKEN_GITHUB` | a GitHub token that can read collaborator permissions, rulesets, branches, checks, commit statuses, issues and pull requests, and use the search API; each forge has its own `ASHIGARU_FORGE_TOKEN_<FORGE>` | none: all intake is held for Josui, and no commit's CI is `success` |
| `ASHIGARU_FORGE_TOKEN_GITHUB_FILE` | a file holding that token | none |
| `ASHIGARU_FORGE_AUTHORS_GITHUB` | comma-separated accounts, such as the app's bot, whose own issues and PRs are trusted as a writer's, so a lane label on one makes it auto-claimable; each forge has its own `ASHIGARU_FORGE_AUTHORS_<FORGE>` | none; the image sets `ashigaru-crunchtools[bot]` |
| `ASHIGARU_FORGE_ORG_GITHUB` | the GitHub org the sweep searches; the sweep runs only with this and the token | none: no sweep |
| `ASHIGARU_SWEEP_EVERY` | seconds between sweeps | `600` |
| `ASHIGARU_DEPENDABOT_AFTER` | seconds a Dependabot or Renovate PR may stay open before the sweep queues it for an exedore-max seat to land or close | `86400` |
| `ASHIGARU_TURN_MAX` | seconds a seat turn may stay active, with no write to its row for 10 minutes, before seat reconcile ends it | `10800` |
| `ASHIGARU_SILENT_AFTER` | seconds a busy seat may go without a write before seat reconcile flags it silent | `900` |
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
