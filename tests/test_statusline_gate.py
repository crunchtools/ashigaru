"""The status line's shell-side gate in docs/desk-cli.md, run as documented."""

import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

DOC = Path(__file__).parent.parent / "docs" / "desk-cli.md"
SESSION = "abc-123_X"

Run = Callable[..., bool]


def snippet() -> str:
    text = DOC.read_text()
    after = text[text.index("`ashigaru hook statusline` feeds") :]
    match = re.search(r"```bash\n(.*?)```", after, re.S)
    assert match
    return match.group(1)


@pytest.fixture
def run(tmp_path: Path) -> Run:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    fake = bin_dir / "ashigaru"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncat >/dev/null\n')
    fake.chmod(0o755)
    runtime = tmp_path / "run"
    (runtime / "ashigaru").mkdir(parents=True)

    def go(session: str = SESSION) -> bool:
        log.unlink(missing_ok=True)
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "XDG_RUNTIME_DIR": str(runtime),
        }
        status = f'{{"session_id": "{session}", "model": {{}}}}'
        subprocess.run(["bash", "-c", snippet() + "\nwait"], input=status, text=True, env=env, check=True)
        return log.exists()

    return go


def state(tmp_path: Path, name: str, age: float = 0) -> None:
    path = tmp_path / "run" / "ashigaru" / f"usage-{name}.json"
    path.write_text("{}")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def test_runs_without_state_file(run: Run) -> None:
    assert run()


def test_skips_with_fresh_state_file(run: Run, tmp_path: Path) -> None:
    state(tmp_path, SESSION)
    assert not run()


def test_runs_with_stale_state_file(run: Run, tmp_path: Path) -> None:
    state(tmp_path, SESSION, age=60)
    assert run()


def test_odd_session_id_maps_to_sanitized_name(run: Run, tmp_path: Path) -> None:
    state(tmp_path, "a_b_c_d")
    assert not run("a/b c.d")
