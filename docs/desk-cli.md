# The ashigaru CLI

`ashigaru` is a client of the Desk's REST API ([desk-api.md](desk-api.md)).
Its command set is the one the `/ashigaru` skill and the Desk's MCP tools use:
the same names, the same arguments (`src/ashigaru/cli/commands.py`). Every
command is the MCP tool `desk_<command>` (`work` is `desk_work`, `list` is
`desk_list`), with a positional item as `item_id`, `assign --task` and its
fields as `record`, `create`'s and `update`'s
flags as `fields` and `list`'s as `filters`; `mcp` and `hook` are the CLI's
alone. Every decision (what `wrap` checks, what `work` shows) is the Desk's,
so a terminal and an MCP client get the same answer.

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
| `agents` | per seat: `free`, `busy-auto` or `busy-manual` (Scott's hold), `WORK` (the item), `TIME` held, turn, last check-in (`SEEN`) and the whole task, last and never clipped; ends `N of M up`. A seat identity that never checked in, or whose seat row has not changed in 24 hours, shows as `missing` |
| `status` | the board, from `GET /v1/status` (`desk_status`): each busy seat with its `OUTCOME`, mode, seat, `WORK`, `TIME`, `ACTIVE` and its whole task; one line of free and missing seats with `X of Y up, Z open`; then NEEDS YOU: manual seats idle with the question they last asked (`waiting_on`), Scott's blocked manual work, and subjects intake holds for approval; a last line says when the sweep ran and what it found (`swept 4m ago (31 subjects, 2 new, 2 closed)`) or its error |
| `work [--all] [--since T] [--advance]` | the whole check round, from `GET /v1/work`: open items as `WORK TIME SEAT STATE OUTCOME TASK NEXT`, grouped by outcome (groups with work only Scott can move first, then newest, then work serving none, `-`); STATE carries the mode (`manual · blocked`, auto bare), TASK is whole, NEXT is the one next step (a blocker, `landed: wrap it`, `overdue since 15:00`, a PR's `CI red`, else the item's next). `--all` adds closed and cleared; every seat (task never clipped) and `N of M up, K open`; the queue with `waits_on`; and the catch-up, the issues and PRs the org webhook brought in after the `sweep` mark (or from `--since`, a time), as `new` (queued, or `held` for a maintainer) and `reopened` (open again while the item is closed). `--advance` (admin, `POST /v1/work`) then moves the mark to the last event the round covered; a round cut at 1000 events reads on from there next time, losing and repeating nothing |
| `queue` | queued items and the state of each item they wait on; `proposed` children and their expiry |
| `sweep [--run]` | the server-side GitHub sweep, from `GET /v1/sweep` (`desk_sweep`): whether it is configured, its interval, whether one is running, and the last run (trigger, times, subjects, new, closed, error). `--run` (admin, `POST /v1/sweep`) wakes the timer and returns at once; 409 when the Desk has no org or token |
| `outcomes` | each outcome's stories: done of total, blocked, open |
| `list [--state S]... [--seat] [--lane] [--kind] [--mode] [--parent] [--subject-key K] [--ref URL] [--[no-]learned] [--[no-]memorized] [--limit N] [--offset N]` | items, newest first; `--state` repeats for any of several; `--learned` means a Learned line other than `none` is recorded |
| `get <item> \| --subject-key K \| --ref URL` | one item: by id, by `subject_key`, or the most recently updated with that ref |
| `create --task T [--id] [--subject-key K] [--kind] [--parent] [--done-when] [--ref] [--pr] [--repo] [--lane] [--mode] [--state] [--after A]... [--priority] [--note] [--filed-by] [--outcome O] [--outcome-text T]` | admin: record an item without assigning it, `queued` unless `--state` says otherwise. `--outcome` names the outcome it serves (an outcome's id, or `RT#N` or an issue, recorded as an outcome if new) and sets `parent`; with `--outcome-text`, a name that is neither is a new one-off outcome keeping that instruction (alone, the text's start names it); an unknown name without text is refused (422). A taken `subject_key` or id prints the item that holds it and exits 1 |
| `update <item> [--state] [--result] [--next] [--note] [--learned] [--blocked-reason] [--check-after] [--[no-]memorized] [--outcome O] [--outcome-text T] [...] [--clear FIELD]...` | set an item's fields (`create`'s too, admin only; `--outcome`/`--outcome-text` set the outcome it serves as on `create`; `--pr owner/repo#N` links the PR carrying the work, which a seat may set on its own item, `--clear pr` drops it); `--clear` sets one to null. A seat sets only its own open item's `state` (working, review, blocked), `result`, `next`, `learned`, `note`, `blocked_reason`, `memorized`, `check_after`, `pr` |
| `mark <name> [--set VALUE]` | a named watermark, such as `sweep`, the event `work`'s catch-up starts after; `--set` (admin) moves it, to an event or a time |
| `report <item> DONE\|BLOCKED\|WORKING\|CC-DONE [--text T] [--next N] [--learned L]` | the report contract: DONE to review with the text as result, BLOCKED to blocked with it as reason, WORKING to working; CC-DONE marks the learnings memorized; a Learned of `none` records nothing |
| `assign <item> [--seat S \| --lane L]` | assign; with no seat, the Desk picks the lane's seat free longest. `--task` (with `--subject-key`, `--done-when`, `--ref`, `--repo`, `--after`, `--mode`) records a new item first; `--outcome`/`--outcome-text` name what it serves, set on an existing item before it is assigned; if that `subject_key` is taken, or the assign is refused, it prints the item and exits 1, so the id is never lost. Sending the brief stays with Josui |
| `auto <item>`, `manual <item>` | hand an item to Josui or to Scott |
| `wait <item> [--timeout S]` | block until the item reaches review or closes; exit 1 unless review or done |
| `wait <repo> <sha> [--branch B] [--timeout S]` | block until the commit's CI is no longer pending, which includes every check branch `B` (default the repo's) requires having a conclusion; print its status, what is still pending, and its checks; exit 1 unless `success` |
| `wrap <item> [--stage wrapping\|cleared] [--learned T] [--state done\|wontdo\|abandoned] [--force]` | admin. `--stage wrapping` records that `/cc seat` was sent. `cleared` (the default) refuses until CC-DONE (or a `--learned` other than `none`) and an idle turn (a seat that never reported counts as not idle), unless `--force`, then clears the wrap and closes the item, which frees the seat |
| `claim <item>` | a seat takes a queued, unowned `auto` item in its lane whose `after` items are all done |
| `file <item> --task T [--done-when D] [--note N]` | a seat files one proposed child of its story, under the story's outcome; one per assignment |
| `mcp mint <identity> [--label L] [--out FILE]` | admin: a new hosted MCP URL, `https://<host>/mcp/<code>`. The URL is the credential and the Desk shows it once: printed alone, or with `--out` written only to that new file (mode 0600) while stdout names the code id. A Desk without the `mcp` extra (the RPM) refuses with 409 |
| `mcp list [identity]` | codes by id, identity, label, created, last used, revoked; never the code. No identity: every one's for an admin, else your own |
| `mcp revoke <id> [--identity NAME]` | revoke a code that `mcp list` shows; its URL stops on the next request |

