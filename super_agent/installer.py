"""Single-host plugin installer and uninstaller for Super-Agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from typing import Any
import uuid

from super_agent import __version__
from super_agent.models import utc_now_iso


class InstallerError(RuntimeError):
    """Raised when plugin layout validation, installation, or uninstallation fails."""


SUPPORTED_HOSTS: tuple[str, ...] = ("antigravity", "codex", "claude", "opencode", "custom")
SUPPORTED_SCOPES: tuple[str, ...] = ("project", "user")

_MANIFEST_FILES: tuple[str, ...] = (
    "plugin.json",
    "mcp_config.json",
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    ".mcp.json",
)
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _normalize_host(host: str) -> str:
    if not isinstance(host, str):
        raise InstallerError(f"Host must be a string, got {type(host).__name__}")
    normalized = host.strip().lower()
    if normalized not in SUPPORTED_HOSTS:
        allowed = ", ".join(SUPPORTED_HOSTS)
        raise InstallerError(f"Unsupported host {host!r}. Supported hosts: {allowed}")
    return normalized


def _normalize_scope(scope: str) -> str:
    if not isinstance(scope, str):
        raise InstallerError(f"Scope must be a string, got {type(scope).__name__}")
    normalized = scope.strip().lower()
    if normalized not in SUPPORTED_SCOPES:
        allowed = ", ".join(SUPPORTED_SCOPES)
        raise InstallerError(f"Unsupported scope {scope!r}. Supported scopes: {allowed}")
    return normalized


def _resolve_base_root(
    scope: str, *, target_root: Path | None, home_dir: Path | None,
) -> Path:
    if scope == "user":
        base = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    else:
        base = Path(target_root).expanduser() if target_root is not None else Path.cwd()
    return base.absolute()


def resolve_install_layout(
    host: str,
    *,
    scope: str = "project",
    target_root: Path | None = None,
    home_dir: Path | None = None,
) -> dict[str, Path]:
    """Return the exact install paths for the requested host and scope."""
    normalized_host = _normalize_host(host)
    normalized_scope = _normalize_scope(scope)
    base = _resolve_base_root(normalized_scope, target_root=target_root, home_dir=home_dir)

    if normalized_host == "antigravity":
        if normalized_scope == "user":
            return {"plugin_dir": base / ".gemini" / "config" / "plugins" / "super-agent"}
        return {"plugin_dir": base / ".agents" / "plugins" / "super-agent"}

    if normalized_host == "codex":
        if normalized_scope == "user":
            return {
                "plugin_dir": base / ".codex" / "plugins" / "super-agent",
                "skill_dir": base / ".agents" / "skills" / "super-agent",
            }
        return {
            "plugin_dir": base / ".agents" / "plugins" / "super-agent",
            "skill_dir": base / ".agents" / "skills" / "super-agent",
        }

    if normalized_host == "claude":
        return {"plugin_dir": base / ".claude" / "plugins" / "super-agent"}

    if normalized_host == "opencode":
        if normalized_scope == "user":
            return {
                "plugin_dir": base / ".config" / "opencode" / "plugins" / "super-agent",
                "skill_dir": base / ".agents" / "skills" / "super-agent",
            }
        return {
            "plugin_dir": base / ".opencode" / "plugins" / "super-agent",
            "skill_dir": base / ".agents" / "skills" / "super-agent",
        }

    # normalized_host == "custom"
    return {
        "plugin_dir": base / ".super-agent" / "plugin",
        "skill_dir": base / ".agents" / "skills" / "super-agent",
    }


def _is_link_or_reparse_point(path: Path) -> bool:
    """Return True if path is a symlink or Windows directory junction/reparse point."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        pass
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            pass
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    file_attrs = getattr(st, "st_file_attributes", 0)
    return bool(file_attrs & reparse_flag)


def _assert_safe_destination(
    dest: Path,
    base_root: Path,
    *,
    source_root: Path | None = None,
) -> None:
    """Reject symlinked/reparse-point targets, non-directories, or destructive overlaps."""
    try:
        rel_parts = dest.relative_to(base_root).parts
    except ValueError:
        rel_parts = ()

    current = base_root
    for part in rel_parts:
        current = current / part
        if _is_link_or_reparse_point(current):
            raise InstallerError(
                f"Refusing to operate on symlink or reparse point: {current}"
            )

    if _is_link_or_reparse_point(dest):
        raise InstallerError(f"Refusing to operate on symlink or reparse point: {dest}")

    if dest.exists() and not dest.is_dir():
        raise InstallerError(f"Destination exists and is not a directory: {dest}")

    if source_root is not None:
        resolved_source = source_root.resolve()
        resolved_dest = dest.resolve()
        if resolved_source == resolved_dest or resolved_source.is_relative_to(resolved_dest):
            raise InstallerError(
                f"Install destination {dest} would overwrite source root {source_root}"
            )


