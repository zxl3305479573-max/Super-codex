"""Custom single-host CLI session adapter driven by host.json configuration."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
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

_PLACEHOLDER_RE = re.compile(
    r"\{(workspace|prompt_file|result_file|last_message_file|task_id|title|role|model)\}"
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


def _home_dir(env: Mapping[str, str] | None) -> Path | None:
    if env is None:
        return Path.home()
    raw = _env_get(env, "HOME") or _env_get(env, "USERPROFILE")
    if raw:
        return Path(raw)
    return None


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


def _merge_resolve_env(
    env: Mapping[str, str] | None,
    extra_env: Mapping[str, str] | None,
) -> Mapping[str, str] | None:
    if env is None and not extra_env:
        return None
    base = dict(os.environ if env is None else env)
    if env is not None and _env_get(base, "PATH") is None:
        base["PATH"] = ""
    elif env is not None:
        base["PATH"] = _env_get(base, "PATH") or ""
    if extra_env:
        for k, v in extra_env.items():
            base[str(k)] = str(v)
    return base


def find_custom_config_path(
    *,
    config_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    workspace: Path | str | None = None,
) -> Path | None:
    """Locate custom host.json checking explicit path, env, workspace, and home in order."""
    if config_path is not None:
        candidate = Path(config_path)
        return candidate if candidate.is_file() else None

    eff_env = os.environ if env is None else env
    env_cfg = (_env_get(eff_env, "SUPER_AGENT_HOST_CONFIG") or "").strip()
    if env_cfg:
        candidate = Path(env_cfg)
        return candidate if candidate.is_file() else None

    if workspace is not None:
        ws_candidate = Path(workspace) / ".super-agent" / "host.json"
        if ws_candidate.is_file():
            return ws_candidate

    home = _home_dir(env)
    if home is not None:
        home_candidate = home / ".super-agent" / "host.json"
        if home_candidate.is_file():
            return home_candidate

    return None


def load_custom_config(
    *,
    config_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    workspace: Path | str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Load and validate a single-host custom adapter configuration JSON file."""
    chosen: Path | None = None
    if config_path is not None:
        candidate = Path(config_path)
        if not candidate.is_file():
            raise AdapterError(f"Custom host config file not found: {candidate}")
        chosen = candidate
    else:
        eff_env = os.environ if env is None else env
        env_cfg = (_env_get(eff_env, "SUPER_AGENT_HOST_CONFIG") or "").strip()
        if env_cfg:
            candidate = Path(env_cfg)
            if not candidate.is_file():
                raise AdapterError(
                    f"SUPER_AGENT_HOST_CONFIG file not found: {candidate}"
                )
            chosen = candidate
        else:
            chosen = find_custom_config_path(env=env, workspace=workspace)

    if chosen is None:
        raise AdapterError(
            "No custom host config found. Checked explicit config_path, "
            "SUPER_AGENT_HOST_CONFIG, <workspace>/.super-agent/host.json, and ~/.super-agent/host.json."
        )

    try:
        raw_data = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"Failed to read custom host config {chosen}: {exc}") from None

    if not isinstance(raw_data, dict):
        raise AdapterError(f"Custom host config {chosen} must be a JSON object")

    command = raw_data.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(tok, str) for tok in command)
        or not command[0].strip()
    ):
        raise AdapterError(
            f"Custom host config {chosen} must define a non-empty string array 'command'"
        )

    name = str(raw_data.get("name") or "custom")
    stdin_prompt = bool(raw_data.get("stdin_prompt", False))

    raw_env = raw_data.get("env")
    if raw_env is None:
        cfg_env: dict[str, str] = {}
    elif isinstance(raw_env, dict):
        cfg_env = {str(k): str(v) for k, v in raw_env.items()}
    else:
        raise AdapterError(f"Custom host config {chosen} field 'env' must be an object")

    session_id_regex = raw_data.get("session_id_regex")
    if session_id_regex is not None:
        if not isinstance(session_id_regex, str):
            raise AdapterError(
                f"Custom host config {chosen} field 'session_id_regex' must be a string"
            )
        try:
            re.compile(session_id_regex)
        except re.error as exc:
            raise AdapterError(
                f"Custom host config {chosen} has invalid 'session_id_regex': {exc}"
            ) from None

    resume_template = raw_data.get("resume_template")
    if resume_template is not None and not isinstance(resume_template, str):
        raise AdapterError(
            f"Custom host config {chosen} field 'resume_template' must be a string"
        )

    validated: dict[str, Any] = {
        "name": name,
        "command": list(command),
        "stdin_prompt": stdin_prompt,
        "env": cfg_env,
        "session_id_regex": session_id_regex,
        "resume_template": resume_template,
    }
    return validated, chosen


def _format_argv_tokens(tokens: list[str], values: Mapping[str, str]) -> list[str]:
    return [_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], tok) for tok in tokens]


def _extract_session_id(log_path: Path | None, pattern: str | None) -> str | None:
    if not pattern or log_path is None or not log_path.is_file():
        return None
    try:
        regex = re.compile(pattern, re.MULTILINE)
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, re.error):
        return None

    match = regex.search(text)
    if match is None:
        return None
    if "session_id" in regex.groupindex:
        val = match.group("session_id")
        return str(val) if val else None
    if match.lastindex and match.lastindex >= 1:
        val = match.group(1)
        return str(val) if val else None
    val = match.group(0)
    return str(val) if val else None


