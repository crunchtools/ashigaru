"""Claude Code hooks: report the seat's turn, task and usage to the Desk. They replace the
seat-state.json hooks and keep their rules.

  session-start   SessionStart. Registers the session with POST
                  /v1/seats/{name}/session (session_id, source), which turns the
                  seat idle (clear also drops the task) and answers the epoch,
                  kept in ~/.local/state/ashigaru/epoch for every later request
                  to send as X-Ashigaru-Epoch. A resume of a session the Desk
                  ended (409 stale-session) leaves the epoch alone and prints a
                  notice on stdout, which SessionStart adds to the session's
                  context: stop work on any earlier assignment. A Desk without
                  the route (404, 405) gets the old PATCH of turn idle (clear
                  also task null). compact can come mid-turn and changes
                  nothing.
  prompt-submit   UserPromptSubmit. turn active. A prompt a person typed, the
                  session's first, becomes the task; turns Claude Code starts
                  itself (a <cross-session-message>, a <task-notification>,
                  a scheduled task firing) leave the task alone.
  stop            Stop, StopFailure and the idle_prompt Notification: turn
                  idle. idle_prompt leaves alone a turn that started too
                  recently to be the one it reports on. Stop also sends the
                  seat's final message as waiting_on, trimmed to WAITING_LENGTH.
  next            SessionStart (startup, clear, resume), registered async with
                  asyncRewake: waits up to NEXT_SECONDS for the Desk to hand
                  this free seat its next queued item, then prints the brief
                  on stderr and exits 2, which wakes the idle session with it.
                  Its asks are what keeps an idle seat's lease, so while the
                  seat cannot take work (401, 403, 404, or a 409 that is not
                  busy, held or stale-session) it checks in again every
                  NEXT_CHECK_IN; a busy or held seat is asked again with
                  backoff. On a 409 stale-session it stops silently if the
                  epoch file now holds a newer session's epoch (a /clear), else
                  exits 2 with a notice that the Desk ended this session.
  statusline      The status line command, not a hook: model, context, session
                  tokens and running cost to POST /v1/seats/{name}/usage. It
                  runs on every refresh, so it posts at most once per
                  STATUSLINE_EVERY per session, keeping its state on tmpfs,
                  and prints nothing, not even its failures.

A hook must never stand between a person and their prompt: the whole run is
bounded at HOOK_SECONDS, every failure (no config, no network, a refusal) goes
to stderr, and the exit is 0. `next` alone runs in the background, bounded at
NEXT_SECONDS, and exits 2 only to hand over a brief. Nothing goes to stdout, which UserPromptSubmit
would add to the conversation.
"""

import json
import os
import re
import signal
import stat
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import FrameType
from typing import Any, TextIO
from urllib.parse import quote

import httpx

from .client import Desk, DeskError, Send, Unreachable, epoch, epoch_file
from .config import load

