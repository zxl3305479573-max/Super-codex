"""Host session adapter interface and cross-platform subprocess helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from super_agent.models import SessionHandle, TaskSpec


class AdapterError(RuntimeError):
    """Raised when a host session adapter fails to start, poll, or cancel a window."""


@dataclass
class AdapterCapabilities:
    ui_native_window: bool
    resumable: bool
    supports_model: bool
    supports_cancel: bool
    transport: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ui_native_window": self.ui_native_window,
            "resumable": self.resumable,
            "supports_model": self.supports_model,
            "supports_cancel": self.supports_cancel,
            "transport": self.transport,
        }


@dataclass
class AdapterHealth:
    host: str
    available: bool
    version: str | None = None
    detail: str = ""
    executable: str | None = None
    shell_fallback_recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "available": self.available,
            "version": self.version,
            "detail": self.detail,
            "executable": self.executable,
            "shell_fallback_recommended": self.shell_fallback_recommended,
        }


@dataclass
class WindowStartRequest:
    run_id: str
    task: TaskSpec
    attempt_id: str
    workspace: Path
    prompt_text: str
    prompt_path: Path
    result_path: Path
    last_message_path: Path
    log_path: Path
    exit_code_path: Path
    read_only: bool
    env: Mapping[str, str] | None = None


@dataclass
class WindowPollResult:
    status: str  # running | completed | failed | cancelled | unknown
    session_id: str | None = None
    exit_code: int | None = None
    resume_hint: str | None = None
    summary: str | None = None
    error: str | None = None
    cleanup_confirmed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "exit_code": self.exit_code,
            "resume_hint": self.resume_hint,
            "summary": self.summary,
            "error": self.error,
            "cleanup_confirmed": self.cleanup_confirmed,
            "metadata": dict(self.metadata),
        }


@dataclass
class CancelResult:
    confirmed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"confirmed": self.confirmed, "detail": self.detail}


class HostSessionAdapter(ABC):
    """Single-host adapter that opens real independent conversation windows/sessions."""

    host_name: str

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Describe what this host's session mechanism supports."""

    @abstractmethod
    def health_check(self, env: Mapping[str, str] | None = None) -> AdapterHealth:
        """Check whether this host adapter is ready in the current environment."""

    @abstractmethod
    def start_window(self, request: WindowStartRequest) -> SessionHandle:
        """Create and launch a brand-new independent conversation window (not a subagent)."""

    @abstractmethod
    def poll_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> WindowPollResult:
        """Inspect the current state of an independent window without blocking."""

    @abstractmethod
    def cancel_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> CancelResult:
        """Request termination and report whether termination is confirmed."""


def resolve_command_argv(
    executable: str, args: list[str], env: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve an executable safely across Windows (.exe/.cmd/.bat/.ps1) and POSIX."""
    path_env = env.get("PATH") if env is not None else os.environ.get("PATH")
    candidate = Path(executable)
    resolved: str | None = None
    if candidate.is_file():
        resolved = str(candidate.resolve())
    else:
        resolved = shutil.which(executable, path=path_env)

    if resolved is None:
        raise AdapterError(f"Executable not found on PATH: {executable}")

    resolved_path = Path(resolved)
    if os.name == "nt":
        suffix = resolved_path.suffix.lower()
        if suffix == ".ps1":
            for ext in (".exe", ".cmd", ".bat"):
                sibling = resolved_path.with_suffix(ext)
                if sibling.is_file():
                    resolved_path = sibling
                    suffix = ext
                    break
        if suffix == ".ps1":
            ps_exe = shutil.which("powershell.exe", path=path_env) or "powershell.exe"
            return [
                ps_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(resolved_path), *args,
            ]
        if suffix in (".cmd", ".bat"):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            return [comspec, "/d", "/c", str(resolved_path), *args]
    return [str(resolved_path), *args]


def is_pid_alive(pid: int | None) -> bool:
    """Return True if the OS process with the given PID appears alive."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_pid_tree(pid: int | None, *, wait_sec: float = 3.0) -> CancelResult:
    """Terminate a managed process tree and confirm whether it exited."""
    if pid is None or pid <= 0:
        return CancelResult(confirmed=False, detail="No PID recorded for managed session")
    if not is_pid_alive(pid):
        return CancelResult(confirmed=True, detail=f"Process {pid} already exited")

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                shell=False, capture_output=True, check=False, timeout=10,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                return CancelResult(confirmed=True, detail=f"Process {pid} already exited")
            except OSError:
                os.kill(pid, signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001
        return CancelResult(confirmed=False, detail=f"Failed to terminate PID {pid}: {exc}")

    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return CancelResult(confirmed=True, detail=f"Process tree {pid} terminated")
        time.sleep(0.05)

    if os.name != "nt":
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.1)

    if not is_pid_alive(pid):
        return CancelResult(confirmed=True, detail=f"Process tree {pid} killed")
    return CancelResult(confirmed=False, detail=f"Process {pid} remained alive after cancel")


def spawn_managed_cli(
    *,
    argv: list[str],
    cwd: Path,
    log_path: Path,
    exit_code_path: Path,
    stdin_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> int:
    """Spawn a detached managed CLI process via _proc_runner and return the runner PID."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path = exit_code_path.parent / "runner_spec.json"

    proc_env = dict(os.environ if env is None else env)
    if extra_env:
        proc_env.update({str(k): str(v) for k, v in extra_env.items()})
    repo_root = str(Path(__file__).resolve().parents[2])
    existing_pp = proc_env.get("PYTHONPATH", "")
    if repo_root not in existing_pp.split(os.pathsep):
        proc_env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing_pp}" if existing_pp else repo_root

    spec = {
        "argv": argv,
        "cwd": str(cwd),
        "log_path": str(log_path),
        "exit_code_path": str(exit_code_path),
        "stdin_path": str(stdin_path) if stdin_path is not None else None,
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    runner_script = str(Path(__file__).resolve().with_name("_proc_runner.py"))
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": proc_env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen([sys.executable, runner_script, str(spec_path)], **kwargs)
    return int(proc.pid)


def read_exit_record(exit_code_path: Path) -> dict[str, Any] | None:
    """Read the atomic exit record written by _proc_runner, or None if not finished."""
    if not exit_code_path.exists():
        return None
    try:
        return json.loads(exit_code_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
