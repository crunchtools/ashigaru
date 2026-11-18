# The ashigaru CLI

`ashigaru` is a client of the Desk's REST API ([desk-api.md](desk-api.md)).
Its command set is the one the `/ashigaru` skill and the Desk's MCP tools use:
the same names, the same arguments (`src/ashigaru/cli/commands.py`).

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
| `agents` | per seat: `free`, `busy-auto` or `busy-manual` (Scott's hold), item, how long, turn, task |
| `work [--all]` | the check table: blocked first, then newest; `--all` adds cleared and closed |
| `queue` | queued items and the state of each item they wait on; `proposed` children and their expiry |
| `outcomes` | each outcome's stories: done of total, blocked, open |
| `assign <item> [--seat S \| --lane L]` | assign; with no seat, the Desk picks the lane's seat free longest. `--task` (with `--done-when`, `--ref`, `--repo`, `--after`, `--mode`) records a new item first. Sending the brief stays with Josui |
| `auto <item>`, `manual <item>` | hand an item to Josui or to Scott |
| `wait <item> [--timeout S]` | block until the item reaches review or closes; exit 1 unless review or done |
| `wrap <item> [--learned T] [--state done\|wontdo\|abandoned] [--force]` | refuse until CC-DONE and an idle turn (unless `--force`), then clear the wrap and close the item, which frees the seat |

Exit 0 on success, 1 when the Desk or a command refuses, 2 on a usage or
config error.

`wait` polls the item every 30 to 60 seconds and rides out a blip. The Desk has
no CI callback yet; when it serves `desk_wait`, `wait.waiter_for` returns a
callback waiter instead and nothing else changes.

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