EVENTS = ("session-start", "prompt-submit", "stop", "next", "statusline")
HOOK_SECONDS = 2.0
# How long `next` waits for work, under the 86400 s timeout its hook entry sets: an idle seat after
# /clear runs no turn, so nothing would start another wait.
NEXT_SECONDS = 86100.0
# One long-poll to the Desk, under its MAX_WAIT.
NEXT_WAIT = 60
# Before the first ask: SessionStart's own hook frees the seat on a /clear, and runs alongside.
NEXT_SETTLE = 3.0
# How much longer than one long-poll the client waits for the Desk's answer.
NEXT_GRACE = 15
# After a Desk that is down, restarting or throttling, before asking again; doubling to NEXT_BACKOFF.
NEXT_RETRY = 5.0
NEXT_BACKOFF = 300.0
# Between looks that brought nothing back, so an empty answer is never a spin.
NEXT_PAUSE = 2.0
# Answers that mean this seat will not get work by asking again soon: check in after NEXT_CHECK_IN. So
# is any 409 but NEXT_BUSY and the stale-session one.
NEXT_FINAL = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND})
# Between check-ins of a seat that cannot take work: each renews its lease, which lapses after 10
# minutes idle (desk.model.LEASE), so the seat stays on the board.
NEXT_CHECK_IN = 180.0
# The 409 codes that pass: the seat is mid-wrap or held, so ask again with the backoff.
NEXT_BUSY = frozenset({"busy", "held"})
# The 409 code for a session the Desk has ended or superseded.
STALE = "stale-session"
# The sources a session is registered for; compact is not a new session.
SESSION_SOURCES = frozenset({"startup", "resume", "clear"})
# Claude Code's asyncRewake: an async hook exiting 2 wakes the idle session with its stderr.
WAKE = 2
TASK_LENGTH = 120
# The most of the seat's final message kept as waiting_on; the end, where the question is.
WAITING_LENGTH = 500
# The most of a transcript's tail a Stop reads to find that message.
WAITING_TAIL = 256 << 10
# Claude Code's messageIdleNotifThresholdMs: idle_prompt comes at least this
# long after the turn it reports on ended.
IDLE_NOTICE_AFTER = timedelta(seconds=60)
# What Claude Code puts first in a prompt it submits on its own.
HARNESS_MARKS = ("<cross-session-message", "<task-notification")
# A scheduled task (CronCreate, /loop, a durable routine) submits its prompt bare. What marks it is
# the system entry Claude Code appends to the transcript as it fires, carrying the prompt.
SCHEDULED_FIRE = "scheduled_task_fire"
SCHEDULED_PROMPT_LENGTH = 200
STATUSLINE_EVERY = 30.0
# The most of a transcript one status line run reads; the rest waits for the next.
TRANSCRIPT_CHUNK = 4 << 20
# A session's state is dropped this long after its last post, so tmpfs holds only live sessions.
STATE_KEPT = timedelta(days=1)
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class _Deadline(Exception):
    pass


def _alarm(signum: int, frame: FrameType | None) -> None:
    raise _Deadline


def task_marker() -> Path:
    """The session whose task the Desk already has, so a later prompt does not replace it."""
    return Path.home() / ".local/state/ashigaru/task-session"


def _started_within(seat: dict[str, Any], window: timedelta) -> bool:
    try:
        started = datetime.strptime(seat["turn_started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        return False
    return datetime.now(UTC) - started < window


def _stale(detail: str) -> str:
    """What the model is told when the Desk ended its session."""
    return (
        f"Ashigaru Desk: this session is stale: {detail}. Any earlier Desk assignment is no longer "
        "yours: stop work on it and do not push, comment or report on it. Ask for /clear to rejoin."
    )


def _keep_epoch(number: Any) -> None:
    """Write the epoch the Desk answered to epoch_file(), atomically."""
    target = epoch_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".tmp")
    partial.write_text(f"{int(number)}\n")
    partial.replace(target)


def _start(event: dict[str, Any], desk: Desk, path: str) -> None:
    source = event.get("source")
    if source == "compact":
        return
    if source == "clear":
        task_marker().unlink(missing_ok=True)
    session = event.get("session_id")
    if source in SESSION_SOURCES and isinstance(session, str) and session:
        try:
            answer = desk.post(f"{path}/session", {"session_id": session, "source": source})
        except DeskError as error:
            if error.status == HTTPStatus.CONFLICT and error.code == STALE:
                print(_stale(error.detail))
                return
            if error.status not in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED):
                raise
            epoch_file().unlink(missing_ok=True)
        else:
            if isinstance(answer, dict) and isinstance(answer.get("epoch"), int):
                _keep_epoch(answer["epoch"])
            return
    desk.request(
        "PATCH", path, body={"turn": "idle", "task": None} if source == "clear" else {"turn": "idle"}
    )


