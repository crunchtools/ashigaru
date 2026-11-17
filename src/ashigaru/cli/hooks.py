"""Claude Code hooks: report the seat's turn and task to the Desk. They replace the seat-state.json
hooks and keep their rules.

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

A hook must never stand between a person and their prompt: the whole run is
bounded at HOOK_SECONDS, every failure (no config, no network, a refusal) goes
to stderr, and the exit is 0. Nothing goes to stdout, which UserPromptSubmit
would add to the conversation.
"""

import json
import signal
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, TextIO
from urllib.parse import quote

import httpx

from .client import Desk
from .config import load

EVENTS = ("session-start", "prompt-submit", "stop")
HOOK_SECONDS = 2.0
TASK_LENGTH = 120
# Claude Code's messageIdleNotifThresholdMs: idle_prompt comes at least this
# long after the turn it reports on ended.
IDLE_NOTICE_AFTER = timedelta(seconds=60)
# What Claude Code puts first in a prompt it submits on its own.
HARNESS_MARKS = ("<cross-session-message", "<task-notification")


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


HANDLERS = {"session-start": _start, "prompt-submit": _prompt, "stop": _stop}


def _report(name: str, stdin: TextIO, env: Mapping[str, str] | None, transport: Any) -> None:
    event = {} if stdin.isatty() else json.loads(stdin.read() or "{}")
    if not isinstance(event, dict):
        raise ValueError("the hook event is not a JSON object")
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
        print(f"ashigaru hook {name}: no answer in {HOOK_SECONDS:g}s", file=sys.stderr)
    except Exception as exc:  # a hook never fails Claude Code
        print(f"ashigaru hook {name}: {exc}", file=sys.stderr)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return 0
