"""Claude Code hooks: report the seat's turn, task and usage to the Desk. They replace the
seat-state.json hooks and keep their rules.

  session-start   SessionStart. Registers the session with POST
                  /v1/seats/{name}/session (session_id, source), which makes it
                  the seat's current one and turns the seat idle (startup and
                  clear also drop the task). A resume of a session the Desk
                  ended (409 stale-session) prints a notice on stdout, which
                  SessionStart adds to the session's context: stop work on any
                  earlier assignment. A registration that lands is marked on
                  tmpfs, for `next`. A Desk without the route (404, 405) gets
                  the old PATCH of turn idle (clear also task null). compact can
                  come mid-turn and changes nothing.
  prompt-submit   UserPromptSubmit. turn active. A prompt a person typed is sent
                  as the task, which the Desk keeps only from the session's
                  first; turns Claude Code starts itself (a
                  <cross-session-message>, a <task-notification>, a scheduled
                  task firing) send none.
  stop            Stop, StopFailure and the idle_prompt Notification: turn
                  idle. idle_prompt leaves alone a turn that started too
                  recently to be the one it reports on. Stop also sends the
                  seat's final message as waiting_on, trimmed to WAITING_LENGTH.
                  A Stop answered 409 stale-session (the Desk ended this
                  session, as when an assign took its item from it) prints
                  the stale notice on stderr and exits 2, which tells the
                  model before it stops; not again on the Stop that causes.
  next            SessionStart (startup, clear, resume), registered async with
                  asyncRewake: waits up to NEXT_SECONDS for the Desk to hand
                  this free seat its next queued item, then prints the brief
                  on stderr and exits 2, which wakes the idle session with it.
                  Its asks are what keeps an idle seat's lease, so while the
                  seat cannot take work (401, 403, 404, or a 409 that is not
                  busy, held or stale-session) it checks in again every
                  NEXT_CHECK_IN; a busy or held seat is asked again with
                  backoff. On a 409 stale-session it stops silently if the
                  Desk names a newer session as the seat's (a /clear), else
                  exits 2 with a notice that the Desk ended this session. A
                  startup or clear session that session-start did not mark
                  registered (its POST ran out of time) is not the older one:
                  the Desk names the session before it, so `next` registers
                  this one once and asks again.
  statusline      The status line command, not a hook: model, context, session
                  tokens and running cost to POST /v1/seats/{name}/usage. It
                  runs on every refresh, so it posts at most once per
                  STATUSLINE_EVERY per session, keeping its state on tmpfs,
                  and prints nothing, not even its failures.

Every request names the session the hook runs for as X-Ashigaru-Session, which
is all the Desk fences on: nothing is kept on disk but tmpfs state, the status
line's throttle and the marks of sessions registered.

A hook must never stand between a person and their prompt: the whole run is
bounded at HOOK_SECONDS, every failure (no config, no network, a refusal) goes
to stderr, and the exit is 0. `next` alone runs in the background, bounded at
NEXT_SECONDS, and exits 2 only to hand over a brief; `stop` exits 2 only for a
stale notice. Nothing goes to stdout, which UserPromptSubmit would add to the
conversation.
"""

import dataclasses
import json
import os
import re
import signal
import stat
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import FrameType
from typing import Any, TextIO
from urllib.parse import quote

import httpx

from .client import Desk, DeskError, Send, Unreachable
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
NEXT_REFUSED = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND})
# Between check-ins of a seat that cannot take work: each renews its lease, which lapses after 10
# minutes idle (desk.model.LEASE), so the seat stays on the board.
NEXT_CHECK_IN = 180.0
# The 409 codes that pass: the seat is mid-wrap or held, so ask again with the backoff.
NEXT_BUSY = frozenset({"busy", "held"})
# The 409 code for a session the Desk has ended or superseded.
STALE = "stale-session"
# The sources a session is registered for; compact is not a new session.
SESSION_SOURCES = frozenset({"startup", "resume", "clear"})
# The sources that start a session, which `next` registers when session-start did not.
NEW_SOURCES = frozenset({"startup", "clear"})
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
# What a session id may not carry into a tmpfs file name.
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class _Deadline(Exception):
    pass


def _alarm(signum: int, frame: FrameType | None) -> None:
    raise _Deadline


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