def _tail(transcript: str) -> list[bytes]:
    """The whole lines in the final WAITING_TAIL bytes of the transcript."""
    with open(transcript, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - WAITING_TAIL))
        tail = handle.read()
    lines = tail.splitlines()
    return lines[1:] if size > WAITING_TAIL else lines


def _fired(entry: Any, prompt: str) -> bool:
    """True when the transcript entry is a scheduled task firing this prompt. Claude Code's copy has
    its whitespace collapsed and is cut at SCHEDULED_PROMPT_LENGTH characters, so it is the prompt,
    or one that long that starts it."""
    if not isinstance(entry, dict) or entry.get("type") != "system" or entry.get("subtype") != SCHEDULED_FIRE:
        return False
    fired = entry.get("prompt")
    if not isinstance(fired, str) or not (fired := " ".join(fired.split())):
        return False
    said = " ".join(prompt.split())
    return said == fired or len(fired) >= SCHEDULED_PROMPT_LENGTH - 1 and said.startswith(fired)


def _scheduled(event: dict[str, Any], prompt: str) -> bool:
    """True when the transcript says a scheduled task just fired this prompt: the entry comes after
    the last assistant entry, so one that fired an earlier turn never matches a prompt typed later."""
    transcript = event.get("transcript_path")
    if not isinstance(transcript, str):
        return False
    try:
        lines = _tail(transcript)
    except OSError:
        return False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if _fired(entry, prompt):
            return True
        if isinstance(entry, dict) and entry.get("type") == "assistant":
            return False
    return False


def _prompt(event: dict[str, Any], desk: Desk, path: str) -> None:
    prompt = str(event.get("prompt", "")).strip()
    session = str(event.get("session_id", ""))
    marker = task_marker()
    try:
        kept = marker.read_text().strip() == session
    except OSError:
        kept = False
    if prompt.startswith(HARNESS_MARKS) or kept or not prompt or _scheduled(event, prompt):
        desk.request("PATCH", path, body={"turn": "active"})
        return
    desk.request("PATCH", path, body={"turn": "active", "task": prompt[:TASK_LENGTH]})
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(session + "\n")


def _entry_text(entry: Any) -> str | None:
    """The text blocks of an assistant transcript entry joined by a blank line, or None."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "\n\n".join(texts) if texts else None


def _last_message(transcript: str) -> str | None:
    """The last assistant text in the final WAITING_TAIL bytes of the transcript. Claude Code writes
    a line per content block, so lines with only tool_use or thinking are passed over."""
    for line in reversed(_tail(transcript)):
        try:
            text = _entry_text(json.loads(line))
        except ValueError:
            continue
        if text is not None:
            return text
    return None


def _waiting_on(event: dict[str, Any]) -> str | None:
    """What the seat said last, trimmed to WAITING_LENGTH from the front, or None when unknown or
    unreadable."""
    text = event.get("last_assistant_message")
    transcript = event.get("transcript_path")
    if not isinstance(text, str) and isinstance(transcript, str):
        try:
            text = _last_message(transcript)
        except OSError:
            # Best effort: a transcript it cannot read costs the question, never the idle report.
            text = None
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        return None
    return text if len(text) <= WAITING_LENGTH else "…" + text[-(WAITING_LENGTH - 1) :]


def _stop(event: dict[str, Any], desk: Desk, path: str) -> None:
    kind = event.get("hook_event_name")
    if kind == "Notification":
        seat = desk.get(path)
        if seat["turn"] == "idle" or _started_within(seat, IDLE_NOTICE_AFTER):
            return
    body: dict[str, Any] = {"turn": "idle"}
    if kind in (None, "Stop") and (waiting := _waiting_on(event)) is not None:
        body["waiting_on"] = waiting
    desk.request("PATCH", path, body=body)


class _Refused(Exception):
    """The Desk said this seat cannot take work now: check in again after NEXT_CHECK_IN."""


class _Stale(Exception):
    """The Desk said this session is over; `detail` is why."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _ask(desk: Desk, method: str, path: str, deadline: float, **params: Any) -> Any:
    """One request, retried with backoff while the Desk is down, restarting or throttling (5xx, 429,
    a 409 busy or held), until `deadline` on the monotonic clock. Raises _Refused on NEXT_FINAL or any
    other 409, _Stale on a 409 stale-session, and _Deadline when no request can finish in the time left."""
    pause = NEXT_RETRY
    while True:
        wait = int(min(params.get("wait", 0), deadline - time.monotonic() - NEXT_GRACE))
        if wait < 0:
            raise _Deadline
        try:
            return desk.request(
                method,
                path,
                params={"wait": wait} if "wait" in params else None,
                send=Send(timeout=wait + NEXT_GRACE),
            )
        except (DeskError, Unreachable) as error:
            if isinstance(error, DeskError):
                if error.status == HTTPStatus.CONFLICT:
                    if error.code == STALE:
                        raise _Stale(error.detail) from error
                    if error.code not in NEXT_BUSY:
                        raise _Refused from error
                elif error.status in NEXT_FINAL:
                    raise _Refused from error
        time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
        pause = min(pause * 2, NEXT_BACKOFF)


