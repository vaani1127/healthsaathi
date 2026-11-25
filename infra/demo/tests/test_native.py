"""Native demo process handling: a pid file is acted on only while its process is the one the
demo started. Windows reuses pid numbers, so a stale pid file can point at an unrelated program."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hs_demo import native

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


@pytest.fixture
def pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(native, "PIDS", tmp_path / "pids")
    monkeypatch.setattr(native, "LOGS", tmp_path / "logs")
    (tmp_path / "pids").mkdir()
    return tmp_path / "pids"


@pytest.fixture
def terminated(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Records terminate calls instead of making them, so no real process can be hit."""
    calls: list[int] = []
    monkeypatch.setattr(native, "terminate", calls.append)
    return calls


def finished_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write(pids: Path, name: str, content: object) -> Path:
    path = pids / f"{name}.pid"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def test_a_stale_pid_file_is_removed_without_stopping_anything(
    pids: Path, terminated: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(pids, "web", {"pid": finished_pid(), "args": SLEEPER})
    assert not native.alive("web")
    native.kill("web")
    assert terminated == [] and not path.exists()
    assert "web not running (stale pid file removed)" in capsys.readouterr().out


def test_a_pid_now_used_by_another_program_is_left_alone(
    pids: Path, terminated: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    # This test's own process is alive but is not the command the pid file records.
    path = write(pids, "api", {"pid": os.getpid(), "args": SLEEPER})
    assert not native.alive("api")
    native.kill("api")
    assert terminated == [] and not path.exists()
    assert "is now another program, left alone" in capsys.readouterr().out


def test_an_old_pid_file_with_only_a_number_is_never_acted_on(
    pids: Path, terminated: list[int]
) -> None:
    path = write(pids, "worker", str(os.getpid()))
    assert native.read_pid("worker") == (os.getpid(), None)
    assert not native.alive("worker")
    native.kill("worker")
    assert terminated == [] and not path.exists()


def test_the_demos_own_process_is_seen_and_stopped(
    pids: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    native.start("sleeper", SLEEPER, Path.cwd(), dict(os.environ))
    pid, args = native.read_pid("sleeper") or (0, None)
    assert args == SLEEPER
    try:
        assert native.alive("sleeper")
        native.kill("sleeper")
        assert "stopped sleeper" in capsys.readouterr().out
        assert native.command_line(pid) is None, "the process has ended"
        assert not (pids / "sleeper.pid").exists()
    finally:
        if native.command_line(pid) is not None:
            native.terminate(pid)


def test_no_pid_file_means_not_alive(pids: Path) -> None:
    assert not native.alive("api")
    assert native.main(["alive", "api"]) == 1
