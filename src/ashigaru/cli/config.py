"""Where the CLI finds the Desk and its key.

`~/.config/ashigaru/config.toml` (mode 0600, owned by the caller; anything
looser is refused) holds `url`, `api_key` and optionally `seat`. The
environment wins: `ASHIGARU_URL`, `ASHIGARU_API_KEY_FILE` (a podman secret or
mounted file), `ASHIGARU_API_KEY`, `ASHIGARU_SEAT`. A key never comes from argv.
`ASHIGARU_MD_COLUMNS` is the width `--markdown` fits its tables to.
"""

import ipaddress
import os
import stat
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MD_COLUMNS = 136
"""The width `--markdown` fits to when neither ASHIGARU_MD_COLUMNS nor a tmux pane says."""


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    url: str
    api_key: str = field(repr=False)
    seat: str | None = None


def config_path() -> Path:
    return Path.home() / ".config/ashigaru/config.toml"


def read_file(path: Path) -> dict[str, Any]:
    """The config file's table, or {} when there is none. Opened without following a symlink and
    checked on the open descriptor, so what is checked is what is read."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{path} is not a regular file")
        if info.st_uid != os.getuid():
            raise ConfigError(f"{path} is not owned by you; refusing it")
        if info.st_mode & 0o077:
            mode = oct(stat.S_IMODE(info.st_mode))
            raise ConfigError(f"{path} is mode {mode}, readable by others; chmod 600 it")
        try:
            return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc


def read_key_file(path: str) -> str:
    try:
        info = os.stat(path)
        with open(path, encoding="utf-8") as handle:
            key = handle.read().strip()
    except OSError as exc:
        raise ConfigError(f"ASHIGARU_API_KEY_FILE {path}: {exc.strerror}") from exc
    if info.st_mode & 0o077:
        print(f"ashigaru: warning: {path} is readable by others", file=sys.stderr)
    return key


def check_url(url: str) -> str:
    """https, or plain http to a loopback address only: the key rides in every request."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname:
        try:
            loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            return url.rstrip("/")
    raise ConfigError(f"the Desk URL must be https (http only to loopback), not {url!r}")


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    stored = read_file(config_path())
    url = env.get("ASHIGARU_URL") or stored.get("url")
    key_file = env.get("ASHIGARU_API_KEY_FILE")
    key = read_key_file(key_file) if key_file else env.get("ASHIGARU_API_KEY") or stored.get("api_key")
    seat = env.get("ASHIGARU_SEAT") or stored.get("seat")
    if not isinstance(url, str) or not url:
        raise ConfigError(f"no Desk URL: set ASHIGARU_URL or url in {config_path()}")
    if not isinstance(key, str) or not key:
        raise ConfigError(f"no API key: set ASHIGARU_API_KEY or api_key in {config_path()}")
    return Config(url=check_url(url), api_key=key, seat=seat if isinstance(seat, str) and seat else None)


def markdown_columns(env: Mapping[str, str] | None = None) -> int:
    """The width of the terminal the Markdown is read in, which is not this process's: the chat's own
    pane. ASHIGARU_MD_COLUMNS, else the Claude Code tmux pane, else MD_COLUMNS."""
    env = os.environ if env is None else env
    if (columns := env.get("ASHIGARU_MD_COLUMNS", "")).isdigit():
        return int(columns)
    for server in (["-L", "claude-agent"], []):
        try:
            shown = subprocess.run(
                ["tmux", *server, "display", "-p", "#{pane_width}"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if shown.isdigit():
            return int(shown)
    return MD_COLUMNS