def _next(desk: Desk, seat: str | None) -> str | None:
    """The brief of the item the Desk hands this seat, or None once the wait runs out. Every request
    ends before NEXT_SECONDS, so no claim lands on a hook already gone. While the seat cannot take work
    it checks in every NEXT_CHECK_IN, which keeps its lease. On a stale-session answer it returns the
    notice for the model, unless the epoch file has moved on since it started: a newer session on this
    seat took over, and it stops silently."""
    deadline = time.monotonic() + NEXT_SECONDS
    time.sleep(NEXT_SETTLE)
    started = epoch()
    name = seat
    try:
        while True:
            try:
                name = name or _ask(desk, "GET", "/v1/me", deadline)["name"]
                found = _ask(desk, "POST", f"/v1/seats/{quote(name, safe='')}/next", deadline, wait=NEXT_WAIT)
            except _Refused:
                time.sleep(min(NEXT_CHECK_IN, max(0.0, deadline - time.monotonic())))
                continue
            brief = found.get("brief") if isinstance(found, dict) else None
            if isinstance(brief, str) and brief.strip():
                return brief
            time.sleep(NEXT_PAUSE)
    except _Stale as stale:
        return None if epoch() != started else _stale(stale.detail)
    except _Deadline:
        return None


def usage_state(session: str, env: Mapping[str, str]) -> Path:
    """Where the status line keeps its throttle and transcript offset: tmpfs, private to the user,
    since it is looked at on every refresh."""
    runtime = env.get("XDG_RUNTIME_DIR")
    directory = Path(runtime) / "ashigaru" if runtime else Path(f"/dev/shm/ashigaru-{os.getuid()}")
    return directory / f"usage-{re.sub(r'[^A-Za-z0-9_-]', '_', session)}.json"


def _private(directory: Path) -> bool:
    """True when only this user can write the state directory. /dev/shm is shared, so another user
    could have made it, or a symlink by its name, first."""
    info = directory.lstat()
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022


def _count_tokens(transcript: str, state: dict[str, Any]) -> None:
    """Add the API calls appended to the transcript since the last run to state["tokens"]. Claude
    Code writes a line per content block, each carrying its message's usage, so an id counts once."""
    if state.get("transcript") != transcript:
        state.update(transcript=transcript, offset=0, tokens=0, last_id=None)
    with open(transcript, "rb") as handle:
        handle.seek(state["offset"])
        chunk = handle.read(TRANSCRIPT_CHUNK)
    end = chunk.rfind(b"\n") + 1 or (len(chunk) if len(chunk) == TRANSCRIPT_CHUNK else 0)
    for line in chunk[:end].splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        message_id = message.get("id")
        if not isinstance(usage, dict) or (message_id is not None and message_id == state["last_id"]):
            continue
        state["last_id"] = message_id
        state["tokens"] += sum(v for k in TOKEN_FIELDS if isinstance(v := usage.get(k), int))
    state["offset"] += end


