"""Claude Code CLI single-host session adapter launching standalone `claude -p` sessions."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
from typing import Any
import uuid

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


def _resolve_exit_code_path(handle: SessionHandle) -> Path | None:
    raw = handle.metadata.get("exit_code_path")
    if raw:
        return Path(str(raw))
    if handle.log_path:
        return Path(handle.log_path).with_name("exit_code.json")
    return None


def _format_result_value(val: Any) -> str:
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


def _extract_claude_result(log_path: Path | None) -> str | None:
    if log_path is None or not log_path.is_file():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None

    # 1. Single JSON document (object or array).
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict) and parsed.get("result") is not None:
        return _format_result_value(parsed["result"])
    if isinstance(parsed, list):
        for item in reversed(parsed):
            if isinstance(item, dict) and item.get("result") is not None:
                return _format_result_value(item["result"])

    # 2. JSONL lines: last JSON object with a "result" field.
    last_result: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("result") is not None:
            last_result = _format_result_value(obj["result"])
    if last_result is not None:
        return last_result

    # 3. Fallback: scan embedded JSON objects (e.g. non-JSON preamble + pretty JSON).
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        brace = text.find("{", pos)
        if brace < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            pos = brace + 1
            continue
        if isinstance(obj, dict) and obj.get("result") is not None:
            last_result = _format_result_value(obj["result"])
        pos = max(end, brace + 1)
    return last_result


class ClaudeAdapter(HostSessionAdapter):
    """Launches independent Claude Code sessions via `claude -p --session-id <uuid>`."""

    host_name = "claude"

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
            argv = resolve_command_argv("claude", ["--version"], norm_env)
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
                detail="claude --version timed out after 5s",
                executable=executable,
            )
        except OSError as exc:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail=f"Failed to run claude --version: {exc}",
                executable=executable,
            )

        output = (proc.stdout or proc.stderr or "").strip()
        first_line = output.splitlines()[0].strip() if output else None
        if proc.returncode != 0:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                version=first_line,
                detail=f"claude --version exited with code {proc.returncode}: {first_line or 'no output'}",
                executable=executable,
            )
        return AdapterHealth(
            host=self.host_name,
            available=True,
            version=first_line,
            detail="claude CLI available",
            executable=executable,
        )

    def start_window(self, request: WindowStartRequest) -> SessionHandle:
        if not request.prompt_path.exists():
            request.prompt_path.parent.mkdir(parents=True, exist_ok=True)
            request.prompt_path.write_text(request.prompt_text, encoding="utf-8")
        request.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        session_id = str(uuid.uuid4())
        title = f"[Super-Agent] {request.task.title}"
        args = [
            "-p",
            "--output-format",
            "json",
            "--session-id",
            session_id,
            "-n",
            title,
            "--permission-mode",
            "plan" if request.read_only else "acceptEdits",
        ]
        if request.task.model:
            args.extend(["--model", request.task.model])

        norm_env = _normalize_resolve_env(request.env)
        argv = resolve_command_argv("claude", args, norm_env)
        pid = spawn_managed_cli(
            argv=argv,
            cwd=request.workspace,
            log_path=request.log_path,
            exit_code_path=request.exit_code_path,
            stdin_path=request.prompt_path,
            env=request.env,
        )
        resume_hint = f"claude --resume {session_id}"
        return SessionHandle(
            host=self.host_name,
            session_id=session_id,
            title=title,
            status="running",
            pid=pid,
            resume_hint=resume_hint,
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

    def _populate_last_message(self, handle: SessionHandle) -> None:
        raw_path = handle.metadata.get("last_message_path")
        if not raw_path:
            return
        last_message_path = Path(str(raw_path))
        if last_message_path.exists():
            return
        log_path = Path(handle.log_path) if handle.log_path else None
        result_text = _extract_claude_result(log_path)
        if result_text is not None:
            try:
                last_message_path.parent.mkdir(parents=True, exist_ok=True)
                last_message_path.write_text(result_text, encoding="utf-8")
            except OSError:
                pass

    def poll_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> WindowPollResult:
        del env
        session_id = handle.session_id
        resume_hint = handle.resume_hint or (f"claude --resume {session_id}" if session_id else None)

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
                    session_id=session_id,
                    resume_hint=resume_hint,
                    error="Managed runner exited without exit record",
                    cleanup_confirmed=True,
                )

        if exit_record is not None:
            code = int(exit_record.get("exit_code", 1))
            err = exit_record.get("error")
            if code == 0:
                self._populate_last_message(handle)
                return WindowPollResult(
                    status="completed",
                    session_id=session_id,
                    exit_code=0,
                    resume_hint=resume_hint,
                    cleanup_confirmed=True,
                )
            return WindowPollResult(
                status="failed",
                session_id=session_id,
                exit_code=code,
                resume_hint=resume_hint,
                error=str(err) if err else f"claude -p exited with code {code}",
                cleanup_confirmed=True,
            )

        return WindowPollResult(
            status="running",
            session_id=session_id,
            resume_hint=resume_hint,
            cleanup_confirmed=False,
        )

    def cancel_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> CancelResult:
        del env
        return terminate_pid_tree(handle.pid)
