"""Detached subprocess runner that captures logs and atomically records exit status."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _atomic_write_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp",
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(target)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: _proc_runner.py <spec.json>", file=sys.stderr)
        return 2
    spec_path = Path(argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cmd: list[str] = spec["argv"]
    cwd = spec["cwd"]
    log_path = Path(spec["log_path"])
    exit_code_path = Path(spec["exit_code_path"])
    stdin_path = Path(spec["stdin_path"]) if spec.get("stdin_path") else None

    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    child_pid: int | None = None
    exit_code: int = 1
    error: str | None = None

    stdin_handle = None
    try:
        if stdin_path is not None:
            stdin_handle = stdin_path.open("rb")
        with log_path.open("wb") as log_handle:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=False,
            )
            child_pid = int(proc.pid)
            exit_code = int(proc.wait())
    except BaseException as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    finally:
        if stdin_handle is not None:
            stdin_handle.close()
        _atomic_write_json(
            exit_code_path,
            {
                "runner_pid": os.getpid(),
                "child_pid": child_pid,
                "exit_code": exit_code,
                "started_at": started_at,
                "ended_at": _utc_now(),
                "error": error,
            },
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