def _number(value: Any) -> Any:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _part(event: dict[str, Any], key: str) -> dict[str, Any]:
    value = event.get(key)
    return value if isinstance(value, dict) else {}


def _statusline(event: dict[str, Any], desk: Desk, path: str) -> None:
    model, cost, window = _part(event, "model"), _part(event, "cost"), _part(event, "context_window")
    report = {
        "session_id": str(event["session_id"]),
        "model": model.get("id") if isinstance(model.get("id"), str) else None,
        "context_used": _number(window.get("total_input_tokens")),
        "context_window": _number(window.get("context_window_size")),
        "session_tokens": event.get("session_tokens"),
        "cost_usd": _number(cost.get("total_cost_usd")),
    }
    desk.post(f"{path}/usage", {key: value for key, value in report.items() if value is not None})


def _throttled(event: dict[str, Any], env: Mapping[str, str]) -> bool:
    """True when this session posted within STATUSLINE_EVERY. Otherwise counts the transcript into
    event["session_tokens"] and records the attempt first, so a Desk that is down is not retried on
    every refresh, and drops any session's state once it is STATE_KEPT old."""
    session = event.get("session_id")
    if not session:
        return True
    state_path = usage_state(str(session), env)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _private(state_path.parent):
        return True
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    now = datetime.now(UTC).timestamp()
    if now - state.get("posted", 0) < STATUSLINE_EVERY:
        return True
    state["posted"] = now
    transcript = event.get("transcript_path")
    if isinstance(transcript, str) and Path(transcript).is_file():
        _count_tokens(transcript, state)
        event["session_tokens"] = state["tokens"]
    partial = state_path.with_suffix(".tmp")
    partial.write_text(json.dumps(state))
    partial.replace(state_path)
    for old in state_path.parent.glob("usage-*.json"):
        if now - old.stat().st_mtime > STATE_KEPT.total_seconds():
            old.unlink(missing_ok=True)
    return False


HANDLERS = {"session-start": _start, "prompt-submit": _prompt, "stop": _stop, "statusline": _statusline}


def _report(name: str, stdin: TextIO, env: Mapping[str, str] | None, transport: Any) -> str | None:
    """Run the hook; `next` returns the brief it was handed."""
    event = {} if stdin.isatty() else json.loads(stdin.read() or "{}")
    if not isinstance(event, dict):
        raise ValueError("the hook event is not a JSON object")
    if name == "statusline" and _throttled(event, os.environ if env is None else env):
        return None
    config = load(env)
    desk = Desk(config, HOOK_SECONDS, transport, str(event.get("session_id") or "") or None)
    try:
        if name == "next":
            return _next(desk, config.seat)
        seat = config.seat or desk.get("/v1/me")["name"]
        HANDLERS[name](event, desk, f"/v1/seats/{quote(seat, safe='')}")
    finally:
        desk.close()
    return None


def run(
    name: str,
    stdin: TextIO = sys.stdin,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run one hook; 0, or WAKE once `next` has printed a brief on stderr."""
    bound = NEXT_SECONDS + NEXT_GRACE if name == "next" else HOOK_SECONDS
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, bound)
    try:
        brief = _report(name, stdin, env, transport)
        if brief is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            print(brief, file=sys.stderr)
            return WAKE
    except _Deadline:
        if name != "statusline":
            print(f"ashigaru hook {name}: no answer in {bound:g}s", file=sys.stderr)
    except Exception as exc:  # a hook never fails Claude Code
        if name != "statusline":
            print(f"ashigaru hook {name}: {exc}", file=sys.stderr)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return 0
