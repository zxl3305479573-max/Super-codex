"""Single-host session adapter registry, host auto-detection, and doctor diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from super_agent.adapters.antigravity import AntigravityAdapter
from super_agent.adapters.base import (
    AdapterCapabilities,
    AdapterError,
    AdapterHealth,
    CancelResult,
    HostSessionAdapter,
    WindowPollResult,
    WindowStartRequest,
)
from super_agent.adapters.claude import ClaudeAdapter
from super_agent.adapters.codex import CodexAdapter
from super_agent.adapters.custom import CustomAdapter, find_custom_config_path
from super_agent.adapters.opencode import OpenCodeAdapter

BUILTIN_HOSTS: tuple[str, ...] = ("antigravity", "codex", "claude", "opencode", "custom")


def _env_get(env: Mapping[str, str], key: str) -> str | None:
    if key in env:
        return env[key]
    if os.name == "nt":
        target = key.lower()
        for k, v in env.items():
            if k.lower() == target:
                return v
    return None


def _has_any_env(env: Mapping[str, str], keys: tuple[str, ...]) -> bool:
    for key in keys:
        val = _env_get(env, key)
        if val is not None and str(val).strip():
            return True
    return False


def detect_host(
    env: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> str:
    """Detect the active single-host environment using deterministic precedence."""
    eff_env: Mapping[str, str] = os.environ if env is None else env

    explicit = _env_get(eff_env, "SUPER_AGENT_HOST")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower()

    if _has_any_env(eff_env, ("ANTIGRAVITY_LS_ADDRESS",)):
        return "antigravity"

    if _has_any_env(eff_env, ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_HOME")):
        return "codex"

    if _has_any_env(eff_env, ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")):
        return "claude"

    if _has_any_env(eff_env, ("OPENCODE", "OPENCODE_SERVER_URL")):
        return "opencode"

    path_env = _env_get(eff_env, "PATH") if env is not None else os.environ.get("PATH")
    which_path = (path_env or "") if env is not None else path_env
    for cli_host in ("codex", "claude", "opencode"):
        if shutil.which(cli_host, path=which_path) is not None:
            return cli_host

    if find_custom_config_path(env=env, workspace=workspace) is not None:
        return "custom"

    return "codex"


def get_adapter(
    host: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> HostSessionAdapter:
    """Instantiate the HostSessionAdapter for the requested or auto-detected host."""
    resolved = (
        host.strip().lower()
        if host is not None and host.strip()
        else detect_host(env=env, workspace=workspace)
    )
    if resolved == "antigravity":
        return AntigravityAdapter()
    if resolved == "codex":
        return CodexAdapter()
    if resolved == "claude":
        return ClaudeAdapter()
    if resolved == "opencode":
        return OpenCodeAdapter()
    if resolved == "custom":
        return CustomAdapter(workspace=workspace, env=env)
    raise AdapterError(
        f"Unknown host adapter '{resolved}'. Expected one of: {', '.join(BUILTIN_HOSTS)}"
    )


def doctor(
    env: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Run health checks and capability discovery across all built-in host adapters."""
    detected = detect_host(env=env, workspace=workspace)
    hosts: dict[str, dict[str, Any]] = {}
    first_available: str | None = None

    for host_name in BUILTIN_HOSTS:
        adapter = get_adapter(host_name, env=env, workspace=workspace)
        health = adapter.health_check(env=env)
        caps = adapter.capabilities()
        if health.available and first_available is None:
            first_available = host_name
        hosts[host_name] = {
            "available": health.available,
            "health": health.to_dict(),
            "capabilities": caps.to_dict(),
        }

    recommended: str | None = None
    if detected in hosts and hosts[detected]["available"]:
        recommended = detected
    else:
        recommended = first_available

    return {
        "detected_host": detected,
        "recommended_host": recommended,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "hosts": hosts,
    }


__all__ = [
    "BUILTIN_HOSTS",
    "AdapterCapabilities",
    "AdapterError",
    "AdapterHealth",
    "AntigravityAdapter",
    "CancelResult",
    "ClaudeAdapter",
    "CodexAdapter",
    "CustomAdapter",
    "HostSessionAdapter",
    "OpenCodeAdapter",
    "WindowPollResult",
    "WindowStartRequest",
    "detect_host",
    "doctor",
    "get_adapter",
]
