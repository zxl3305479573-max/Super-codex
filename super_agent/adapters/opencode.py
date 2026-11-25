"""OpenCode CLI single-host session adapter launching standalone `opencode run` sessions."""

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

_DEFAULT_RUN_PROMPT = (
    "Follow all instructions in the attached prompt file and write the required result JSON."
)


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


def _extract_content_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str) and block:
                parts.append(block)
            elif isinstance(block, dict):
                txt = block.get("text")
                if isinstance(txt, str) and txt:
                    parts.append(txt)
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        txt = value.get("text") or value.get("content")
        return _extract_content_text(txt)
    return None


def _iter_json_events(log_path: Path | None) -> list[dict[str, Any]]:
    if log_path is None or not log_path.is_file():
        return []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)

    if not events:
        stripped = text.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                events.append(parsed)
            elif isinstance(parsed, list):
                events.extend(item for item in parsed if isinstance(item, dict))
    return events


def _extract_opencode_log(log_path: Path | None) -> tuple[str | None, str | None]:
    events = _iter_json_events(log_path)
    session_id: str | None = None
    text_parts: list[str] = []
    explicit_result: str | None = None

    for event in events:
        if session_id is None:
            sid = event.get("sessionID") or event.get("session_id")
            if not sid and isinstance(event.get("session"), dict):
                sid = event["session"].get("id") or event["session"].get("sessionID")
            if not sid and isinstance(event.get("part"), dict):
                sid = event["part"].get("sessionID") or event["part"].get("session_id")
            if not sid and isinstance(event.get("properties"), dict):
                sid = event["properties"].get("sessionID") or event["properties"].get("session_id")
            if sid:
                session_id = str(sid)

        ev_type = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else None

        if ev_type == "result" and event.get("result") is not None:
            res = _extract_content_text(event.get("result"))
            if res:
                explicit_result = res
            continue

        if ev_type == "text":
            txt = None
            if part is not None:
                txt = _extract_content_text(part.get("text"))
            if txt is None:
                txt = _extract_content_text(event.get("text"))
            if txt:
                text_parts.append(txt)
            continue

        if part is not None and part.get("type") == "text":
            txt = _extract_content_text(part.get("text"))
            if txt:
                text_parts.append(txt)
            continue

        role = event.get("role")
        if role == "assistant" or ev_type in ("assistant", "assistant_message", "message"):
            if ev_type == "message" and role not in (None, "assistant"):
                continue
            txt = (
                _extract_content_text(event.get("text"))
                or _extract_content_text(event.get("content"))
                or _extract_content_text(event.get("message"))
            )
            if txt:
                text_parts.append(txt)
            continue

        if isinstance(event.get("text"), str) and event["text"] and role not in ("user", "system", "tool"):
            text_parts.append(event["text"])

    final_text = explicit_result if explicit_result is not None else ("\n".join(text_parts) if text_parts else None)
    return session_id, final_text


class OpenCodeAdapter(HostSessionAdapter):
    """Launches independent OpenCode sessions via `opencode run --format json`."""

    host_name = "opencode"

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
            argv = resolve_command_argv("opencode", ["--version"], norm_env)
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
                detail="opencode --version timed out after 5s",
                executable=executable,
            )
        except OSError as exc:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail=f"Failed to run opencode --version: {exc}",
                executable=executable,
            )

        output = (proc.stdout or proc.stderr or "").strip()
        first_line = output.splitlines()[0].strip() if output else None
        if proc.returncode != 0:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                version=first_line,
                detail=f"opencode --version exited with code {proc.returncode}: {first_line or 'no output'}",
                executable=executable,
            )
        return AdapterHealth(
            host=self.host_name,
            available=True,
            version=first_line,
            detail="opencode CLI available",
            executable=executable,
        )

    def start_window(self, request: WindowStartRequest) -> SessionHandle:
        if not request.prompt_path.exists():
            request.prompt_path.parent.mkdir(parents=True, exist_ok=True)
            request.prompt_path.write_text(request.prompt_text, encoding="utf-8")
        request.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        eff_env = os.environ if request.env is None else request.env
        title = f"[Super-Agent] {request.task.title}"
        args = [
            "run",
            "--format",
            "json",
            "--title",
            title,
            "-f",
            str(request.prompt_path),
        ]
        if request.task.model:
            args.extend(["-m", request.task.model])
        opencode_url = (_env_get(eff_env, "OPENCODE_SERVER_URL") or "").strip()
        if opencode_url:
            args.extend(["--attach", opencode_url])
        args.append(_DEFAULT_RUN_PROMPT)

        norm_env = _normalize_resolve_env(request.env)
        argv = resolve_command_argv("opencode", args, norm_env)
        pid = spawn_managed_cli(
            argv=argv,
            cwd=request.workspace,
            log_path=request.log_path,
            exit_code_path=request.exit_code_path,
            stdin_path=None,
            env=request.env,
        )
        return SessionHandle(
            host=self.host_name,
            session_id=None,
            title=title,
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

    def _populate_last_message(self, handle: SessionHandle, final_text: str | None) -> None:
        if final_text is None:
            return
        raw_path = handle.metadata.get("last_message_path")
        if not raw_path:
            return
        last_message_path = Path(str(raw_path))
        if last_message_path.exists():
            return
        try:
            last_message_path.parent.mkdir(parents=True, exist_ok=True)
            last_message_path.write_text(final_text, encoding="utf-8")
        except OSError:
            pass

    def poll_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> WindowPollResult:
        del env
        log_path = Path(handle.log_path) if handle.log_path else None
        discovered_sid, final_text = _extract_opencode_log(log_path)
        session_id = discovered_sid or handle.session_id
        resume_hint = f"opencode --session {session_id}" if session_id else handle.resume_hint

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
                self._populate_last_message(handle, final_text)
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
                error=str(err) if err else f"opencode run exited with code {code}",
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