_WIN_TRANSIENT_ERRORS: frozenset[int] = frozenset({5, 32, 145})


def _is_transient_win_error(exc: OSError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in _WIN_TRANSIENT_ERRORS


def _handle_remove_readonly(func: Any, path: str, _exc: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _rename_with_retry(
    src: Path, dst: Path, *, attempts: int = 12, delay_sec: float = 0.05
) -> None:
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            src.rename(dst)
            return
        except OSError as exc:
            last_exc = exc
            if not _is_transient_win_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay_sec * (attempt + 1))
    if last_exc is not None:
        raise last_exc


def _rmtree(path: Path, *, attempts: int = 10, delay_sec: float = 0.05) -> None:
    for attempt in range(attempts):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_handle_remove_readonly)
            else:
                shutil.rmtree(path, onerror=_handle_remove_readonly)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if not _is_transient_win_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay_sec * (attempt + 1))


def _swap_directory(staging: Path, target: Path) -> None:
    """Atomically replace target directory with staging directory, rolling back on error."""
    backup: Path | None = None
    try:
        if target.exists():
            backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
            _rename_with_retry(target, backup)
        _rename_with_retry(staging, target)
    except OSError as exc:
        if backup is not None and backup.exists() and not target.exists():
            try:
                _rename_with_retry(backup, target)
            except OSError:
                pass
        raise InstallerError(f"Failed to place installed directory at {target}: {exc}") from exc
    finally:
        if backup is not None and backup.exists():
            _rmtree(backup)


def _validate_source_root(source_root: Path) -> None:
    required_dirs = (
        source_root / "super_agent",
        source_root / "skills" / "super-agent",
    )
    for directory in required_dirs:
        if not directory.is_dir():
            raise InstallerError(f"Missing required plugin source directory: {directory}")
    for rel_file in _MANIFEST_FILES:
        file_path = source_root / rel_file
        if not file_path.is_file():
            raise InstallerError(f"Missing required plugin manifest file: {file_path}")


