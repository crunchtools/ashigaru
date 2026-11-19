---
name: ashigaru
description: Run the worker fleet from Josui Prime through the Ashigaru Desk, with exactly the `ashigaru` CLI's commands (usage, agents, status, changes, work, queue, sweep, outcomes, list, get, create, update, mark, report, assign, auto, manual, wait, wrap, claim, file, mcp, hook). List the twelve seats, assign work with a proper brief, check on it, hand items between auto (Prime) and manual (Scott), and wrap finished seats with /cc seat then /clear. Use when Scott says "/ashigaru", "list the agents", "who's up", "give this to a Vertex/Max seat", "assign", "farm this out", "check on the agents", "show the board", "what needs me", "what changed", or "what are the agents doing".
argument-hint: "[usage | agents | status | changes [--since T] [--ack] | work [--all] [--since T] [--advance] | queue | sweep [--run] | outcomes | list [--state S]... | get <item> | create --task T ... | update <item> ... | mark <name> [--set V] | report <item> <verb> | assign [item] [--seat S | --lane L] [--task T ...] | auto <item> | manual <item> | wait <item> [--timeout S] | wrap <item> [--stage S] [--state S] [--force] | claim <item> | file <item> --task T | mcp <mint|list|revoke> ... | hook <event>]"
---

# /ashigaru

**Hard rule: the subcommands and arguments are exactly the CLI's.**
`/ashigaru work --all` is `ashigaru work --all`, `/ashigaru assign a7 --lane exedore-max`
is `ashigaru assign a7 --lane exedore-max`, and the same goes for every
command: same names, flags, argument order, table and `--json` output. No
skill-only or renamed verbs. Each `## ` heading in backticks below is one CLI
subcommand, and there is one for every subcommand. Bare `/ashigaru` runs
`work`. `tests/test_skill.py` in crunchtools/ashigaru fails on a
mismatch in either direction, and parses every ashigaru command line here with
the CLI's own parser.

A thin skill over the Ashigaru Desk (`https://ashigaru.crunchtools.com`), the
system of record for every item and every seat. Use the Desk MCP tools when the
session has them, and the `ashigaru` CLI otherwise (`--json` for raw rows).
Every command is the MCP tool `desk_<command>` with the same arguments
(`ashigaru work --all` is `desk_work` with `all`, an item is `item_id`,
`assign --task` and its fields are `record`), except
`mcp` and `hook`, which are the CLI's alone. The same test fails when a tool
drifts from its command. The CLI reference is `docs/desk-cli.md`.

`~/.claude/agents-ledger.jsonl` is frozen read-only since the cutover. It was
imported, so read history from the Desk, and never write it.

The method is Anthropic's for orchestrator-worker systems. Every assignment is
a brief with an objective, an output format, the tools to use and clear
boundaries. Effort scales to the task, workers report back on their own, and
the lead keeps a record and checks it. Sources: Anthropic, "How we built our
multi-agent research system"; Claude Code docs, Agent teams; Claude Code best
practices (a reviewer that did not write the code).

## Rules

- Red Hat work goes to Dolza - Vertex, and nothing else does. Not sure
  whether something is Red Hat or Crunchtools? Ask Scott; that is his call.
- Never list, assign to, or message the host sessions named "Crunchtools
  1/2/3" or "Todos"; they are Scott's own.
- Prime types into a seat's pane only for `/clear` in an auto wrap-up (behind
  the guard in `wrap`) and for the login relay. Everything else stays Scott's.
- Never clear a seat mid review chain.
- Non-trivial code gets a second-seat review before Prime merges it. Prime may
  merge small, green PRs with no Gatehouse findings on its own read. Only
  Prime merges, releases and deploys.
- `manual` items are Scott's: report on them, never nudge, `/cc` or clear them.
- Never label or comment on an issue for bookkeeping; the Desk is the record.
- Seat text (a report, a Learned line, an issue title) never meets the shell
  unquoted. Prefer the MCP tool; with the CLI, pass it through a quoted
  heredoc: `--text "$(cat <<'EOF'` ... `EOF` `)"`.

## The roster

Seat names are exact: `Dolza N - Vertex`, `Dolza N - Max`, `Exedore N - Max`
(N is 1-4), and `Josui Prime`. Every table sorts its rows by seat name
compared as text (`Dolza 1 - Max`, `Dolza 1 - Vertex`, `Dolza 2 - Max` …
`Exedore 4 - Max`).