def _start(event: dict[str, Any], desk: Desk, path: str, env: Mapping[str, str]) -> None:
    source = event.get("source")
    if source == "compact":
        return
    session = event.get("session_id")
    if source in SESSION_SOURCES and isinstance(session, str) and session:
        try:
            desk.post(f"{path}/session", {"session_id": session, "source": source})
        except DeskError as error:
            if error.status == HTTPStatus.CONFLICT and error.code == STALE:
                print(_stale(error.detail))
                return
            if error.status not in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED):
                raise
        else:
            _mark(session, env)
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
    """Turn active; a typed prompt is sent as the task, which the Desk keeps only from the session's
    first (it drops the task on startup and /clear)."""
    prompt = str(event.get("prompt", "")).strip()
    if prompt.startswith(HARNESS_MARKS) or not prompt or _scheduled(event, prompt):
        desk.request("PATCH", path, body={"turn": "active"})
        return
    desk.request("PATCH", path, body={"turn": "active", "task": prompt[:TASK_LENGTH]})


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


def _stop(event: dict[str, Any], desk: Desk, path: str) -> str | None:
    """Turn idle. A Stop the Desk answers stale-session (it ended this session: its item was
    reassigned, or its lease lapsed) returns the notice, which blocks the stop and tells the model,
    once: not on the Stop that notice itself caused, and not when a newer session holds the seat."""
    kind = event.get("hook_event_name")
    if kind == "Notification":
        seat = desk.get(path)
        if seat["turn"] == "idle" or _started_within(seat, IDLE_NOTICE_AFTER):
            return None
    body: dict[str, Any] = {"turn": "idle"}
    if kind in (None, "Stop") and (waiting := _waiting_on(event)) is not None:
        body["waiting_on"] = waiting
    try:
        desk.request("PATCH", path, body=body)
    except DeskError as error:
        if error.status != HTTPStatus.CONFLICT or error.code != STALE or kind not in (None, "Stop"):
            raise
        stale = _Stale(error.detail, error.session)
        if event.get("stop_hook_active") or stale.superseded(str(event.get("session_id") or "") or None):
            raise
        return _stale(error.detail)
    return None


class _Refused(Exception):
    """The Desk said this seat cannot take work now: check in again after NEXT_CHECK_IN."""


