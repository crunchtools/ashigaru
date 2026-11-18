"""Claude Code hooks: report the seat's turn, task and usage to the Desk. They replace the
seat-state.json hooks and keep their rules.

  session-start   SessionStart. startup and resume are not mid-turn: turn idle.
                  clear also drops the task. compact can come mid-turn and
                  changes nothing.
  prompt-submit   UserPromptSubmit. turn active. A prompt a person typed, the
                  session's first, becomes the task; turns Claude Code starts
                  itself (a <cross-session-message>, a <task-notification>)
                  leave the task alone.
  stop            Stop, StopFailure and the idle_prompt Notification: turn
                  idle. idle_prompt leaves alone a turn that started too
                  recently to be the one it reports on.
  statusline      The status line command, not a hook: model, context, session
                  tokens and running cost to POST /v1/seats/{name}/usage. It
                  runs on every refresh, so it posts at most once per
                  STATUSLINE_EVERY per session, keeping its state on tmpfs,
                  and prints nothing, not even its failures.

A hook must never stand between a person and their prompt: the whole run is
bounded at HOOK_SECONDS, every failure (no config, no network, a refusal) goes
to stderr, and the exit is 0. Nothing goes to stdout, which UserPromptSubmit
would add to the conversation.
"""

import json
import os
import re
import signal
import stat
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, TextIO
from urllib.parse import quote

import httpx

from .client import Desk, DeskError
from .config import load

EVENTS = ("session-start", "prompt-submit", "stop", "statusline")
HOOK_SECONDS = 2.0
TASK_LENGTH = 120
# Claude Code's messageIdleNotifThresholdMs: idle_prompt comes at least this
# long after the turn it reports on ended.
IDLE_NOTICE_AFTER = timedelta(seconds=60)
# What Claude Code puts first in a prompt it submits on its own.
HARNESS_MARKS = ("<cross-session-message", "<task-notification")
STATUSLINE_EVERY = 30.0
# The most of a transcript one status line run reads; the rest waits for the next.
TRANSCRIPT_CHUNK = 4 << 20
# A session's state is dropped this long after its last post, so tmpfs holds only live sessions.
STATE_KEPT = timedelta(days=1)
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
LANE_BILLING = {"vertex": "vertex", "dolza-max": "max", "exedore-max": "max"}


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


def _start(event: dict[str, Any], desk: Desk, path: str) -> None:
    source = event.get("source")
    if source == "compact":
        return
    if source == "clear":
        task_marker().unlink(missing_ok=True)
    desk.request(
        "PATCH", path, body={"turn": "idle", "task": None} if source == "clear" else {"turn": "idle"}
    )


def _prompt(event: dict[str, Any], desk: Desk, path: str) -> None:
    prompt = str(event.get("prompt", "")).strip()
    session = str(event.get("session_id", ""))
    marker = task_marker()
    try:
        kept = marker.read_text().strip() == session
    except OSError:
        kept = False
    if prompt.startswith(HARNESS_MARKS) or kept or not prompt:
        desk.request("PATCH", path, body={"turn": "active"})
        return
    desk.request("PATCH", path, body={"turn": "active", "task": prompt[:TASK_LENGTH]})
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(session + "\n")


def _stop(event: dict[str, Any], desk: Desk, path: str) -> None:
    if event.get("hook_event_name") == "Notification":
        seat = desk.get(path)
        if seat["turn"] == "idle" or _started_within(seat, IDLE_NOTICE_AFTER):
            return
    desk.request("PATCH", path, body={"turn": "idle"})


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
    try:
        lane = desk.get(path).get("lane")
    except DeskError:  # no seat row yet: this report creates it, without a lane
        lane = None
    report = {
        "session_id": str(event["session_id"]),
        "model": model.get("id") if isinstance(model.get("id"), str) else None,
        "billing": LANE_BILLING.get(lane),
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


def _report(name: str, stdin: TextIO, env: Mapping[str, str] | None, transport: Any) -> None:
    event = {} if stdin.isatty() else json.loads(stdin.read() or "{}")
    if not isinstance(event, dict):
        raise ValueError("the hook event is not a JSON object")
    if name == "statusline" and _throttled(event, os.environ if env is None else env):
        return
    config = load(env)
    desk = Desk(config, timeout=HOOK_SECONDS, transport=transport)
    try:
        seat = config.seat or desk.get("/v1/me")["name"]
        HANDLERS[name](event, desk, f"/v1/seats/{quote(seat, safe='')}")
    finally:
        desk.close()


def run(
    name: str,
    stdin: TextIO = sys.stdin,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run one hook; always 0."""
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, HOOK_SECONDS)
    try:
        _report(name, stdin, env, transport)
    except _Deadline:
        if name != "statusline":
            print(f"ashigaru hook {name}: no answer in {HOOK_SECONDS:g}s", file=sys.stderr)
    except Exception as exc:  # a hook never fails Claude Code
        if name != "statusline":
            print(f"ashigaru hook {name}: {exc}", file=sys.stderr)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return 0
