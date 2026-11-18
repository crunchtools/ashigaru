# The ashigaru CLI

`ashigaru` is a client of the Desk's REST API ([desk-api.md](desk-api.md)).
Its command set is the one the `/ashigaru` skill and the Desk's MCP tools use:
the same names, the same arguments (`src/ashigaru/cli/commands.py`). The item
commands `list`, `get`, `create`, `update`, `report`, `mark` and
`wait <repo> <sha>` are the MCP tools `desk_list`, `desk_get`, `desk_create`,
`desk_update`, `desk_report`, `desk_mark` and `desk_wait`. Every decision (what `wrap` checks, what
`queue` shows) is the Desk's, so a terminal and an MCP client get the same
answer.

## Config

`~/.config/ashigaru/config.toml`, mode 0600 and owned by you; a looser mode,
another owner or a symlink is refused:

```toml
url = "https://ashigaru.crunchtools.com"
api_key = "ash_<key_id>_<secret>"
seat = "Vertex 1"   # optional; defaults to the key's identity
```

The environment wins: `ASHIGARU_URL`, `ASHIGARU_API_KEY_FILE` (a podman secret
or mounted file), `ASHIGARU_API_KEY`, `ASHIGARU_SEAT`. There is no key flag. The
URL must be https, or http to loopback. Redirects are not followed.

## Commands

Every view and action prints a table, or the Desk's JSON with `--json`.

| Command | Does |
|---|---|
| `usage` | per seat: model, context, session tokens and cost, turn and time in it; spend by billing source today and this month against limits |
| `agents` | per seat: `free`, `busy-auto` or `busy-manual` (Scott's hold), item, how long, turn, task, last seen. A seat identity that never checked in, or whose seat row has not changed in 24 hours, shows as `missing` |
| `work [--all]` | the check table: blocked first, then newest; `--all` adds cleared and closed |
| `queue` | queued items and the state of each item they wait on; `proposed` children and their expiry |
| `outcomes` | each outcome's stories: done of total, blocked, open |
| `list [--state S]... [--seat] [--lane] [--kind] [--mode] [--parent] [--subject-key K] [--ref URL] [--[no-]learned] [--[no-]memorized] [--limit N] [--offset N]` | items, newest first; `--state` repeats for any of several; `--learned` means a Learned line other than `none` is recorded |
| `get <item> \| --subject-key K \| --ref URL` | one item: by id, by `subject_key`, or the most recently updated with that ref |
| `create --task T [--id] [--subject-key K] [--kind] [--parent] [--done-when] [--ref] [--repo] [--lane] [--mode] [--state] [--after A]... [--priority] [--note] [--filed-by]` | admin: record an item without assigning it, `queued` unless `--state` says otherwise. A taken `subject_key` or id prints the item that holds it and exits 1 |
| `update <item> [--state] [--result] [--next] [--note] [--learned] [--blocked-reason] [--check-after] [--[no-]memorized] [...] [--clear FIELD]...` | set an item's fields (`create`'s too, admin only); `--clear` sets one to null. A seat sets only its own open item's `state` (working, review, blocked), `result`, `next`, `learned`, `note`, `blocked_reason`, `memorized`, `check_after` |
| `mark <name> [--set VALUE]` | a named watermark, such as `sweep`, the time the last sweep reached; `--set` (admin) moves it |
| `report <item> DONE\|BLOCKED\|WORKING\|CC-DONE [--text T] [--next N] [--learned L]` | the report contract: DONE to review with the text as result, BLOCKED to blocked with it as reason, WORKING to working; CC-DONE marks the learnings memorized; a Learned of `none` records nothing |
| `assign <item> [--seat S \| --lane L]` | assign; with no seat, the Desk picks the lane's seat free longest. `--task` (with `--subject-key`, `--done-when`, `--ref`, `--repo`, `--after`, `--mode`) records a new item first; if that `subject_key` is taken, or the assign is refused, it prints the item and exits 1, so the id is never lost. Sending the brief stays with Josui |
| `auto <item>`, `manual <item>` | hand an item to Josui or to Scott |
| `wait <item> [--timeout S]` | block until the item reaches review or closes; exit 1 unless review or done |
| `wait <repo> <sha> [--timeout S]` | block until the commit's CI is no longer pending; print its status and checks; exit 1 unless `success` |
| `wrap <item> [--stage wrapping\|cleared] [--learned T] [--state done\|wontdo\|abandoned] [--force]` | admin. `--stage wrapping` records that `/cc seat` was sent. `cleared` (the default) refuses until CC-DONE (or a `--learned` other than `none`) and an idle turn (a seat that never reported counts as not idle), unless `--force`, then clears the wrap and closes the item, which frees the seat |
| `mcp mint <identity> [--label L] [--out FILE]` | admin: a new hosted MCP URL, `https://<host>/mcp/<code>`. The URL is the credential and the Desk shows it once: printed alone, or with `--out` written only to that new file (mode 0600) while stdout names the code id. A Desk without the `mcp` extra (the RPM) refuses with 409 |
| `mcp list [identity]` | codes by id, identity, label, created, last used, revoked; never the code. No identity: every one's for an admin, else your own |
| `mcp revoke <id> [--identity NAME]` | revoke a code that `mcp list` shows; its URL stops on the next request |

Exit 0 on success, 1 when the Desk or a command refuses, 2 on a usage or
config error.

`wait <item>` polls the item every 30 to 60 seconds and rides out a blip.
`wait <repo> <sha>` long-polls `GET /v1/ci/wait`, which the Desk answers as
soon as a webhook delivery settles the commit, or with `pending` after 90
seconds, when the CLI asks again; a blip is retried after 5 seconds. A Desk
that does not serve the route yet answers 404, and `wait` says so.

`subject_key` is normalised wherever it is written or looked up: an issue or PR
URL and `owner/repo#N` in any case are `owner/repo#N` in lowercase, an RT URL or
`RT #N` is `RT#N`.

## The /ashigaru skill

The `ashigaru` RPM installs the Claude Code skill that drives this CLI at
`/usr/share/ashigaru/skills/ashigaru/SKILL.md`. Claude Code loads personal
skills from `~/.claude/skills`, so link it there once per user:

```sh
mkdir -p ~/.claude/skills && ln -sfn /usr/share/ashigaru/skills/ashigaru ~/.claude/skills/ashigaru
```

The link follows every RPM update. The skill's commands are this CLI's, verb
for verb; `tests/test_skill.py` fails on a mismatch in either direction and
parses every command line in the skill with the CLI's parser.

## Claude Code hooks

`ashigaru hook session-start | prompt-submit | stop` replace the
`seat-state.json` hooks, with their rules: a typed prompt (not a
`<cross-session-message>` or `<task-notification>`) that is the session's first
becomes the seat's task; `stop` (also for StopFailure and the `idle_prompt`
Notification) ends the turn; `/clear` drops the task; compact changes nothing.
Each run is bounded at 2 seconds, prints nothing on stdout, and exits 0 on
every failure, so a hook never blocks Claude.

```json
"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "ashigaru hook prompt-submit", "timeout": 5}]}],
"SessionStart": [{"hooks": [{"type": "command", "command": "ashigaru hook session-start", "timeout": 5}]}],
"Stop": [{"hooks": [{"type": "command", "command": "ashigaru hook stop", "timeout": 5}]}]
```

The Desk lets a seat set only its `turn` and `task`, and infers Scott's hold
from them ([desk-api.md](desk-api.md#scotts-hold)): the session's first typed
prompt on a seat with no item opens a manual item, and `/clear` releases it.