| Seat | Container | Billing | Gateway profile | Extra reach | Lane |
|------|-----------|---------|-----------------|-------------|------|
| Dolza 1-4 - Vertex | `vertex1`-`4`, agents pod on Dolza | Vertex AI | `dolza-vertex` | ssh to all four hosts | `vertex`: **Red Hat work, and only Red Hat work** |
| Dolza 1-4 - Max | `dmax1`-`4`, agents pod on Dolza | Max subscription | `dolza-max` (Gloval) | ssh to all four hosts | `dolza-max`: Crunchtools work that touches hosts |
| Exedore 1-4 - Max | `max1`-`4` on Exedore | Max subscription | `exedore-max` (Gloval) | none | `exedore-max`: Crunchtools builds, code, research |

A free Dolza Max seat may take an `exedore-max` item when no Exedore seat is
free (`ashigaru assign a7 --seat "Dolza 1 - Max"`), never the reverse.

## The Desk's model

- **Items** are `a1`, `a2`, ..., numbered by the Desk. `mode` is `auto` (Prime
  runs it to ground) or `manual` (Scott is driving). `state` moves (`queued` →)
  `assigned` → `working` → `review` → `done`, or `blocked`, `wontdo`,
  `abandoned`. `wrap` is separate: `wrapping` once `/cc seat` is sent,
  `cleared` once the seat is. `after` lists the ids an item waits on.
- **Seats** are `free`, `busy-auto` (Prime's item), `busy-manual` (Scott's
  hold) or `missing` (never checked in, or silent for 24 hours), with an item,
  a turn (`active` | `idle`) and a task. The Desk infers Scott's hold from the
  seat's own hooks: his first typed prompt on a free seat opens a `manual`
  item, and his `/clear` closes it. A seat is **free** only when the Desk says
  so.
- `subject_key` is the Desk's dedup key: `owner/repo#N` for a GitHub issue or
  PR, `RT#N` for an RT ticket. The Desk normalises a URL to that form. A second
  item with the same key is refused, and the CLI prints the item holding it.
- **Every filed issue is assigned or queued.** When Prime files an issue, or a
  report carries `Filed: <urls>`, `create` an item for each one unless it is
  assigned on the spot (a taken key means it is already recorded), then start
  the loop.

## `usage`

`ashigaru usage`: context, session tokens and cost per seat, plus spend by
billing source against the limits. Show it when Scott asks about spend. Before handing out a large
task, check that the seat isn't near its context window or its billing source
near its limit.

## `agents`

`ashigaru agents`: who's up, one table by seat name with `free` / `busy-auto` / `busy-manual` /
`missing`, the work each holds (WORK), how long (TIME), last check-in (SEEN)
and its whole TASK, ending `N of 12 up`. `work` carries the same table and its
`N of 12 up, K open` line, so a round needs no separate `agents`. Call
**ListAgents** too when a seat is `missing`: several `offline` rows with one name are stale registrations. For a
missing seat, the likely cause and fix:

