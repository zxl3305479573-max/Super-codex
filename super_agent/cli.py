"""Command-line interface for Super-Agent orchestration and plugin installation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from super_agent import __version__
from super_agent.installer import (
    SUPPORTED_HOSTS,
    SUPPORTED_SCOPES,
    install_plugin,
    uninstall_plugin,
)
from super_agent.mcp_server import McpServer


def _build_engine(
    *,
    run_root: str | None = None,
    host_override: str | None = None,
) -> Any:
    from super_agent.engine import SuperAgentEngine
    from super_agent.store import RunStore

    store = RunStore(Path(run_root)) if run_root is not None else None
    env: dict[str, str] | None = None
    if host_override:
        env = dict(os.environ)
        env["SUPER_AGENT_HOST"] = host_override
    return SuperAgentEngine(store=store, env=env)


def _load_tasks_arg(*, tasks_file: str | None, tasks_json: str | None) -> list[dict[str, Any]]:
    if tasks_file is not None:
        path = Path(tasks_file).expanduser()
        try:
            raw_text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"Cannot read tasks file {tasks_file!r}: {exc}") from exc
    elif tasks_json is not None:
        raw_text = tasks_json
    else:
        raise ValueError("One of --tasks-file or --tasks-json is required")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid tasks JSON: {exc}") from exc

    if isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
        tasks = parsed["tasks"]
    elif isinstance(parsed, list):
        tasks = parsed
    else:
        raise ValueError(
            "Tasks JSON must be a list of task objects (or an object with a 'tasks' list)"
        )

    if not tasks:
        raise ValueError("Task list must not be empty")
    for idx, item in enumerate(tasks):
        if not isinstance(item, dict):
            raise ValueError(f"Task at index {idx} must be a JSON object")
    return tasks


def _format_human(command: str, result: Any) -> str:
    if command == "doctor" and isinstance(result, dict):
        lines = [
            f"Detected host:    {result.get('detected_host')}",
            f"Recommended host: {result.get('recommended_host')}",
        ]
        if "shell_fallback_recommended" in result:
            lines.append(
                f"Shell fallback:   {'yes' if result.get('shell_fallback_recommended') else 'no'}"
            )
        if "git_worktree_available" in result:
            lines.append(
                f"Git worktrees:    {'yes' if result.get('git_worktree_available') else 'no'}"
            )
        hosts = result.get("hosts")
        if isinstance(hosts, dict) and hosts:
            lines.append("Hosts:")
            for host_name, info in hosts.items():
                avail = info.get("available") if isinstance(info, dict) else False
                status = "available" if avail else "unavailable"
                parts: list[str] = []
                if isinstance(info, dict):
                    health = info.get("health")
                    if isinstance(health, dict):
                        if health.get("version"):
                            parts.append(str(health["version"]))
                        detail = health.get("detail") or health.get("reason")
                        if detail:
                            parts.append(str(detail))
                        if health.get("shell_fallback_recommended"):
                            parts.append("shell fallback recommended")
                suffix = f" ({'; '.join(parts)})" if parts else ""
                lines.append(f"  - {host_name}: {status}{suffix}")
        return "\n".join(lines)

    if command == "list-runs" and isinstance(result, list):
        if not result:
            return "No runs found."
        lines = []
        for item in result:
            if isinstance(item, dict):
                lines.append(
                    f"{item.get('run_id')}  status={item.get('status')}  "
                    f"host={item.get('host')}  tasks={item.get('task_count')}  "
                    f"updated={item.get('updated_at')}"
                )
            else:
                lines.append(str(item))
        return "\n".join(lines)

    if command == "install" and isinstance(result, dict):
        action = "Upgraded" if result.get("upgraded") else "Installed"
        lines = [
            f"{action} super-agent {result.get('version')} for host={result.get('host')} "
            f"(scope={result.get('scope')})",
            f"Plugin dir: {result.get('plugin_dir')}",
        ]
        if result.get("skill_dir"):
            lines.append(f"Skill dir:  {result.get('skill_dir')}")
        lines.append(f"MCP config: {result.get('mcp_config_path')}")
        next_steps = result.get("next_steps")
        if isinstance(next_steps, list) and next_steps:
            lines.append("Next steps:")
            for step in next_steps:
                lines.append(f"  - {step}")
        return "\n".join(lines)

    if command == "uninstall" and isinstance(result, dict):
        if result.get("already_absent"):
            return (
                f"Super-Agent plugin for host={result.get('host')} "
                f"(scope={result.get('scope')}) was already absent."
            )
        removed = result.get("removed_paths") or []
        lines = [
            f"Uninstalled super-agent for host={result.get('host')} "
            f"(scope={result.get('scope')})",
            "Removed:",
        ]
        for path in removed:
            lines.append(f"  - {path}")
        return "\n".join(lines)

    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON output.",
    )
    common.add_argument(
        "--run-root",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Override the base directory used by RunStore.",
    )

    parser = argparse.ArgumentParser(
        prog="super-agent",
        description="Single-host multi-window orchestration plugin and CLI.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "--run-root",
        default=None,
        metavar="PATH",
        help="Override the base directory used by RunStore.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        parents=[common],
        help="Inspect active host detection, adapters, and Git worktree support.",
    )
    p_doctor.add_argument("--workspace", default=None, help="Optional workspace directory.")
    p_doctor.add_argument(
        "--host",
        default=None,
        choices=SUPPORTED_HOSTS,
        help="Optional host override for diagnostics.",
    )

    # plan-create
    p_plan = subparsers.add_parser(
        "plan-create",
        parents=[common],
        help="Validate a module DAG and create a draft run awaiting approval.",
    )
    p_plan.add_argument("--workspace", required=True, help="Target project workspace directory.")
    tasks_group = p_plan.add_mutually_exclusive_group(required=True)
    tasks_group.add_argument("--tasks-file", default=None, help="Path to JSON file with tasks.")
    tasks_group.add_argument("--tasks-json", default=None, help="Inline JSON string of tasks.")
    p_plan.add_argument(
        "--max-concurrency",
        type=int,
        default=3,
        help="Maximum concurrent independent windows (default: 3).",
    )
    p_plan.add_argument(
        "--host",
        default=None,
        choices=SUPPORTED_HOSTS,
        help="Optional explicit host adapter binding.",
    )
    p_plan.add_argument(
        "--isolation",
        choices=("auto", "worktree", "shared"),
        default="auto",
        help="Workspace isolation mode (default: auto).",
    )
    p_plan.add_argument(
        "--no-auto-verifier",
        action="store_true",
        default=False,
        help="Do not automatically append a read-only verifier task.",
    )
    p_plan.add_argument("--run-id", default=None, help="Optional explicit run ID.")

    # plan-approve
    p_approve = subparsers.add_parser(
        "plan-approve",
        parents=[common],
        help="Approve a draft run after user confirmation.",
    )
    p_approve.add_argument("--run-id", required=True, help="Run identifier.")

    # dispatch
    p_dispatch = subparsers.add_parser(
        "dispatch",
        parents=[common],
        help="Launch ready tasks in fresh independent host windows.",
    )
    p_dispatch.add_argument("--run-id", required=True, help="Run identifier.")

    # poll
    p_poll = subparsers.add_parser(
        "poll",
        parents=[common],
        help="Poll active windows once and harvest completed results.",
    )
    p_poll.add_argument("--run-id", required=True, help="Run identifier.")

    # wait
    p_wait = subparsers.add_parser(
        "wait",
        parents=[common],
        help="Poll an active run in bounded intervals until state changes or timeout.",
    )
    p_wait.add_argument("--run-id", required=True, help="Run identifier.")
    p_wait.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Maximum seconds to wait (default: 30).",
    )
    p_wait.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1).",
    )

    # retry
    p_retry = subparsers.add_parser(
        "retry",
        parents=[common],
        help="Retry a failed/timed-out/cancelled task in a fresh window.",
    )
    p_retry.add_argument("--run-id", required=True, help="Run identifier.")
    p_retry.add_argument("--task-id", required=True, help="Task identifier to retry.")
    p_retry.add_argument(
        "--no-reset-dependents",
        action="store_true",
        default=False,
        help="Do not reset blocked downstream dependents back to pending.",
    )

    # cancel
    p_cancel = subparsers.add_parser(
        "cancel",
        parents=[common],
        help="Request cancellation of active windows and cancel the run.",
    )
    p_cancel.add_argument("--run-id", required=True, help="Run identifier.")

    # integrate
    p_integrate = subparsers.add_parser(
        "integrate",
        parents=[common],
        help="Consolidate worker patches in DAG order and produce final.patch.",
    )
    p_integrate.add_argument("--run-id", required=True, help="Run identifier.")

    # summarize
    p_summarize = subparsers.add_parser(
        "summarize",
        parents=[common],
        help="Summarize run status, task results, sessions, and verification evidence.",
    )
    p_summarize.add_argument("--run-id", required=True, help="Run identifier.")

    # apply
    p_apply = subparsers.add_parser(
        "apply",
        parents=[common],
        help="Check or apply consolidated final.patch to the primary workspace.",
    )
    p_apply.add_argument("--run-id", required=True, help="Run identifier.")
    p_apply.add_argument(
        "--check-only",
        action="store_true",
        default=False,
        help="Only check if final.patch applies cleanly without modifying files.",
    )

    # list-runs
    subparsers.add_parser(
        "list-runs",
        parents=[common],
        help="List persisted runs sorted by updated_at descending.",
    )

    # mcp-serve
    subparsers.add_parser(
        "mcp-serve",
        parents=[common],
        help="Run the zero-dependency MCP JSON-RPC 2.0 stdio server.",
    )

    # install
    p_install = subparsers.add_parser(
        "install",
        parents=[common],
        help="Install or upgrade the Super-Agent plugin for a single host.",
    )
    p_install.add_argument(
        "--host",
        required=True,
        choices=SUPPORTED_HOSTS,
        help="Target host environment.",
    )
    p_install.add_argument(
        "--scope",
        choices=SUPPORTED_SCOPES,
        default="project",
        help="Installation scope: project or user (default: project).",
    )
    p_install.add_argument(
        "--target",
        default=None,
        help="Target project root (for scope=project) or home directory (for scope=user).",
    )
    p_install.add_argument(
        "--python",
        default=None,
        help="Explicit Python executable path for generated MCP config.",
    )
    p_install.add_argument(
        "--host-config",
        default=None,
        help="Optional custom host adapter JSON config path (for --host custom).",
    )

    # uninstall
    p_uninstall = subparsers.add_parser(
        "uninstall",
        parents=[common],
        help="Uninstall the Super-Agent plugin for a single host.",
    )
    p_uninstall.add_argument(
        "--host",
        required=True,
        choices=SUPPORTED_HOSTS,
        help="Target host environment.",
    )
    p_uninstall.add_argument(
        "--scope",
        choices=SUPPORTED_SCOPES,
        default="project",
        help="Installation scope: project or user (default: project).",
    )
    p_uninstall.add_argument(
        "--target",
        default=None,
        help="Target project root (for scope=project) or home directory (for scope=user).",
    )

    return parser


def _dispatch_command(args: argparse.Namespace, *, engine: Any | None = None) -> Any:
    cmd = args.command

    if cmd == "install":
        target_path = Path(args.target) if args.target is not None else None
        target_root = target_path if args.scope == "project" else None
        home_dir = target_path if args.scope == "user" else None
        custom_cfg = Path(args.host_config) if args.host_config is not None else None
        return install_plugin(
            args.host,
            scope=args.scope,
            target_root=target_root,
            python_executable=args.python,
            home_dir=home_dir,
            custom_host_config=custom_cfg,
        )

    if cmd == "uninstall":
        target_path = Path(args.target) if args.target is not None else None
        target_root = target_path if args.scope == "project" else None
        home_dir = target_path if args.scope == "user" else None
        return uninstall_plugin(
            args.host,
            scope=args.scope,
            target_root=target_root,
            home_dir=home_dir,
        )

    host_override = getattr(args, "host", None) if cmd == "doctor" else None
    eng = engine if engine is not None else _build_engine(
        run_root=args.run_root,
        host_override=host_override,
    )

    if cmd == "doctor":
        return eng.doctor(workspace=args.workspace)

    if cmd == "plan-create":
        if args.max_concurrency < 1:
            raise ValueError("--max-concurrency must be >= 1")
        tasks = _load_tasks_arg(tasks_file=args.tasks_file, tasks_json=args.tasks_json)
        return eng.create_plan(
            args.workspace,
            tasks,
            max_concurrency=args.max_concurrency,
            host=args.host,
            isolation_mode=args.isolation,
            auto_add_verifier=not args.no_auto_verifier,
            run_id=args.run_id,
        )

    if cmd == "plan-approve":
        return eng.approve_plan(args.run_id)

    if cmd == "dispatch":
        return eng.dispatch_ready(args.run_id)

    if cmd == "poll":
        return eng.poll_run(args.run_id)

    if cmd == "wait":
        if args.timeout < 0:
            raise ValueError("--timeout must be >= 0")
        if args.interval <= 0:
            raise ValueError("--interval must be > 0")
        return eng.wait_step(
            args.run_id,
            timeout_sec=args.timeout,
            poll_interval_sec=args.interval,
        )

    if cmd == "retry":
        return eng.retry_task(
            args.run_id,
            args.task_id,
            reset_dependents=not args.no_reset_dependents,
        )

    if cmd == "cancel":
        return eng.cancel_run(args.run_id)

    if cmd == "integrate":
        return eng.integrate_run(args.run_id)

    if cmd == "summarize":
        return eng.summarize_run(args.run_id)

    if cmd == "apply":
        return eng.apply_integration(args.run_id, check_only=args.check_only)

    if cmd == "list-runs":
        return eng.list_runs()

    raise ValueError(f"Unsupported command: {cmd}")


def _safe_print(text: str, *, stream: Any) -> None:
    payload = text + "\n"
    try:
        stream.write(payload)
        stream.flush()
    except UnicodeEncodeError:
        if hasattr(stream, "buffer") and stream.buffer is not None:
            stream.buffer.write(payload.encode("utf-8"))
            stream.buffer.flush()
        else:
            encoding = getattr(stream, "encoding", None) or "utf-8"
            stream.write(payload.encode(encoding, errors="replace").decode(encoding))
            stream.flush()


def main(argv: list[str] | None = None, *, engine: Any | None = None) -> int:
    """CLI entrypoint for `python -m super_agent` and `super-agent`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "mcp-serve":
        server = McpServer(engine=engine, run_root=args.run_root)
        return server.serve_stdio()

    try:
        result = _dispatch_command(args, engine=engine)
    except Exception as exc:  # noqa: BLE001
        if args.json:
            _safe_print(
                json.dumps(
                    {"error": str(exc), "error_type": type(exc).__name__},
                    ensure_ascii=False,
                    indent=2,
                ),
                stream=sys.stderr,
            )
        else:
            _safe_print(f"Error: {exc}", stream=sys.stderr)
        return 1

    if args.json:
        _safe_print(json.dumps(result, ensure_ascii=False, indent=2), stream=sys.stdout)
    else:
        _safe_print(_format_human(args.command, result), stream=sys.stdout)
    return 0


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    sys.exit(main())
