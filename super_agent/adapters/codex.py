"""Codex CLI single-host session adapter launching standalone `codex exec` threads."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from super_agent.adapters.base import (
    AdapterCapabilities,
    AdapterError,
    AdapterHealth,
    CancelResult,
    HostSessionAdapter,
    WindowPollResult,
    WindowStartRequest,
    is_pid_alive,
    read_exit_record,
    resolve_command_argv,
    spawn_managed_cli,
    terminate_pid_tree,
)
from super_agent.models import SessionHandle


def _env_get(env: Mapping[str, str], key: str) -> str | None:
    if key in env:
        return env[key]
    if os.name == "nt":
        target = key.lower()
        for k, v in env.items():
            if k.lower() == target:
                return v
    return None


def _normalize_resolve_env(env: Mapping[str, str] | None) -> Mapping[str, str] | None:
    if env is None:
        return None
    normalized = dict(env)
    path_val = _env_get(env, "PATH")
    normalized["PATH"] = path_val if path_val is not None else ""
    return normalized


def _probe_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    merged = dict(env)
    if os.name == "nt":
        upper_keys = {k.upper() for k in merged}
        for key in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR"):
            if key not in upper_keys and key in os.environ:
                merged[key] = os.environ[key]
    return merged


def _display_executable(argv: list[str]) -> str:
    if (
        os.name == "nt"
        and len(argv) >= 4
        and Path(argv[0]).name.lower() == "cmd.exe"
        and argv[1] == "/d"
        and argv[2] == "/c"
    ):
        return argv[3]
    if os.name == "nt" and "-File" in argv:
        idx = argv.index("-File")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return argv[0]


def _extract_thread_id(log_path: Path | None) -> str | None:
    if log_path is None or not log_path.is_file():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        tid = event.get("thread_id")
        if not tid and isinstance(event.get("thread"), dict):
            tid = event["thread"].get("id")
        if not tid:
            tid = event.get("session_id")
        if tid:
            return str(tid)
    return None


def _resolve_exit_code_path(handle: SessionHandle) -> Path | None:
    raw = handle.metadata.get("exit_code_path")
    if raw:
        return Path(str(raw))
    if handle.log_path:
        return Path(handle.log_path).with_name("exit_code.json")
    return None


class CodexAdapter(HostSessionAdapter):
    """Launches independent Codex threads via standalone `codex exec` (never spawn_agent)."""

    host_name = "codex"

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            ui_native_window=False,
            resumable=True,
            supports_model=True,
            supports_cancel=True,
            transport="cli-managed",
        )

    def health_check(self, env: Mapping[str, str] | None = None) -> AdapterHealth:
        norm_env = _normalize_resolve_env(env)
        try:
            argv = resolve_command_argv("codex", ["--version"], norm_env)
        except AdapterError as exc:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail=str(exc),
                executable=None,
            )

        executable = _display_executable(argv)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                shell=False,
                env=_probe_env(norm_env),
            )
        except subprocess.TimeoutExpired:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail="codex --version timed out after 5s",
                executable=executable,
            )
        except OSError as exc:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail=f"Failed to run codex --version: {exc}",
                executable=executable,
            )

        output = (proc.stdout or proc.stderr or "").strip()
        first_line = output.splitlines()[0].strip() if output else None
        if proc.returncode != 0:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                version=first_line,
                detail=f"codex --version exited with code {proc.returncode}: {first_line or 'no output'}",
                executable=executable,
            )
        return AdapterHealth(
            host=self.host_name,
            available=True,
            version=first_line,
            detail="codex CLI available",
            executable=executable,
        )

    def start_window(self, request: WindowStartRequest) -> SessionHandle:
        if not request.prompt_path.exists():
            request.prompt_path.parent.mkdir(parents=True, exist_ok=True)
            request.prompt_path.write_text(request.prompt_text, encoding="utf-8")
        request.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        args = [
            "exec",
            "-C",
            str(request.workspace),
            "--sandbox",
            "read-only" if request.read_only else "workspace-write",
            "--json",
            "-o",
            str(request.last_message_path),
            "--skip-git-repo-check",
        ]
        if request.task.model:
            args.extend(["-m", request.task.model])
        args.append("-")

        norm_env = _normalize_resolve_env(request.env)
        argv = resolve_command_argv("codex", args, norm_env)
        pid = spawn_managed_cli(
            argv=argv,
            cwd=request.workspace,
            log_path=request.log_path,
            exit_code_path=request.exit_code_path,
            stdin_path=request.prompt_path,
            env=request.env,
        )
        return SessionHandle(
            host=self.host_name,
            session_id=None,
            title=f"[Super-Agent] {request.task.title}",
            status="running",
            pid=pid,
            resume_hint=None,
            log_path=str(request.log_path),
            metadata={
                "run_id": request.run_id,
                "task_id": request.task.id,
                "attempt_id": request.attempt_id,
                "exit_code_path": str(request.exit_code_path),
                "last_message_path": str(request.last_message_path),
                "result_path": str(request.result_path),
            },
        )

    def poll_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> WindowPollResult:
        del env
        log_path = Path(handle.log_path) if handle.log_path else None
        thread_id = _extract_thread_id(log_path) or handle.session_id
        resume_hint = f"codex resume {thread_id}" if thread_id else handle.resume_hint

        exit_code_path = _resolve_exit_code_path(handle)
        exit_record: dict[str, Any] | None = (
            read_exit_record(exit_code_path) if exit_code_path is not None else None
        )

        if exit_record is None and not is_pid_alive(handle.pid):
            if exit_code_path is not None:
                exit_record = read_exit_record(exit_code_path)
            if exit_record is None:
                return WindowPollResult(
                    status="failed",
                    session_id=thread_id,
                    resume_hint=resume_hint,
                    error="Managed runner exited without exit record",
                    cleanup_confirmed=True,
                )

        if exit_record is not None:
            code = int(exit_record.get("exit_code", 1))
            err = exit_record.get("error")
            if code == 0:
                return WindowPollResult(
                    status="completed",
                    session_id=thread_id,
                    exit_code=0,
                    resume_hint=resume_hint,
                    cleanup_confirmed=True,
                )
            return WindowPollResult(
                status="failed",
                session_id=thread_id,
                exit_code=code,
                resume_hint=resume_hint,
                error=str(err) if err else f"codex exec exited with code {code}",
                cleanup_confirmed=True,
            )

        return WindowPollResult(
            status="running",
            session_id=thread_id,
            resume_hint=resume_hint,
            cleanup_confirmed=False,
        )

    def cancel_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> CancelResult:
        del env
        return terminate_pid_tree(handle.pid)