def _build_mcp_payload(
    *,
    python_executable: str,
    plugin_dir_resolved: Path,
    host: str,
    custom_host_config: Path | None,
) -> dict[str, Any]:
    env: dict[str, str] = {
        "PYTHONPATH": str(plugin_dir_resolved),
        "SUPER_AGENT_HOST": host,
    }
    if custom_host_config is not None:
        env["SUPER_AGENT_HOST_CONFIG"] = str(Path(custom_host_config).expanduser().resolve())
    return {
        "mcpServers": {
            "super-agent": {
                "command": python_executable,
                "args": ["-m", "super_agent.mcp_server"],
                "env": env,
            }
        }
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _build_next_steps(
    host: str,
    *,
    plugin_dir: Path,
    skill_dir: Path | None,
    mcp_config_path: Path,
    custom_host_config: Path | None,
) -> list[str]:
    steps: list[str] = []
    if host == "antigravity":
        steps.append(
            f"Reload Antigravity plugins so {plugin_dir} and {mcp_config_path.name} are loaded."
        )
    elif host == "codex":
        steps.append(
            f"Restart or reload Codex so the plugin ({plugin_dir}) and skill ({skill_dir}) are discovered."
        )
    elif host == "claude":
        steps.append(
            f"Reload Claude Code so the plugin manifest in {plugin_dir} and {mcp_config_path.name} are active."
        )
    elif host == "opencode":
        steps.append(
            f"Register or reload the OpenCode MCP config at {mcp_config_path} and skill at {skill_dir}."
        )
    else:
        steps.append(
            f"Configure your custom host MCP client to load {mcp_config_path} and skill {skill_dir}."
        )
        if custom_host_config is None:
            steps.append(
                "Set SUPER_AGENT_HOST_CONFIG to your custom host adapter JSON config if required."
            )
    steps.append(f"Run `python -m super_agent doctor --host {host}` to verify host readiness.")
    steps.append("Invoke `$super-agent` explicitly in a conversation to start an orchestrated run.")
    return steps


def install_plugin(
    host: str,
    *,
    scope: str = "project",
    target_root: Path | None = None,
    source_root: Path | None = None,
    python_executable: str | None = None,
    home_dir: Path | None = None,
    custom_host_config: Path | None = None,
) -> dict[str, Any]:
    """Install or upgrade the Super-Agent plugin for a single host and scope."""
    normalized_host = _normalize_host(host)
    normalized_scope = _normalize_scope(scope)
    base_root = _resolve_base_root(
        normalized_scope, target_root=target_root, home_dir=home_dir
    )
    layout = resolve_install_layout(
        normalized_host,
        scope=normalized_scope,
        target_root=target_root,
        home_dir=home_dir,
    )

    resolved_source = (
        Path(source_root).expanduser().resolve()
        if source_root is not None
        else Path(__file__).resolve().parent.parent
    )
    _validate_source_root(resolved_source)

    if python_executable is not None:
        resolved_python = str(python_executable).strip()
        if not resolved_python:
            raise InstallerError("python_executable cannot be empty")
    else:
        resolved_python = str(sys.executable)

    plugin_dir = layout["plugin_dir"]
    skill_dir = layout.get("skill_dir")

    _assert_safe_destination(plugin_dir, base_root, source_root=resolved_source)
    if skill_dir is not None:
        _assert_safe_destination(skill_dir, base_root, source_root=resolved_source)

    upgraded = plugin_dir.exists()

    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    plugin_dir_resolved = plugin_dir.parent.resolve() / plugin_dir.name

    plugin_staging = plugin_dir.parent / f".{plugin_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        plugin_staging.mkdir(parents=True, exist_ok=False)

        shutil.copytree(
            resolved_source / "super_agent",
            plugin_staging / "super_agent",
            ignore=_COPY_IGNORE,
        )
        shutil.copytree(
            resolved_source / "skills" / "super-agent",
            plugin_staging / "skills" / "super-agent",
            ignore=_COPY_IGNORE,
        )
        for rel_file in _MANIFEST_FILES:
            src_file = resolved_source / rel_file
            dst_file = plugin_staging / rel_file
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

        mcp_payload = _build_mcp_payload(
            python_executable=resolved_python,
            plugin_dir_resolved=plugin_dir_resolved,
            host=normalized_host,
            custom_host_config=custom_host_config,
        )
        _write_json(plugin_staging / "mcp_config.json", mcp_payload)
        _write_json(plugin_staging / ".mcp.json", mcp_payload)

        install_meta = {
            "host": normalized_host,
            "scope": normalized_scope,
            "version": __version__,
            "installed_at": utc_now_iso(),
            "python_executable": resolved_python,
        }
        _write_json(plugin_staging / "install_meta.json", install_meta)

        _swap_directory(plugin_staging, plugin_dir)
    except InstallerError:
        raise
    except OSError as exc:
        raise InstallerError(f"Failed to install plugin into {plugin_dir}: {exc}") from exc
    finally:
        if plugin_staging.exists():
            _rmtree(plugin_staging)

    if skill_dir is not None:
        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        skill_staging = skill_dir.parent / f".{skill_dir.name}.staging-{uuid.uuid4().hex}"
        try:
            shutil.copytree(
                resolved_source / "skills" / "super-agent",
                skill_staging,
                ignore=_COPY_IGNORE,
            )
            _swap_directory(skill_staging, skill_dir)
        except InstallerError:
            raise
        except OSError as exc:
            raise InstallerError(f"Failed to install skill into {skill_dir}: {exc}") from exc
        finally:
            if skill_staging.exists():
                _rmtree(skill_staging)

    mcp_config_path = (
        plugin_dir / "mcp_config.json"
        if normalized_host == "antigravity"
        else plugin_dir / ".mcp.json"
    )
    next_steps = _build_next_steps(
        normalized_host,
        plugin_dir=plugin_dir,
        skill_dir=skill_dir,
        mcp_config_path=mcp_config_path,
        custom_host_config=custom_host_config,
    )

    return {
        "host": normalized_host,
        "scope": normalized_scope,
        "plugin_dir": str(plugin_dir),
        "skill_dir": str(skill_dir) if skill_dir is not None else None,
        "mcp_config_path": str(mcp_config_path),
        "upgraded": upgraded,
        "version": __version__,
        "next_steps": next_steps,
    }


def uninstall_plugin(
    host: str,
    *,
    scope: str = "project",
    target_root: Path | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Safely uninstall the Super-Agent plugin for a single host and scope."""
    normalized_host = _normalize_host(host)
    normalized_scope = _normalize_scope(scope)
    base_root = _resolve_base_root(
        normalized_scope, target_root=target_root, home_dir=home_dir
    )
    layout = resolve_install_layout(
        normalized_host,
        scope=normalized_scope,
        target_root=target_root,
        home_dir=home_dir,
    )

    plugin_dir = layout["plugin_dir"]
    skill_dir = layout.get("skill_dir")
    targets = [plugin_dir] + ([skill_dir] if skill_dir is not None else [])

    for target in targets:
        _assert_safe_destination(target, base_root)

    removed_paths: list[str] = []
    for target in targets:
        if target.exists():
            try:
                _rmtree(target)
            except OSError as exc:
                raise InstallerError(f"Failed to remove {target}: {exc}") from exc
            removed_paths.append(str(target))

    return {
        "host": normalized_host,
        "scope": normalized_scope,
        "plugin_dir": str(plugin_dir),
        "skill_dir": str(skill_dir) if skill_dir is not None else None,
        "removed_paths": removed_paths,
        "already_absent": len(removed_paths) == 0,
    }


__all__ = [
    "InstallerError",
    "SUPPORTED_HOSTS",
    "SUPPORTED_SCOPES",
    "install_plugin",
    "resolve_install_layout",
    "uninstall_plugin",
]
