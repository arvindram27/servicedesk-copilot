"""Configuration boundary and single-instance lock behaviour."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import app.config
import pytest
from app.config import PROJECT_ROOT, InstanceLock, InstanceLockError, Mode, Settings
from pydantic import ValidationError

WORKER = Path(__file__).with_name("lock_worker.py")


def _read_results(paths: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _wait_for_results(paths: list[Path], timeout: float) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _read_results(paths)
        if len(rows) >= len(paths):
            return rows
        time.sleep(0.02)
    return _read_results(paths)


def test_defaults_stay_on_loopback() -> None:
    settings = Settings()
    assert settings.listen_host == "127.0.0.1"
    assert settings.mode is Mode.CI_FAKE
    assert settings.allow_hosted_calls is False
    assert settings.writes_enabled is True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.com", "0:0:0:0:0:0:0:0"],
)
def test_non_loopback_listen_host_is_refused(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(listen_host=host)


def test_non_loopback_listen_host_from_environment_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SDC_LISTEN_HOST", "0.0.0.0")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST"])
def test_loopback_listen_host_is_accepted(host: str) -> None:
    assert Settings(listen_host=host).listen_host == host.lower()


def test_hosted_mode_needs_explicit_opt_in() -> None:
    with pytest.raises(ValidationError):
        Settings(mode=Mode.HOSTED)
    assert Settings(mode=Mode.HOSTED, allow_hosted_calls=True).mode is Mode.HOSTED


def test_second_worker_is_refused_on_the_instance_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / "instance.lock"
    first = InstanceLock(lock_path)
    first.acquire()
    assert first.held is True

    second = InstanceLock(lock_path)
    with pytest.raises(InstanceLockError):
        second.acquire()
    assert second.held is False

    first.release()


def test_released_lock_can_be_taken_again(tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / "instance.lock"
    first = InstanceLock(lock_path)
    first.acquire()
    first.release()

    second = InstanceLock(lock_path)
    second.acquire()
    assert second.held is True
    second.release()


def test_default_runtime_dir_is_inside_the_project() -> None:
    settings = Settings()
    assert settings.instance_lock_path == PROJECT_ROOT / "runtime" / "instance.lock"
    assert PROJECT_ROOT in settings.instance_lock_path.parents


def test_instance_lock_file_is_never_deleted(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    lock = InstanceLock(lock_path)
    lock.acquire()
    lock.release()
    assert lock_path.exists()


def test_lk1_same_holder_reacquires_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    lock = InstanceLock(lock_path)
    lock.acquire()
    lock.release()
    lock.acquire()
    assert lock.held is True
    lock.release()
    assert lock_path.exists()


def test_lk2_two_in_process_claims_yield_one_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    start = threading.Barrier(2)
    release_winner = threading.Event()
    outcomes: list[str] = []
    outcomes_guard = threading.Lock()

    def claim() -> None:
        lock = InstanceLock(lock_path)
        start.wait(timeout=5)
        try:
            lock.acquire()
        except InstanceLockError:
            with outcomes_guard:
                outcomes.append("refused")
        else:
            with outcomes_guard:
                outcomes.append("held")
            release_winner.wait(timeout=5)
            lock.release()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10
    while len(outcomes) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    release_winner.set()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["held", "refused"]
    assert lock_path.exists()


def test_lk3_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    gate = tmp_path / "gate"
    stop = tmp_path / "stop"

    # Leave a previously released file: the exact state the old marker check mishandled.
    warm = InstanceLock(lock_path)
    warm.acquire()
    warm.release()
    assert lock_path.exists()

    results = [tmp_path / "first.jsonl", tmp_path / "second.jsonl"]
    commands = [
        [
            sys.executable,
            str(WORKER),
            "--lock",
            str(lock_path),
            "--gate",
            str(gate),
            "--stop",
            str(stop),
            "--result",
            str(result),
            "--mode",
            "hold",
        ]
        for result in results
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]
    try:
        gate.write_text("go\n", encoding="utf-8")
        rows = _wait_for_results(results, timeout=20.0)
        assert len(rows) == 2, rows
        outcomes = sorted(str(row["result"]) for row in rows)
        assert outcomes == ["HELD", "REFUSED"], rows
        assert lock_path.exists()
    finally:
        stop.write_text("stop\n", encoding="utf-8")
        for process in processes:
            try:
                _, stderr = process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate(timeout=5)
                pytest.fail(f"lock worker timed out: {stderr!r}")
            assert process.returncode == 0, stderr

    assert lock_path.exists()


def test_lk4_crash_releases_the_lock_and_keeps_the_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    gate = tmp_path / "gate"
    stop = tmp_path / "stop"
    result = tmp_path / "crash.jsonl"
    process = subprocess.Popen(
        [
            sys.executable,
            str(WORKER),
            "--lock",
            str(lock_path),
            "--gate",
            str(gate),
            "--stop",
            str(stop),
            "--result",
            str(result),
            "--mode",
            "crash",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        gate.write_text("go\n", encoding="utf-8")
        _, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    rows = _read_results([result])
    assert rows and rows[0]["result"] == "HELD", rows
    assert lock_path.exists()

    recovered = InstanceLock(lock_path)
    recovered.acquire()
    assert recovered.held is True
    recovered.release()


def test_lk5_stale_held_record_does_not_block_a_new_claim(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    lock_path.write_text("state=held pid=99999", encoding="ascii")

    lock = InstanceLock(lock_path)
    lock.acquire()
    assert lock.held is True
    lock.release()
    assert lock_path.exists()


def test_lk6_refused_claim_succeeds_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    first = InstanceLock(lock_path)
    first.acquire()

    second = InstanceLock(lock_path)
    with pytest.raises(InstanceLockError):
        second.acquire()

    first.release()
    second.acquire()
    assert second.held is True
    second.release()


def test_lk7_acquire_write_failure_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "instance.lock"

    def boom(descriptor: int, payload: bytes) -> None:
        raise OSError(errno.EIO, "simulated write failure")

    monkeypatch.setattr(app.config, "_write_all", boom)
    lock = InstanceLock(lock_path)
    with pytest.raises(OSError):
        lock.acquire()
    assert lock.held is False
    assert app.config._HOLDERS == {}

    monkeypatch.undo()
    fresh = InstanceLock(lock_path)
    fresh.acquire()
    assert fresh.held is True
    fresh.release()


def test_lk8_release_write_failure_leaves_the_object_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "instance.lock"
    lock = InstanceLock(lock_path)
    lock.acquire()

    def boom(descriptor: int, payload: bytes) -> None:
        raise OSError(errno.EIO, "simulated release write failure")

    monkeypatch.setattr(app.config, "_write_all", boom)
    with pytest.raises(OSError):
        lock.release()
    assert lock.held is True
    assert app.config._HOLDERS != {}

    monkeypatch.undo()
    lock.release()
    assert lock.held is False
    assert app.config._HOLDERS == {}


def test_lk9_close_failure_still_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "instance.lock"
    lock = InstanceLock(lock_path)
    lock.acquire()
    descriptor = lock._handle
    assert descriptor is not None

    def boom(handle: int) -> None:
        raise OSError(errno.EIO, "simulated close failure")

    monkeypatch.setattr(app.config, "_close_descriptor", boom)
    with pytest.raises(OSError):
        lock.release()
    assert lock.held is False
    assert app.config._HOLDERS == {}

    monkeypatch.undo()
    os.close(descriptor)

    fresh = InstanceLock(lock_path)
    fresh.acquire()
    assert fresh.held is True
    fresh.release()


def test_lk10_unexpected_lock_errno_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "instance.lock"
    if app.config.msvcrt is not None:
        target_module, target_name = app.config.msvcrt, "locking"
    else:
        target_module, target_name = app.config.fcntl, "flock"

    def raiser(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EINVAL, "simulated unexpected errno")

    monkeypatch.setattr(target_module, target_name, raiser)
    lock = InstanceLock(lock_path)
    with pytest.raises(app.config.ConfigError):
        lock.acquire()
    assert lock.held is False
    assert app.config._HOLDERS == {}

    monkeypatch.undo()
    fresh = InstanceLock(lock_path)
    fresh.acquire()
    assert fresh.held is True
    fresh.release()
