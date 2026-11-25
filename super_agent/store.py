"""Atomic persistence and event log store for Super-Agent runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from super_agent.models import RunState, utc_now_iso


class StoreError(RuntimeError):
    """Raised when reading, writing, or validating persisted run data fails."""


def default_runs_root(env: Mapping[str, str] | None = None) -> Path:
    """Return the default root directory for persisted Super-Agent runs."""
    source = os.environ if env is None else env
    override = (source.get("SUPER_AGENT_HOME") or "").strip()
    if override:
        return Path(override) / "runs"
    return Path.home() / ".super-agent" / "runs"


def _validate_single_name(name: str, label: str) -> str:
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or any(ch in name for ch in "/\\:\x00")
        or any(ord(ch) < 32 for ch in name)
        or name.strip() != name
        or Path(name).name != name
        or (os.name == "nt" and name.endswith((".", " ")))
    ):
        raise StoreError(f"{label} must be a safe single directory name: {name!r}")
    return name


@dataclass
class AttemptPaths:
    attempt_dir: Path
    prompt_path: Path
    result_path: Path
    last_message_path: Path
    log_path: Path
    exit_code_path: Path
    patch_path: Path
    worktrees_root: Path


class RunStore:
    """Manages per-run directories, atomic state.json persistence, and events.jsonl."""

    def __init__(self, base_dir: Path | None = None) -> None:
        root = Path(base_dir).expanduser() if base_dir is not None else default_runs_root()
        self.base_dir: Path = root

    def run_dir(self, run_id: str) -> Path:
        """Return the validated directory path for a run."""
        safe_id = _validate_single_name(run_id, "run_id")
        return self.base_dir / safe_id

    def attempt_paths(self, run_id: str, attempt_id: str) -> AttemptPaths:
        """Return artifact paths for a task attempt and ensure its directory exists."""
        rdir = self.run_dir(run_id)
        safe_attempt = _validate_single_name(attempt_id, "attempt_id")
        attempt_dir = rdir / "attempts" / safe_attempt
        worktrees_root = rdir / "worktrees"
        try:
            attempt_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(f"Cannot create attempt directory {attempt_dir}: {exc}") from exc
        return AttemptPaths(
            attempt_dir=attempt_dir,
            prompt_path=attempt_dir / "prompt.md",
            result_path=attempt_dir / "result.json",
            last_message_path=attempt_dir / "last_message.txt",
            log_path=attempt_dir / "output.log",
            exit_code_path=attempt_dir / "exit_code.json",
            patch_path=attempt_dir / "changes.patch",
            worktrees_root=worktrees_root,
        )

    def integration_paths(self, run_id: str) -> tuple[Path, Path]:
        """Return (worktrees_root, integration_dir / 'final.patch') for a run."""
        rdir = self.run_dir(run_id)
        worktrees_root = rdir / "worktrees"
        integration_dir = rdir / "integration"
        try:
            integration_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(
                f"Cannot create integration directory {integration_dir}: {exc}"
            ) from exc
        return worktrees_root, integration_dir / "final.patch"

    def create_run(self, state: RunState) -> RunState:
        """Initialize run directories, atomically persist state.json, and log run_created."""
        rdir = self.run_dir(state.run_id)
        state_path = rdir / "state.json"
        if state_path.exists():
            raise StoreError(f"Run already exists: {state.run_id!r}")
        try:
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / "attempts").mkdir(parents=True, exist_ok=True)
            (rdir / "worktrees").mkdir(parents=True, exist_ok=True)
            (rdir / "integration").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(f"Cannot create run directories in {rdir}: {exc}") from exc

        state.run_dir = str(rdir)
        self.save_run(state)
        self.append_event(
            state.run_id,
            "run_created",
            {
                "host": state.host,
                "workspace": state.workspace,
                "isolation_mode": state.isolation_mode.value,
                "task_count": len(state.tasks),
            },
        )
        return state

    def save_run(self, state: RunState) -> None:
        """Update updated_at and atomically write state.json."""
        rdir = self.run_dir(state.run_id)
        state_path = rdir / "state.json"
        tmp_path: Path | None = None
        try:
            rdir.mkdir(parents=True, exist_ok=True)
            state.updated_at = utc_now_iso()
            serialized = json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=rdir,
                prefix=".state-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, state_path)
            tmp_path = None
        except OSError as exc:
            raise StoreError(f"Failed to save run state for {state.run_id!r}: {exc}") from exc
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def load_run(self, run_id: str) -> RunState:
        """Load and deserialize state.json for a run; raise StoreError if missing or corrupt."""
        rdir = self.run_dir(run_id)
        state_path = rdir / "state.json"
        if not state_path.is_file():
            raise StoreError(f"Run state not found for {run_id!r}: {state_path}")
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise StoreError(f"Run state for {run_id!r} must be a JSON object")
            return RunState.from_dict(raw)
        except StoreError:
            raise
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise StoreError(f"Corrupt run state for {run_id!r}: {exc}") from exc

    def list_runs(self) -> list[dict[str, Any]]:
        """List persisted runs sorted by updated_at descending."""
        if not self.base_dir.is_dir():
            return []
        runs: list[dict[str, Any]] = []
        try:
            entries = list(self.base_dir.iterdir())
        except OSError as exc:
            raise StoreError(f"Cannot list runs directory {self.base_dir}: {exc}") from exc

        for entry in entries:
            if not entry.is_dir():
                continue
            if not (entry / "state.json").is_file():
                continue
            try:
                state = self.load_run(entry.name)
            except StoreError:
                continue
            runs.append(
                {
                    "run_id": state.run_id,
                    "status": state.status.value,
                    "host": state.host,
                    "workspace": state.workspace,
                    "isolation_mode": state.isolation_mode.value,
                    "task_count": len(state.tasks),
                    "created_at": state.created_at,
                    "updated_at": state.updated_at,
                    "run_dir": state.run_dir,
                }
            )
        runs.sort(key=lambda item: (item["updated_at"], item["run_id"]), reverse=True)
        return runs

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a structured event record to events.jsonl and return it."""
        rdir = self.run_dir(run_id)
        events_path = rdir / "events.jsonl"
        event: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "run_id": run_id,
            "event_type": event_type,
            "payload": dict(payload) if payload is not None else {},
        }
        line = json.dumps(event, ensure_ascii=False) + "\n"
        try:
            rdir.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        except OSError as exc:
            raise StoreError(f"Failed to append event for {run_id!r}: {exc}") from exc
        return event

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        """Read all structured event records from events.jsonl in append order."""
        rdir = self.run_dir(run_id)
        events_path = rdir / "events.jsonl"
        if not events_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            text = events_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StoreError(f"Failed to read events for {run_id!r}: {exc}") from exc
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise StoreError(
                    f"Corrupt event JSON on line {lineno} of {events_path}: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise StoreError(
                    f"Event on line {lineno} of {events_path} must be a JSON object"
                )
            events.append(parsed)
        return events