def _resolve_exit_code_path(handle: SessionHandle) -> Path | None:
    raw = handle.metadata.get("exit_code_path")
    if raw:
        return Path(str(raw))
    if handle.log_path:
        return Path(handle.log_path).with_name("exit_code.json")
    return None


class CustomAdapter(HostSessionAdapter):
    """Single-host CLI session adapter configured via host.json."""

    host_name = "custom"

    def __init__(
        self,
        config_path: Path | str | None = None,
        workspace: Path | str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config_path: Path | None = Path(config_path) if config_path is not None else None
        self.workspace: Path | None = Path(workspace) if workspace is not None else None
        self._default_env: Mapping[str, str] | None = env

    def capabilities(self) -> AdapterCapabilities:
        resumable = False
        supports_model = False
        try:
            cfg, _ = load_custom_config(
                config_path=self.config_path,
                env=self._default_env,
                workspace=self.workspace,
            )
            resumable = bool(cfg.get("session_id_regex") and cfg.get("resume_template"))
            supports_model = any("{model}" in tok for tok in cfg.get("command", []))
        except AdapterError:
            pass
        return AdapterCapabilities(
            ui_native_window=False,
            resumable=resumable,
            supports_model=supports_model,
            supports_cancel=True,
            transport="cli-custom",
        )

    def health_check(self, env: Mapping[str, str] | None = None) -> AdapterHealth:
        eff_env = self._default_env if env is None else env
        try:
            cfg, cfg_path = load_custom_config(
                config_path=self.config_path,
                env=eff_env,
                workspace=self.workspace,
            )
        except AdapterError as exc:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail=str(exc),
                executable=None,
            )

        resolve_env = _merge_resolve_env(eff_env, cfg.get("env"))
        cmd_exe = cfg["command"][0]
        try:
            argv = resolve_command_argv(cmd_exe, [], resolve_env)
        except AdapterError as exc:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail=f"Custom host '{cfg['name']}' executable unavailable: {exc}",
                executable=None,
            )

        executable = _display_executable(argv)
        return AdapterHealth(
            host=self.host_name,
            available=True,
            version=None,
            detail=f"Custom host '{cfg['name']}' configured from {cfg_path}",
            executable=executable,
        )

    def start_window(self, request: WindowStartRequest) -> SessionHandle:
        eff_env = self._default_env if request.env is None else request.env
        cfg, cfg_path = load_custom_config(
            config_path=self.config_path,
            env=eff_env,
            workspace=request.workspace or self.workspace,
        )
        if not request.prompt_path.exists():
            request.prompt_path.parent.mkdir(parents=True, exist_ok=True)
            request.prompt_path.write_text(request.prompt_text, encoding="utf-8")
        request.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        values = {
            "workspace": str(request.workspace),
            "prompt_file": str(request.prompt_path),
            "result_file": str(request.result_path),
            "last_message_file": str(request.last_message_path),
            "task_id": request.task.id,
            "title": request.task.title,
            "role": request.task.role.value,
            "model": request.task.model or "",
        }
        formatted = _format_argv_tokens(cfg["command"], values)
        resolve_env = _merge_resolve_env(eff_env, cfg.get("env"))
        argv = resolve_command_argv(formatted[0], formatted[1:], resolve_env)

        stdin_path = request.prompt_path if cfg.get("stdin_prompt", False) else None
        pid = spawn_managed_cli(
            argv=argv,
            cwd=request.workspace,
            log_path=request.log_path,
            exit_code_path=request.exit_code_path,
            stdin_path=stdin_path,
            env=eff_env,
            extra_env=cfg.get("env") or None,
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
                "custom_name": cfg["name"],
                "config_path": str(cfg_path),
                "exit_code_path": str(request.exit_code_path),
                "last_message_path": str(request.last_message_path),
                "result_path": str(request.result_path),
                "session_id_regex": cfg.get("session_id_regex"),
                "resume_template": cfg.get("resume_template"),
            },
        )

    def poll_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> WindowPollResult:
        del env
        log_path = Path(handle.log_path) if handle.log_path else None
        regex = handle.metadata.get("session_id_regex")
        resume_template = handle.metadata.get("resume_template")

        session_id = _extract_session_id(
            log_path, str(regex) if regex else None
        ) or handle.session_id
        resume_hint = handle.resume_hint
        if session_id and isinstance(resume_template, str) and resume_template:
            resume_hint = resume_template.replace("{session_id}", session_id)

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
                return WindowPollResult(
                    status="completed",
                    session_id=session_id,
                    exit_code=0,
                    resume_hint=resume_hint,
                    cleanup_confirmed=True,
                )
            custom_name = handle.metadata.get("custom_name") or "custom"
            return WindowPollResult(
                status="failed",
                session_id=session_id,
                exit_code=code,
                resume_hint=resume_hint,
                error=str(err) if err else f"Custom host '{custom_name}' exited with code {code}",
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