- **Dolza N - Vertex**: the container or its session is down. With Scott's
  go-ahead (a restart ends that seat's conversation):
  `ssh dolza systemctl --user restart vertex@N`.
- **Dolza N - Max / Exedore N - Max**: usually logged out ("Not logged in" in
  its pane) or Remote Control did not start. Fix it with a login relay: read the
  URL from the pane (`ssh dolza podman exec dmaxN tmux capture-pane -p -J -t
  =dev:0`, or `ssh exedore podman exec maxN ...` from a Dolza seat). Scott
  signs in and pastes the code back; type it into the pane, then restart. Under
  claude-agent (developer 1.12.0+), the pane is `tmux -L claude-agent …
  -t =claude-agent:0`. The `=` stops tmux prefix-matching another session.

## `status`

`ashigaru status`: the board Scott reads. Each busy seat (OUTCOME, MODE, SEAT,
WORK, TIME, ACTIVE, whole TASK), one line of free and missing seats with `X of Y up,
Z open`, then NEEDS YOU: manual seats idle with the question they last asked
(`waiting_on`, from the seat's `stop` hook, dropped on a new turn or `/clear`),
Scott's blocked manual work, his holds on seats silent for a day (`stale`: the
Desk never closes them; tell Scott), and subjects intake holds for approval. A
seat whose `stop` hook never arrived is ended by the Desk itself after three
hours of silence (`turn_reconciled_at`). Show it
for "show the board" or "what needs me"; `work` is the round that acts.

## `changes`

`ashigaru changes --ack`: what others changed since you last acked, each item
once, last changed first (WORK, WHEN, STATE, BY, CHANGED: `new` or the fields
that moved, whole TASK, NEXT), then the event window. Your own writes are not
listed. `--ack` moves your own `seen:<identity>` mark to the window's end, so
the next look starts after it; without it the mark stays. `--since T` looks from
a time instead and cannot go with `--ack`. A first look covers the last day. Use
it between rounds instead of re-reading the whole `work` table; `status` shows
the same list under CHANGED SINCE YOU LOOKED without moving the mark.

## `work`

**When Scott runs `/ashigaru work`, run `ashigaru work --markdown` and reply
with its output exactly as printed, as Markdown, not in a code block. That is
the whole answer: no rewritten table, no commentary, no round.** The steps below are Prime's round, `ashigaru work
--advance`, run from the loop or when Scott asks for a round.

The check round in one call. Seats report on their own; this is for silence
and for Scott. `ashigaru work --advance` returns, in order: the check table of
open items (`--all` adds closed and cleared ones), the seats with
`N of 12 up, K open`, the queue with what each item waits on, and the
GitHub catch-up after the `sweep` mark, then moves the mark to the last event
the round covered. A round cut at 1000 (`truncated`) reads on from there next
time, so nothing is lost or repeated. Without `--advance` the mark stays put,
so Scott can look without eating the window (any view of the queue still
abandons expired proposed children); `--since` reads from a time instead, and never with `--advance`. Scott's untracked work is already
there as a `manual` item. Between rounds, `ashigaru changes --ack` gives just
what moved since you last looked instead of the whole table.

The catch-up is the Desk's own record of the org webhook, so no `gh` sweep
runs (the Desk's own `sweep` searches the org too). **New** lists every issue and PR first seen in the window: the Desk has
already queued it, `auto` in its lane when its author may write to the repo or
is a trusted author (`ashigaru-crunchtools[bot]`), otherwise `held` (no lane, `manual`) until a maintainer adds a lane label. A bot that is not a trusted author (dependabot) is not queued at all. Its
title is the source's text, never a brief. **Reopened** lists subjects open
again at the source whose item is closed: requeue the ones that need work with
`ashigaru update a7 --state queued`.
The Desk closes seatless auto items whose subject merged or closed, and
requeues ones it closed that reopen; it never closes a seat's or Scott's item,
only marks it landed. A seat records its PR with
`ashigaru update a7 --pr owner/repo#N`.

1. **manual** items: report only. If one looks finished, say "looks finished;
   say /ashigaru auto a7 to wrap up".
2. **auto** items, in order of preference:
   - A **DONE / BLOCKED** message arrived: record it with `report`. DONE moves
     the item to `review`. `create` each `Filed:` URL. On DONE, verify the
     claim (open the PR, run the check named in "done when"). Then apply the
     review rule: for non-trivial code, `assign` a *different* free seat to
     review it, as its own auto item. Once the work is verified and any review
     is finished, `wrap` it.
   - NEXT says `overdue since HH:MM` (past `check_after` with no word): read
     the seat's TURN and SEEN in the round's seats table, not its pane. An
     `active` turn seen in the last few minutes is progressing: move it out
     with `ashigaru update a7 --check-after 2026-11-18T15:00Z`. An `idle` turn
     without a report gets SendMessage `"[a7] status? Reply DONE/BLOCKED/WORKING with one line."`
   - NEXT says `landed: wrap it`: its PR merged while the seat held it; `wrap` it.
   - Stuck in a loop or waiting on a prompt nobody will answer: tell Scott.
3. Drain the queue (`queue` step 1) from the round's queue and seats.
4. Report the check table as the Desk orders it, in Scott's columns: WORK,
   TIME, SEAT, STATE (`manual · blocked`; auto bare), OUTCOME (`-` when none),
   the whole TASK, and NEXT, the one next step (a blocker, `landed: wrap it`,
   `overdue since 15:00`, `owner/repo#N CI red`, else the item's next). Rows
   come grouped by outcome, groups holding work only Scott can move first. Below it, the seat line, then **Queued** items, then
   **New since last check** from the catch-up: ref, title, lane (or `held`),
   `filed_by`, and any **Reopened**.

## `queue`

`ashigaru queue`: queued items, the state of each item they wait on
(`waits_on`), and proposed children. `work` carries the same queue; the org
sweep that used to run here is `work`'s catch-up.

1. **Drain**, for each free seat: pick the oldest auto queued item in its lane
   whose `waits_on` items are all `done`. A Dolza Max seat falls back to
   `exedore-max` items. An item waiting on a `blocked` or `abandoned` one is
   stuck: report it, don't wait on it. The Desk has already closed any whose
   GitHub subject merged or closed, so don't re-read issues. The Desk does not
   read RT: if an `RT#N` item's ticket is resolved, close it unbriefed with
   `ashigaru update a7 --state done --result "done elsewhere"` and pick again.
   Otherwise `ashigaru assign a7 --seat "Exedore 1 - Max"`, then `assign`
   steps 4-5.

## `sweep`

`ashigaru sweep`: the Desk's own sweep of GitHub, when it has an org and a
token. It re-reads every open item's subject and PR, searches the org for
issues and PRs updated since the `sweep-search` mark (queuing a member's or a
trusted author's, held unless the author writes to that repo; anyone else's, other bots included, is only listed under NEEDS
YOU as `outsider`, never stored: create it by hand if it is work), links a
writer's PR that names `(a676)` as that item's `pr`, and settles everything. It
runs at startup, every 10 minutes and on request, and never writes to GitHub.
Prime no longer runs `gh search` or per-PR lookups. The board's last line shows
`swept 4m ago (31 subjects, 2 new, 2 closed)`; when it shows an error, run
`ashigaru sweep` to read it, and `ashigaru sweep --run` to run one now (admin;
returns at once).

## `outcomes`

`ashigaru outcomes`: each outcome (an RT ticket, a GitHub issue, or Scott's
one-off instruction) with its stories' done, blocked and open counts. Name the
outcome when recording work: `--outcome RT#1652` (or an issue, or an outcome's
id) finds or records it; for an instruction with no ticket,
`--outcome board --outcome-text "<Scott's words>"` records a one-off outcome
named `board` that keeps the words. A review or fix-up of a story's PR inherits
its outcome, so give it none. Use it when Scott asks how a feature or program is going.

## `list`

Items, newest first, by any filters: `ashigaru list --state blocked --state review`,
`ashigaru list --lane vertex --mode auto`, `ashigaru list --learned --no-memorized`
for Learned lines not yet stored in memory.

## `get`

One item by id (`ashigaru get a7`), by key (`ashigaru get --subject-key crunchtools/ashigaru#12`),
or the most recently updated with a ref (`ashigaru get --ref https://github.com/crunchtools/ashigaru/issues/12`).

## `create`

Record an item without assigning it; it is `queued` unless `--state` says
otherwise. `--subject-key` only when there is a ref. `--mode manual` keeps a
queued item for Scott, so `queue` skips it. The task is in Prime's words, never
the issue's title verbatim:

```sh
ashigaru create --task "Retry PyPI timeouts in pip-audit" --done-when "PR open, CI green" --ref https://github.com/crunchtools/python-sdlc/issues/66 --subject-key crunchtools/python-sdlc#66 --repo crunchtools/python-sdlc --lane exedore-max --mode auto --filed-by fatherlinux
```

## `update`

Set an item's fields; `--clear FIELD` nulls one. Prime uses it for
`check_after`, closing an item done elsewhere, and requeueing:
`ashigaru update a7 --check-after 2026-11-18T15:00Z`, `ashigaru update a7 --clear check_after`.

## `mark`

A named watermark on the Desk. `sweep` is the only one in use: the event
`work`'s catch-up starts after, moved by `ashigaru work --advance`. Read it with
`ashigaru mark sweep`; set it by hand to a time (`ashigaru mark sweep --set 2026-11-18T14:00Z`)
only to replay a window.

## `report`

The report contract. A seat's `DONE a7: ...` is
`ashigaru report a7 DONE --text "<result>" --learned "<Learned line>"`, through
the heredoc form in the Rules when the text came from the seat. BLOCKED takes
the blocker as `--text`, WORKING a progress note, and `CC-DONE` marks the
item's learnings memorized. A Learned of `none` records nothing.

## `assign`

1. **Pick the lane.** Red Hat work is `vertex`, nothing else is. Work that
   needs a host (ssh, podman, units) is `dolza-max`. Everything else is
   `exedore-max`.
2. **Scale the effort.** One seat for one well-bounded task. Split only along
   lines that cannot collide: separate repositories, or separate files with
   no shared edits. Three to five seats at once is the useful ceiling.
3. **Record and assign** before briefing, so the brief carries the Desk's id:
   `ashigaru assign --task "<one line>" --done-when "<one line>" --ref <url> --subject-key <key> --repo <owner/repo> --lane exedore-max --outcome RT#1652`,
   or `ashigaru assign a7 --lane exedore-max` for an item already recorded
   (`--outcome`/`--outcome-text` set its outcome first; see `outcomes`).
   The Desk picks the lane's seat that has been free longest and isn't held by
   Scott, and never a busy one; `--seat` names a seat instead. If the assign is
   refused, the CLI prints the item, which stays `queued`: start the loop, tell
   Scott, and stop.
4. **Write the brief.** The seat knows only what it says. Send it with
   **SendMessage** to the seat's exact name, with the Desk's id:

   ```
   [a7] <objective in one sentence>
   Context: <what is known, what was ruled out, links/paths/ticket>
   Scope: <repo and branch; files or areas in bounds>. Out of bounds: <what not to touch>.
   Use: <tools, hosts, skills that fit>. Don't: <deploy / merge / message others / restart X> unless stated.
   Use: Fan out with Sonnet subagents (Agent tool, model sonnet) wherever the work splits; you stay the integrator: judgment, edits and final verification are yours.
   Done when: <the observable result, e.g. "PR open with CI green", "root cause named with evidence">.
   Report: SendMessage to Josui Prime (the name ListAgents shows) — "DONE a7: <result, links>" or "BLOCKED a7: <what you need>", then "Filed: <urls of issues you opened, or none>".
   End the report with "Learned: <what you hit that the next person would not expect — a gotcha, a wrong assumption, a missing check>" or "Learned: none".
   If you stop partway, report what is finished and what is left.
   When Prime asks, run /cc seat and reply CC-DONE a7.
   Gatehouse: answer each finding with a reply whose first line is "Fixed in <sha>", "Not a bug: <20+ chars>" or "Deferred: <open issue URL>"; anything else leaves triage red, and a reply posted just before a push waits for the next retriage.
   ```
5. Set `check_after`: `ashigaru update a7 --check-after 2026-11-18T15:00Z`,
   about 30 minutes out for a small task, two hours for a large one. Start the
   loop if it isn't running. Don't ask; this is the default every time work
   goes out.
6. Tell Scott in one line: which seat, the id, what "done" means.

## `auto`

`ashigaru auto a7` hands the item to Prime; treat it as an assigned item from
then on. Given a seat, take the item `agents` shows it holding.

## `manual`

`ashigaru manual a7` hands it back to Scott; stop sending that seat anything.

## `wait`

`ashigaru wait a7 --timeout 3600` blocks until the item reaches `review` or
closes. `ashigaru wait crunchtools/ashigaru 8d2682f --timeout 900` blocks until
that commit's CI ends and every check its target branch requires (`--branch`,
default the repo's) has reported; at the timeout it names what is still pending. Use them instead of polling when Prime has nothing else
to do.

## `wrap`

Auto items only.

1. SendMessage `"[a7] run /cc seat"`, mark the stage with
   `ashigaru wrap a7 --stage wrapping`, and wait for `CC-DONE a7`. Record it
   with `ashigaru report a7 CC-DONE`.
2. Confirm the pane is idle: the Desk says the turn is idle, no shell command
   running, no open prompt or dialog, and an empty input line in
   `capture-pane`. Attached tmux clients don't matter: Scott often attaches
   just to watch, and the empty-input check covers anything he is typing. If
   in any doubt, don't clear; check again next round.
3. Type the clear. The guard runs in the same call and echoes which branch ran:
   - `TURN-ACTIVE`: the Desk doesn't say the seat's turn is `idle`. Either it
     is mid-turn, whatever the pane shows, or the Desk didn't answer (shown as
     an empty value). Either way, don't clear.
   - `IN-MODE`: the pane is in tmux copy mode, with Scott scrolling.
     `capture-pane` still shows the live screen, but keys would go to copy
     mode and never reach Claude.
   - `BUSY`: the spinner line (`<verb>… (<elapsed>`, or an older build's "esc
     to interrupt") is in the bottom 25 lines. Claude Code keeps an empty
     input box up mid-turn, and a typed `/clear` would queue and wipe the turn
     when it ends. Checking only the bottom keeps transcript text that quotes
     these strings from matching.
   - `NOT-EMPTY-PROMPT`: the bottom 8 lines hold no empty input line directly
     above the box's `─` rule, so a multi-line draft that opens with a blank
     line still counts as typed. The prompt is `❯` plus a NO-BREAK SPACE,
     which `[[:space:]]` misses, hence the `sed`.
   - `NO-SESSION`: there is no Claude tmux session at all.
   - `CLEARED`: the clear was sent.

   The pane part targets the claude-agent layout when that server is up, else
   the old `dev:0`. It goes over stdin as a quoted heredoc so its quotes
   survive ssh, podman and sh. Dolza is shown; on Exedore it is `ssh exedore
   podman exec -i maxN sh <<'EOF'` with the same body:

   ```sh
   turn=$(ashigaru agents --json | python3 -c 'import json,sys; print(next((s["turn"] for s in json.load(sys.stdin) if s["name"] == sys.argv[1]), ""))' '<seat name>')
   if [ "$turn" != idle ]; then echo "TURN-ACTIVE ($turn)"; else
   ssh dolza podman exec -i <dmaxN|vertexN> sh <<'EOF'
   if tmux -L claude-agent has-session -t =claude-agent 2>/dev/null; then T='tmux -L claude-agent'; P='=claude-agent:0'
   elif tmux has-session -t =dev 2>/dev/null; then T=tmux; P='=dev:0'
   else echo NO-SESSION; exit; fi
   pane=$($T capture-pane -p -t "$P" | tail -n 25 | sed 's/\xc2\xa0/ /g')
   if [ "$($T display-message -p -t "$P" '#{pane_in_mode}')" != 0 ]; then echo IN-MODE
   elif printf '%s\n' "$pane" | grep -Eq '… \([0-9]+[hms]|esc to interrupt'; then echo BUSY
   elif ! printf '%s\n' "$pane" | tail -n 8 | awk 'p && /^─/ {ok=1} {p = /^❯ *$/} END {exit !ok}'; then echo NOT-EMPTY-PROMPT
   else $T send-keys -t "$P" /clear Enter && echo CLEARED; fi
   EOF
   fi
   ```
4. Close it with `ashigaru wrap a7`. The Desk refuses without CC-DONE or while
   the turn is active, then clears the wrap, closes the item `done` and frees
   the seat. Seat text never goes on its command line: the Learned line
   already reached the Desk through `report`, so `--learned` stays unused.
   Confirm with `agents` that the seat shows `free`.

## `claim`

A seat's own pull, not Prime's: `ashigaru claim a7` takes a queued, unowned
`auto` item in the seat's lane whose `after` items are all `done`. Prime
assigns instead.

## `file`

A seat's one proposed child per assignment:
`ashigaru file a7 --task "Split the parser" --done-when "PR open"` files it
under the story's outcome as `proposed`, which `queue` lists until Prime
queues it or it expires.

## `mcp`

Hosted Desk MCP URLs, one per identity. Each URL is a credential, so mint one
only when Scott asks, and always with `--out`, so the URL lands in a new 0600
file and never in the transcript:
`ashigaru mcp mint "Dolza 1 - Max" --label seat --out /run/user/1000/dmax1.mcp`.
`ashigaru mcp list` shows codes (id, identity, created, last used);
`ashigaru mcp revoke <id>` kills one at the next request.

## `hook`

Not run by hand, and like `mcp` the CLI's alone. `ashigaru hook session-start`, `prompt-submit` and `stop` are
the seats' Claude Code hooks (`docs/desk-cli.md`), which report each seat's
turn and task to the Desk; `ashigaru hook statusline`, run from the seat's
status line, reports its model, context, session tokens and cost for `usage`.
`ashigaru hook next` runs on SessionStart, async with asyncRewake: it waits for
the Desk to hand this free seat its next queued item and wakes it with the
brief. Nothing flows until `ashigaru mark autodispatch --set on`.
When `agents` shows a seat's turn or task wrong, or `usage` shows it blank,
check that its settings and status line carry them.

## The loop

Every time work goes out, start `/loop 15m /ashigaru work --advance` if it
isn't running. Stop it (CronDelete) only when no auto item is queued, every auto
item is closed, **and** its work has landed: PRs reviewed where the review
rule asks, merged, releases cut, and the change deployed to production and
verified. A seat reporting DONE is not the stop condition; Prime's merge and
deploy are. When it stops, tell Scott what shipped.