Exit 0 on success, 1 when the Desk or a command refuses, 2 on a usage or
config error.

`wait <item>` polls the item every 30 to 60 seconds and rides out a blip.
`wait <repo> <sha>` long-polls `GET /v1/ci/wait`, which the Desk answers as
soon as a webhook delivery settles the commit and every required check of the
target branch has a conclusion, or with `pending` after 90 seconds, when the
CLI asks again; a blip is retried after 5 seconds. At `--timeout` it prints
`pending: <names>` (a required check that never started is named too) and
exits 1. A Desk
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
for verb, and so are the MCP tools; `tests/test_skill.py` fails on a mismatch
in any direction, between tool and command arguments too, and parses every
command line in the skill with the CLI's parser.

## Claude Code hooks

`hook`, like `mcp`, has no MCP tool. `ashigaru hook session-start | prompt-submit | stop` replace the
`seat-state.json` hooks, with their rules: a typed prompt (not a
`<cross-session-message>` or `<task-notification>`) that is the session's first
becomes the seat's task; `stop` (also for StopFailure and the `idle_prompt`
Notification) ends the turn and records the final assistant message as the seat's `waiting_on` (a new turn drops it); `/clear` drops the task; compact changes nothing.
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

`ashigaru hook statusline` feeds `ashigaru usage`. Run it from the status line
command, which gets Claude Code's status JSON on stdin: it posts the model,
context (`context_window.total_input_tokens` of `context_window_size`), session
tokens and `cost.total_cost_usd` to `POST /v1/seats/{name}/usage`. Session
tokens are every input, cache and output token of the session's API calls,
counted from the transcript a few MiB at a time. Cache reads are in that sum and
every call rereads the context, so a long session reads in the millions. The Desk
bills by the seat's lane (`vertex` is Vertex, `dolza-max` and `exedore-max` are
Max) and books the cost it could not bill yet, a seat with no row or a lane with
no billing, once billing is known. The status line refreshes often, so it posts at most once every 30
seconds per session, with its state (dropped a day after its last post) in `$XDG_RUNTIME_DIR/ashigaru` (else
`/dev/shm/ashigaru-<uid>`); it prints nothing at all, failures included. Python
costs ~75 ms to start, so the snippet skips it while that state file is under 30
seconds old (the file's mtime is the last attempt), and pipes the JSON to it in
the background so the bar never waits:

```bash
input=$(cat) skip=0
# Gate: skip the launch while this session's state file is under 30 s old.
if [[ $input =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  f=${XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/ashigaru}
  f=${f:-/dev/shm/ashigaru-$UID}/usage-${BASH_REMATCH[1]//[^A-Za-z0-9_-]/_}.json
  m=$(stat -c %Y "$f" 2>/dev/null) && ((d = ${EPOCHSECONDS:-0} - m, d >= 0 && d < 30)) && skip=1
fi
((skip)) || printf '%s' "$input" | ashigaru hook statusline >/dev/null 2>&1 &
```
