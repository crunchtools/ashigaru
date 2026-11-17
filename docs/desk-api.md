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

## Routes (all under `/v1` but `/healthz`)

| Route | Does |
|---|---|
| `GET /healthz` | 200 with the schema version; no key |
| `GET /me` | the caller's identity and role |
| `GET /items` | list, newest first; filters `state`, `seat`, `lane`, `kind`, `mode`, `parent`; pages of `limit` (200, at most 1000) from `offset` |
| `POST /items` | admin: create; `id` defaults to the next `a<N>`, `state` to `queued`; 409 on a taken id or `subject_key` |
| `GET /items/{id}` | one item |
| `PATCH /items/{id}` | the fields sent change; `null` clears |
| `POST /items/{id}/report` | the report contract: `DONE` to `review` with `text` as the result, `BLOCKED` to `blocked` with `text` as the reason, `WORKING` to `working`, `CC-DONE` sets `memorized`; a `learned` of `none` is not recorded |
| `POST /items/{id}/file` | `desk_file`: a `proposed` child under the story's outcome; one per assignment (409 on the second); abandoned 14 days later unless Josui promotes it |
| `POST /items/{id}/assign` | admin: `{seat}` names one; `{}` or `{lane}` picks the seat in the lane (default: the item's) free longest and not held by Scott. 409 when the item is closed or waits on an `after` item not done, or the seat is busy, held or unknown |
| `POST /items/{id}/claim` | `desk_claim`, seats only: take a `queued`, seatless, `auto` item in the seat's lane whose `after` items are done. Atomic; the loser of a race gets 409. The seat must be free, unheld and have a lane: a seat with no lane claims nothing |
| `GET /items/{id}/events` | the item's history, ledger lines included, oldest first; pages of `limit` (200, at most 1000) after `after` (a seq) |
| `GET /check` | the `/agents check` table: id, seat, mode, task, state, wrap, result or blocker, next. Blocked first, then newest; `?all=true` includes cleared, wontdo and abandoned |
| `GET /seats`, `GET /seats/{name}` | seat state: free/busy, owner, turn, task, item, since, and the last usage report |
| `PATCH /seats/{name}` | created on first call; a seat sends `turn` and `task`. `since` is server-set when `status` changes, `turn_started_at` when the turn goes active |
| `POST /seats/{name}/usage` | the seat or an admin: `session_id`, `model`, `billing` (max / vertex / openrouter), `context_used`, `context_window`, `session_tokens`, `cost_usd` (the session's running total; the increase since the last report is booked to today's spend) |
| `GET /usage` | `ashigaru usage`: per seat model, context, session tokens and cost, turn and seconds in it; spend by billing source today and this month (UTC) with limits |
| `PUT /billing/{billing}` | admin: `daily_usd`, `monthly_usd` limits |
| `GET /outcomes` | `ashigaru outcomes`: each outcome item with its stories' total, done, blocked and open counts |
| `GET`, `POST /identities` | admin |
| `GET /identities/{name}/keys` | admin or the owner; metadata only |
| `POST /identities/{name}/keys` | admin: mint; the token is in this response only |
| `DELETE /identities/{name}/keys/{key_id}` | admin or the owner: revoke, effective on the next request |

## Item

States: `proposed`, `queued`, `assigned`, `working`, `review`, `done`,
`blocked`, `wontdo`, `abandoned`. Wrap-up is separate, in `wrap`: `wrapping`
when `/cc seat` has been sent, `cleared` once the seat is cleared. Setting a
state stamps `assigned_at`, `queued_at` or `closed_at`; `wrap: cleared` stamps
`cleared_at`. Closing an item, clearing it, or moving it to another seat frees
the seat that held it.
Times are UTC, `YYYY-MM-DDTHH:MM:SSZ`.

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

Settings are flags on `ashigaru-desk serve`, else these environment variables:

| Variable | Means | Default (image) |
|---|---|---|
| `ASHIGARU_DESK_DB` | the SQLite file | none (`/data/desk.db`) |
| `ASHIGARU_BIND` | the address to listen on | `127.0.0.1` (`0.0.0.0`) |
| `ASHIGARU_PORT` | the port | `8080` |
| `ASHIGARU_TRUSTED_PROXIES` | comma-separated addresses or CIDRs of the reverse proxies in front | none |

Behind Cloudflare and Gloval's proxy, set `ASHIGARU_TRUSTED_PROXIES` to the
address the proxy connects from. The client address and scheme then come from
`X-Forwarded-For` and `X-Forwarded-Proto`, but only on requests from those
addresses; a client that connects directly cannot forge either.

First key, on the host:
`podman exec ashigaru ashigaru-desk mint-key "Josui Prime" --role admin --db /data/desk.db --out /data/josui.key`,
then move the key file off the volume into the client's secret store.
