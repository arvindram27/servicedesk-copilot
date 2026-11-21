"""Spawn-only helper for the cross-process instance-lock tests.

It never forks. Each run is a fresh interpreter, so it cannot inherit a parent's
lock claim through a shared open file description.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import InstanceLock, InstanceLockError  # noqa: E402

GATE_TIMEOUT_S = 15.0
HOLD_TIMEOUT_S = 15.0
POLL_INTERVAL_S = 0.01


def wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


def append_result(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Instance lock worker")
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--gate", required=True, type=Path)
    parser.add_argument("--stop", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--mode", choices=("hold", "crash"), default="hold")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    outcome: dict[str, Any] = {"pid": os.getpid(), "result": "REFUSED"}
    lock = InstanceLock(args.lock)
    if not wait_for(args.gate, GATE_TIMEOUT_S):
        append_result(args.result, outcome)
        return 0
    try:
        lock.acquire()
    except InstanceLockError:
        append_result(args.result, outcome)
        return 0
    outcome["result"] = "HELD"
    # Report before waiting, so the parent observes both outcomes while this
    # process still holds the lock and can then signal stop promptly.
    append_result(args.result, outcome)
    if args.mode == "crash":
        # Crash on purpose: no release, so only the OS can drop the lock.
        os._exit(0)
    wait_for(args.stop, HOLD_TIMEOUT_S)
    lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
