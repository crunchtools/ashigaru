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
| `seat` | one worker; its identity name is its seat name | everything | items whose `seat` is its name, its own seat row, its own keys (revoke) |
| `ci` | a job | everything | its own keys (revoke) |

A seat may set only `state`, `result`, `next`, `learned`, `note`,
`blocked_reason`, `memorized` and `check_after`, and may move an item only to
`working`, `review` or `blocked`. Josui verifies and closes.

The first admin key comes from the host, since every API call needs one:
`ashigaru-desk mint-key "Josui Prime" --role admin --db <file> --out <new key file>`,
which writes the token to a new file, mode 0600, and never prints it.

Every 4xx body is `{"detail": "..."}`; each route's contract lists the codes it
can return.

## Routes (all under `/v1` but `/healthz`)

| Route | Does |
|---|---|
| `GET /healthz` | 200 with the schema version; no key |
| `GET /me` | the caller's identity and role |
| `GET /items` | list, newest first; filters `state`, `seat`, `lane`, `kind`, `mode`, `parent` |
| `POST /items` | admin: create; `id` defaults to the next `a<N>`, `state` to `queued`; 409 on a taken id or `subject_key` |
| `GET /items/{id}` | one item |
| `PATCH /items/{id}` | the fields sent change; `null` clears |
| `POST /items/{id}/report` | the report contract: `DONE` to `review` with `text` as the result, `BLOCKED` to `blocked` with `text` as the reason, `WORKING` to `working`, `CC-DONE` sets `memorized`; a `learned` of `none` is not recorded |
| `POST /items/{id}/file` | `desk_file`: a `proposed` child under the story's outcome; one per assignment (409 on the second); abandoned 14 days later unless Josui promotes it |
| `GET /items/{id}/events` | the item's history, ledger lines included, oldest first; pages of `limit` (200, at most 1000) after `after` (a seq) |
| `GET /check` | the `/agents check` table: id, seat, mode, task, state, wrap, result or blocker, next. Blocked first, then newest; `?all=true` includes cleared, wontdo and abandoned |
| `GET /seats`, `GET /seats/{name}` | seat state: free/busy, owner, turn, task, item, since |
| `PATCH /seats/{name}` | created on first call; `since` moves when `status` does |
| `GET`, `POST /identities` | admin |
| `GET /identities/{name}/keys` | admin or the owner; metadata only |
| `POST /identities/{name}/keys` | admin: mint; the token is in this response only |
| `DELETE /identities/{name}/keys/{key_id}` | admin or the owner: revoke, effective on the next request |

## Item

States: `proposed`, `queued`, `assigned`, `working`, `review`, `done`,
`blocked`, `wontdo`, `abandoned`. Wrap-up is separate, in `wrap`: `wrapping`
when `/cc seat` has been sent, `cleared` once the seat is cleared. Setting a
state stamps `assigned_at`, `queued_at` or `closed_at`; `wrap: cleared` stamps
`cleared_at`. Times are UTC, `YYYY-MM-DDTHH:MM:SSZ`.

## Ledger import

`ashigaru-desk import <ledger.jsonl>` folds Josui's agents ledger in, in one
transaction. A ledger line is a patch, so the newest value of each field wins,
and every line is also kept as an event. A line already imported (same number,
same hash) is skipped, so a re-run changes nothing and a grown ledger applies
only its new lines. `assigned`, `queued`, `blocker`, `wrapped`, `cleared`,
`closed` and the old `wrapping` state map onto columns; anything else, and any
value outside the Desk's enums, is kept in `extra`.