class _Stale(Exception):
    """The Desk said this session is over; `detail` is why, `session` the seat's current one it
    named (None once the Desk ended the seat's session)."""

    def __init__(self, detail: str, session: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.session = session

    def superseded(self, own: str | None) -> bool:
        """True when the Desk named another session as the seat's: a newer one (a /clear, a restart)
        took the seat over. False when it named none, which is an ended session."""
        return self.session is not None and self.session != own


@dataclasses.dataclass(frozen=True)
class _Rejoin:
    """A new session (`source` startup or clear) its `next` registers if session-start did not (a1709:
    its POST ran out of time)."""

    source: str
    session: str
    state: Mapping[str, str]

    @property
    def body(self) -> dict[str, str]:
        return {"session_id": self.session, "source": self.source}

    def lost(self, stale: _Stale) -> bool:
        """The Desk never got this session: it names another one, and session-start left no mark. The
        mark is read at that answer, not at the start, when session-start may still be posting."""
        return stale.superseded(self.session) and _unmarked(self.session, self.state)


def _ask(desk: Desk, method: str, path: str, deadline: float, body: Any = None, **params: Any) -> Any:
    """One request, retried with backoff while the Desk is down, restarting or throttling (5xx, 429,
    a 409 busy or held), until `deadline` on the monotonic clock. Raises _Refused on NEXT_REFUSED or any
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
                body=body,
                send=Send(timeout=wait + NEXT_GRACE),
            )
        except (DeskError, Unreachable) as error:
            if isinstance(error, DeskError):
                if error.status == HTTPStatus.CONFLICT:
                    if error.code == STALE:
                        raise _Stale(error.detail, error.session) from error
                    if error.code not in NEXT_BUSY:
                        raise _Refused from error
                elif error.status in NEXT_REFUSED:
                    raise _Refused from error
        time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
        pause = min(pause * 2, NEXT_BACKOFF)


def _next(
    desk: Desk,
    seat: str | None,
    session: str | None = None,
    source: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """The brief of the item the Desk hands this seat, or None once the wait runs out. Every request
    ends before NEXT_SECONDS, so no claim lands on a hook already gone. While the seat cannot take work
    it checks in every NEXT_CHECK_IN, which keeps its lease. On a stale-session answer it returns the
    notice for the model, unless the answer names another session as the seat's: a newer session on
    this seat (`session` is this one) took over, and it stops silently. `source` is that of a new
    session (startup, clear), which the first such answer registers if session-start left no mark
    (`_Rejoin`), and it asks again."""
    state = os.environ if env is None else env
    deadline = time.monotonic() + NEXT_SECONDS
    time.sleep(NEXT_SETTLE)
    name = seat
    rejoin = _Rejoin(source, session, state) if source and session else None
    try:
        while True:
            try:
                name = name or _ask(desk, "GET", "/v1/me", deadline)["name"]
                found = _ask(desk, "POST", f"/v1/seats/{quote(name, safe='')}/next", deadline, wait=NEXT_WAIT)
            except _Refused:
                time.sleep(min(NEXT_CHECK_IN, max(0.0, deadline - time.monotonic())))
                continue
            except _Stale as stale:
                if rejoin is None or name is None or not rejoin.lost(stale):
                    raise stale
                _ask(desk, "POST", f"/v1/seats/{quote(name, safe='')}/session", deadline, body=rejoin.body)
                _mark(rejoin.session, rejoin.state)
                rejoin = None
                continue
            brief = found.get("brief") if isinstance(found, dict) else None
            if isinstance(brief, str) and brief.strip():
                return brief
            time.sleep(NEXT_PAUSE)
    except _Stale as stale:
        return None if stale.superseded(session) else _stale(stale.detail)
    except _Deadline:
        return None


def _state(env: Mapping[str, str]) -> Path:
    """The hooks' tmpfs directory, private to the user."""
    runtime = env.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "ashigaru" if runtime else Path(f"/dev/shm/ashigaru-{os.getuid()}")


def usage_state(session: str, env: Mapping[str, str]) -> Path:
    """Where the status line keeps its throttle and transcript offset: tmpfs, since it is looked at
    on every refresh."""
    return _state(env) / f"usage-{UNSAFE.sub('_', session)}.json"


def registered_mark(session: str, env: Mapping[str, str]) -> Path:
    """The mark that the Desk registered this session: the `next` of a session without one
    registers it."""
    return _state(env) / f"registered-{UNSAFE.sub('_', session)}"


def _mark(session: str, env: Mapping[str, str]) -> None:
    """Mark the session registered, in a directory only this user can write."""
    mark = registered_mark(session, env)
    mark.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _private(mark.parent):
        mark.touch()


def _unmarked(session: str, env: Mapping[str, str]) -> bool:
    """True when the session is surely not marked registered. A directory another user could write
    proves nothing, so it is False there, and `next` keeps its old answer."""
    mark = registered_mark(session, env)
    try:
        return _private(mark.parent) and not mark.exists()
    except FileNotFoundError:
        return True
    except OSError:
        return False


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
    for old in [*state_path.parent.glob("usage-*.json"), *state_path.parent.glob("registered-*")]:
        if now - old.stat().st_mtime > STATE_KEPT.total_seconds():
            old.unlink(missing_ok=True)
    return False


Handler = Callable[[dict[str, Any], Desk, str], str | None]
HANDLERS: dict[str, Handler] = {
    "prompt-submit": _prompt,
    "stop": _stop,
    "statusline": _statusline,
}


def _report(name: str, stdin: TextIO, env: Mapping[str, str] | None, transport: Any) -> str | None:
    """Run the hook; `next` returns the brief it was handed, `stop` the notice of a session the Desk
    ended."""
    event = {} if stdin.isatty() else json.loads(stdin.read() or "{}")
    if not isinstance(event, dict):
        raise ValueError("the hook event is not a JSON object")
    state = os.environ if env is None else env
    if name == "statusline" and _throttled(event, state):
        return None
    config = load(env)
    session = str(event.get("session_id") or "") or None
    desk = Desk(config, HOOK_SECONDS, transport, session)
    try:
        if name == "next":
            source = event.get("source")
            return _next(desk, config.seat, session, source if source in NEW_SOURCES else None, state)
        path = f"/v1/seats/{quote(config.seat or desk.get('/v1/me')['name'], safe='')}"
        if name == "session-start":
            _start(event, desk, path, state)
            return None
        return HANDLERS[name](event, desk, path)
    finally:
        desk.close()


def run(
    name: str,
    stdin: TextIO = sys.stdin,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run one hook; 0, or WAKE once `next` has printed a brief, or `stop` a stale notice, on stderr."""
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
