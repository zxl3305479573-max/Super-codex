"""Core single-host multi-window orchestration engine for Super-Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any
import uuid

from super_agent import adapters, worktree
from super_agent.adapters import (
    BUILTIN_HOSTS,
    AdapterError,
    CancelResult,
    HostSessionAdapter,
    WindowPollResult,
    WindowStartRequest,
    detect_host,
    get_adapter,
)
from super_agent.dag import (
    DagValidationError,
    downstream_task_ids,
    occupied_slot_count,
    prerequisite_order,
    propagate_blocked,
    reserved_write_paths,
    select_ready_tasks,
    validate_tasks,
)
from super_agent.models import (
    AttemptRecord,
    IntegrationState,
    IsolationMode,
    ResultContract,
    RunState,
    RunStatus,
    TERMINAL_TASK_STATUSES,
    TaskRole,
    TaskRuntimeState,
    TaskSpec,
    TaskStatus,
    utc_now_iso,
)
from super_agent.protocol import (
    build_verifier_prompt,
    build_worker_prompt,
    parse_and_validate_result,
)
from super_agent.store import RunStore, StoreError, default_runs_root
from super_agent.worktree import WorktreeError


class EngineError(RuntimeError):
    """Raised on invalid orchestration state transitions or runtime failures."""


_TRANSIENT_ERROR_RE = re.compile(
    r"\b(?:transient|rate[\s_-]?limit(?:ed)?|too\s+many\s+requests|429|502|503|504|"
    r"temporar(?:y|ily)\s+unavailable|service\s+unavailable|connection\s+reset|"
    r"connection\s+refused|network\s+(?:error|timeout)|econnreset|etimedout|retryable)\b",
    re.IGNORECASE,
)

_DEFAULT_VERIFIER_CHECKS: tuple[str, ...] = (
    "Inspect deliverables, verify patch replay if in worktree mode, and run relevant verification checks.",
)


def _parse_iso_utc(value: str) -> datetime:
    """Parse an RFC 3339 / ISO 8601 timestamp string into an aware UTC datetime."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_command(argv: list[str]) -> str:
    """Format an argument vector into a shell-safe display command."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _escape_md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _format_plan_table(tasks: list[TaskSpec], topo_order: list[str]) -> str:
    task_map = {t.id: t for t in tasks}
    lines = [
        "| Task ID | Role | Title | Depends On | Write Paths | Timeout (s) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for tid in topo_order:
        task = task_map[tid]
        deps = ", ".join(task.depends_on) if task.depends_on else "-"
        writes = ", ".join(task.write_paths) if task.write_paths else "(read-only)"
        lines.append(
            f"| `{_escape_md_cell(task.id)}` | {task.role.value} | "
            f"{_escape_md_cell(task.title)} | {_escape_md_cell(deps)} | "
            f"{_escape_md_cell(writes)} | {task.timeout_sec} |"
        )
    return "\n".join(lines)


def _is_retryable_failure(result: ResultContract | None, poll_res: WindowPollResult) -> bool:
    """Determine whether a failed attempt is explicitly/safely retryable."""
    if result is not None:
        return bool(result.retryable)
    if poll_res.metadata.get("retryable") is True:
        return True
    if poll_res.status == "failed" and poll_res.exit_code not in (None, 0):
        err_text = (poll_res.error or "").strip()
        if err_text and _TRANSIENT_ERROR_RE.search(err_text):
            return True
    return False


def _patch_diff_stat(workspace: Path, patch_path: Path) -> str | None:
    """Return `git apply --stat` summary for a patch file, or None if unavailable."""
    if not patch_path.is_file():
        return None
    try:
        raw = patch_path.read_bytes()
    except OSError:
        return None
    if not raw:
        return "0 files changed"
    try:
        res = subprocess.run(
            ["git", "-C", str(workspace), "apply", "--stat", "-"],
            shell=False,
            input=raw,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", errors="replace").strip()


class SuperAgentEngine:
    """Single-host multi-window DAG orchestration engine with atomic persistence."""

    def __init__(
        self,
        store: RunStore | None = None,
        *,
        adapter_factory: Callable[
            [str | None, Mapping[str, str] | None, Path | None],
            HostSessionAdapter,
        ]
        | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._env: dict[str, str] | None = dict(env) if env is not None else None
        self.store: RunStore = (
            store if store is not None else RunStore(default_runs_root(self._env))
        )
        self._adapter_factory = adapter_factory

    def _ignore_case(self) -> bool:
        return os.name == "nt"

    def _get_adapter(self, host: str | None, workspace: Path | None) -> HostSessionAdapter:
        try:
            if self._adapter_factory is not None:
                return self._adapter_factory(host, self._env, workspace)
            return get_adapter(host, env=self._env, workspace=workspace)
        except AdapterError as exc:
            raise EngineError(str(exc)) from exc

    def _load_run(self, run_id: str) -> RunState:
        try:
            return self.store.load_run(run_id)
        except StoreError as exc:
            raise EngineError(str(exc)) from exc

    def _save_run(self, run: RunState) -> None:
        try:
            self.store.save_run(run)
        except StoreError as exc:
            raise EngineError(str(exc)) from exc

    def _append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.store.append_event(run_id, event_type, payload)
        except StoreError as exc:
            raise EngineError(str(exc)) from exc

    @staticmethod
    def _active_task_count(run: RunState) -> int:
        return sum(1 for ts in run.task_states.values() if ts.status == TaskStatus.RUNNING)

    @staticmethod
    def _latest_attempt_for_task(run: RunState, task_id: str) -> AttemptRecord | None:
        ts = run.task_states.get(task_id)
        if ts is None:
            return None
        for aid in reversed(ts.attempt_ids):
            if aid in run.attempts:
                return run.attempts[aid]
        if ts.active_attempt_id and ts.active_attempt_id in run.attempts:
            return run.attempts[ts.active_attempt_id]
        return None

    @staticmethod
    def _best_attempt_for_task(run: RunState, task_id: str) -> AttemptRecord | None:
        ts = run.task_states.get(task_id)
        if ts is None:
            return None
        if ts.succeeded_attempt_id and ts.succeeded_attempt_id in run.attempts:
            return run.attempts[ts.succeeded_attempt_id]
        if ts.active_attempt_id and ts.active_attempt_id in run.attempts:
            return run.attempts[ts.active_attempt_id]
        for aid in reversed(ts.attempt_ids):
            if aid in run.attempts:
                return run.attempts[aid]
        return None

    def _collect_prereq_summaries(self, run: RunState, task: TaskSpec) -> list[dict[str, Any]]:
        try:
            prereq_ids = prerequisite_order(run.tasks, task.id)
        except DagValidationError as exc:
            raise EngineError(str(exc)) from exc

        if not prereq_ids and task.role == TaskRole.VERIFIER:
            prereq_ids = [t.id for t in run.tasks if t.role == TaskRole.WORKER]

        summaries: list[dict[str, Any]] = []
        for pid in prereq_ids:
            ts = run.task_states.get(pid)
            att = self._best_attempt_for_task(run, pid)
            res = att.result if att is not None else None
            summaries.append(
                {
                    "task_id": pid,
                    "status": ts.status.value if ts is not None else TaskStatus.PENDING.value,
                    "summary": res.summary if res is not None else "",
                    "files_changed": list(res.files_changed) if res is not None else [],
                    "tests": [t.to_dict() for t in res.tests] if res is not None else [],
                    "artifacts": list(res.artifacts) if res is not None else [],
                    "issues": list(res.issues) if res is not None else [],
                }
            )
        return summaries

    def _build_integration_info(self, run: RunState) -> dict[str, Any]:
        if run.isolation_mode != IsolationMode.WORKTREE:
            return {
                "worktree_path": run.workspace,
                "patch_path": None,
                "source_patch_paths": [],
                "verify_command": None,
            }

        wt_path = run.integration.worktree_path
        patch_path = run.integration.patch_path
        source_patches = list(run.integration.source_patch_paths)
        verify_command: str | None = None
        if wt_path and patch_path:
            argv = [
                sys.executable,
                "-m",
                "super_agent.worktree",
                "verify",
                "--workspace",
                str(Path(wt_path).resolve()),
                "--output",
                str(Path(patch_path).resolve()),
            ]
            for sp in source_patches:
                argv.extend(["--patch", str(Path(sp).resolve())])
            verify_command = _format_command(argv)

        return {
            "worktree_path": wt_path or run.workspace,
            "patch_path": patch_path,
            "source_patch_paths": source_patches,
            "verify_command": verify_command,
        }

    def _refresh_run_status(self, run: RunState) -> None:
        if run.status in (RunStatus.DRAFT, RunStatus.CANCELLED):
            return

        task_map = run.task_map()
        running_roles = [
            task_map[tid].role
            for tid, ts in run.task_states.items()
            if tid in task_map and ts.status == TaskStatus.RUNNING
        ]
        if running_roles:
            run.status = (
                RunStatus.VERIFYING
                if any(role == TaskRole.VERIFIER for role in running_roles)
                else RunStatus.RUNNING
            )
            return

        pending_roles = [
            task_map[tid].role
            for tid, ts in run.task_states.items()
            if tid in task_map and ts.status == TaskStatus.PENDING
        ]
        if pending_roles:
            if run.status == RunStatus.APPROVED and not run.attempts:
                return
            if all(role == TaskRole.VERIFIER for role in pending_roles):
                run.status = RunStatus.VERIFYING
            else:
                run.status = RunStatus.RUNNING
            return

        # All tasks are in terminal states.
        statuses = [run.task_states[t.id].status for t in run.tasks]
        if statuses and all(st == TaskStatus.SUCCEEDED for st in statuses):
            run.status = RunStatus.COMPLETED
        elif statuses and all(st == TaskStatus.CANCELLED for st in statuses):
            run.status = RunStatus.CANCELLED
        else:
            run.status = RunStatus.FAILED

    def _ready_count(self, run: RunState) -> int:
        if run.status in (
            RunStatus.DRAFT,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            return 0
        ignore_case = self._ignore_case()
        try:
            workers = select_ready_tasks(run, ignore_case=ignore_case, include_verifiers=False)
            if workers:
                return len(workers)
            workers_in_progress = any(
                run.task_states[t.id].status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                for t in run.tasks
                if t.role == TaskRole.WORKER
            )
            if not workers_in_progress:
                return len(select_ready_tasks(run, ignore_case=ignore_case, include_verifiers=True))
        except DagValidationError:
            return 0
        return 0

    def _integrate_locked(
        self,
        run: RunState,
        *,
        require_all_workers: bool = True,
        required_worker_ids: set[str] | None = None,
    ) -> IntegrationState:
        if run.isolation_mode != IsolationMode.WORKTREE:
            return run.integration
        if not run.base_commit:
            raise EngineError(f"Run {run.run_id!r} is missing base_commit for worktree integration")

        try:
            topo_order = validate_tasks(run.tasks)
        except DagValidationError as exc:
            raise EngineError(str(exc)) from exc

        task_map = run.task_map()
        worker_ids = [tid for tid in topo_order if task_map[tid].role == TaskRole.WORKER]

        if require_all_workers:
            incomplete = [
                tid
                for tid in worker_ids
                if run.task_states[tid].status != TaskStatus.SUCCEEDED
            ]
            if incomplete:
                raise EngineError(
                    f"Cannot integrate run {run.run_id!r}: worker tasks not succeeded: "
                    f"{', '.join(incomplete)}"
                )
        elif required_worker_ids:
            incomplete = [
                tid
                for tid in worker_ids
                if tid in required_worker_ids
                and run.task_states[tid].status != TaskStatus.SUCCEEDED
            ]
            if incomplete:
                raise EngineError(
                    f"Cannot integrate run {run.run_id!r}: required worker tasks not succeeded: "
                    f"{', '.join(incomplete)}"
                )

        ordered_patches: list[Path] = []
        for tid in worker_ids:
            ts = run.task_states[tid]
            if ts.status != TaskStatus.SUCCEEDED:
                continue
            if not ts.succeeded_attempt_id or ts.succeeded_attempt_id not in run.attempts:
                raise EngineError(
                    f"Succeeded worker task {tid!r} is missing its attempt record in run {run.run_id!r}"
                )
            att = run.attempts[ts.succeeded_attempt_id]
            if att.patch_path:
                patch_file = Path(att.patch_path).resolve()
                if not patch_file.is_file():
                    raise EngineError(
                        f"Missing patch artifact for worker {tid!r}: {patch_file}"
                    )
                ordered_patches.append(patch_file)

        try:
            worktrees_root, final_patch = self.store.integration_paths(run.run_id)
        except StoreError as exc:
            raise EngineError(str(exc)) from exc

        seq = int(run.metadata.get("integration_seq", 0)) + 1
        while (worktrees_root / f"integration-{seq}").exists():
            seq += 1
        run.metadata["integration_seq"] = seq
        integration_name = f"integration-{seq}"

        try:
            wt_path, _ = worktree.prepare(
                Path(run.workspace),
                worktrees_root,
                integration_name,
                run.base_commit,
                [],
            )
            if final_patch.exists():
                final_patch.unlink()
            worktree.integrate(wt_path, ordered_patches, final_patch)
        except (WorktreeError, OSError) as exc:
            run.integration = IntegrationState(
                status="failed",
                worktree_path=str((worktrees_root / integration_name).resolve()),
                patch_path=str(final_patch.resolve()),
                source_patch_paths=[str(p) for p in ordered_patches],
                applied_at=None,
                error=str(exc),
            )
            self._save_run(run)
            self._append_event(
                run.run_id,
                "integration_failed",
                {
                    "error": str(exc),
                    "source_patch_paths": [str(p) for p in ordered_patches],
                },
            )
            raise EngineError(f"Integration failed for run {run.run_id!r}: {exc}") from exc

        run.integration = IntegrationState(
            status="ready",
            worktree_path=str(wt_path.resolve()),
            patch_path=str(final_patch.resolve()),
            source_patch_paths=[str(p) for p in ordered_patches],
            applied_at=None,
            error=None,
        )
        self._append_event(
            run.run_id,
            "integration_ready",
            {
                "worktree_path": run.integration.worktree_path,
                "patch_path": run.integration.patch_path,
                "source_patch_count": len(ordered_patches),
            },
        )
        return run.integration

    def doctor(self, workspace: str | Path | None = None) -> dict[str, Any]:
        """Run host adapter health checks and capability discovery."""
        ws = Path(workspace) if workspace else None
        return adapters.doctor(env=self._env, workspace=ws)

    def create_plan(
        self,
        workspace: str | Path,
        tasks: list[dict[str, Any] | TaskSpec],
        *,
        max_concurrency: int = 3,
        host: str | None = None,
        isolation_mode: str = "auto",
        auto_add_verifier: bool = True,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate a task DAG and persist a new DRAFT run plan without dispatching."""
        ws = Path(workspace).expanduser().resolve()
        if not ws.exists() or not ws.is_dir():
            raise EngineError(f"Workspace does not exist or is not a directory: {workspace}")

        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise EngineError(f"max_concurrency must be an integer >= 1, got {max_concurrency!r}")

        if host is not None and str(host).strip():
            resolved_host = str(host).strip().lower()
        else:
            resolved_host = detect_host(self._env, ws)

        if self._adapter_factory is None and resolved_host not in BUILTIN_HOSTS:
            raise EngineError(
                f"Unknown host '{resolved_host}'. Expected one of: {', '.join(BUILTIN_HOSTS)}"
            )

        if not isinstance(tasks, list) or not tasks:
            raise EngineError("Task list must not be empty")

        task_specs: list[TaskSpec] = []
        for idx, item in enumerate(tasks):
            try:
                if isinstance(item, TaskSpec):
                    spec = TaskSpec.from_dict(item.to_dict())
                elif isinstance(item, dict):
                    spec = TaskSpec.from_dict(item)
                else:
                    raise TypeError(f"Expected dict or TaskSpec, got {type(item).__name__}")
            except (KeyError, TypeError, ValueError) as exc:
                raise EngineError(f"Invalid task specification at index {idx}: {exc}") from exc
            task_specs.append(spec)

        # Validate user-supplied DAG before adding any automatic verifier or persisting anything.
        try:
            validate_tasks(task_specs)
        except DagValidationError as exc:
            raise EngineError(str(exc)) from exc

        if auto_add_verifier and not any(t.role == TaskRole.VERIFIER for t in task_specs):
            existing_ids = {t.id for t in task_specs}
            verifier_id = "verify"
            suffix = 1
            while verifier_id in existing_ids:
                verifier_id = f"verify-{suffix}"
                suffix += 1
            worker_ids = [t.id for t in task_specs if t.role == TaskRole.WORKER]
            task_specs.append(
                TaskSpec(
                    id=verifier_id,
                    title="Independent verification",
                    goal=(
                        "Independently inspect deliverables, verify patch replay if in worktree mode, "
                        "and run relevant verification checks."
                    ),
                    depends_on=worker_ids,
                    write_paths=[],
                    acceptance_checks=list(_DEFAULT_VERIFIER_CHECKS),
                    role=TaskRole.VERIFIER,
                    max_retries=1,
                )
            )

        try:
            topo_order = validate_tasks(task_specs)
        except DagValidationError as exc:
            raise EngineError(str(exc)) from exc

        mode_raw = str(isolation_mode).strip().lower()
        git_repo = worktree.is_git_repo(ws)
        if mode_raw == "auto":
            resolved_mode = IsolationMode.WORKTREE if git_repo else IsolationMode.SHARED
        elif mode_raw == "worktree":
            if not git_repo:
                raise EngineError(
                    f"isolation_mode='worktree' requires a Git repository root with a committed HEAD: {ws}"
                )
            resolved_mode = IsolationMode.WORKTREE
        elif mode_raw == "shared":
            resolved_mode = IsolationMode.SHARED
        else:
            raise EngineError(
                f"Invalid isolation_mode {isolation_mode!r}; expected 'auto', 'worktree', or 'shared'"
            )

        base_commit: str | None = None
        if resolved_mode == IsolationMode.WORKTREE:
            try:
                base_commit = worktree.resolve_base(ws)
            except WorktreeError as exc:
                raise EngineError(f"Failed to resolve Git HEAD in {ws}: {exc}") from exc

        resolved_run_id = run_id if run_id is not None else f"run-{uuid.uuid4().hex[:12]}"
        try:
            planned_run_dir = self.store.run_dir(resolved_run_id)
        except StoreError as exc:
            raise EngineError(str(exc)) from exc

        state = RunState(
            run_id=resolved_run_id,
            host=resolved_host,
            workspace=str(ws),
            run_dir=str(planned_run_dir),
            isolation_mode=resolved_mode,
            base_commit=base_commit,
            max_concurrency=max_concurrency,
            status=RunStatus.DRAFT,
            tasks=task_specs,
            task_states={t.id: TaskRuntimeState(task_id=t.id) for t in task_specs},
            attempts={},
            integration=IntegrationState(),
            metadata={"topological_order": list(topo_order)},
        )

        try:
            self.store.create_run(state)
        except StoreError as exc:
            raise EngineError(str(exc)) from exc

        plan_table = _format_plan_table(state.tasks, topo_order)
        return {
            "run_id": state.run_id,
            "status": state.status.value,
            "host": state.host,
            "workspace": state.workspace,
            "run_dir": state.run_dir,
            "isolation_mode": state.isolation_mode.value,
            "base_commit": state.base_commit,
            "max_concurrency": state.max_concurrency,
            "topological_order": topo_order,
            "tasks": [t.to_dict() for t in state.tasks],
            "requires_approval": True,
            "plan_table": plan_table,
        }

    def approve_plan(self, run_id: str) -> dict[str, Any]:
        """Transition a DRAFT plan to APPROVED (idempotent if already APPROVED)."""
        run = self._load_run(run_id)
        if run.status == RunStatus.APPROVED:
            return {
                "run_id": run.run_id,
                "status": run.status.value,
                "host": run.host,
                "workspace": run.workspace,
                "isolation_mode": run.isolation_mode.value,
                "max_concurrency": run.max_concurrency,
                "task_count": len(run.tasks),
                "task_statuses": {tid: ts.status.value for tid, ts in run.task_states.items()},
            }
        if run.status != RunStatus.DRAFT:
            raise EngineError(
                f"Cannot approve run {run_id!r} in status {run.status.value!r}; expected 'draft'"
            )

        run.status = RunStatus.APPROVED
        self._save_run(run)
        self._append_event(
            run.run_id,
            "plan_approved",
            {"host": run.host, "task_count": len(run.tasks)},
        )
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "host": run.host,
            "workspace": run.workspace,
            "isolation_mode": run.isolation_mode.value,
            "max_concurrency": run.max_concurrency,
            "task_count": len(run.tasks),
            "task_statuses": {tid: ts.status.value for tid, ts in run.task_states.items()},
        }

    def dispatch_ready(self, run_id: str) -> dict[str, Any]:
        """Dispatch ready tasks respecting dependencies, concurrency, and write-scope conflicts."""
        run = self._load_run(run_id)
        if run.status not in (RunStatus.APPROVED, RunStatus.RUNNING, RunStatus.VERIFYING):
            raise EngineError(
                f"Cannot dispatch tasks for run {run_id!r} in status {run.status.value!r}; "
                "expected 'approved', 'running', or 'verifying'"
            )

        try:
            blocked = propagate_blocked(run)
        except DagValidationError as exc:
            raise EngineError(str(exc)) from exc

        if blocked:
            self._append_event(run.run_id, "tasks_blocked", {"task_ids": blocked})

        ignore_case = self._ignore_case()
        try:
            ready_tasks = select_ready_tasks(
                run,
                ignore_case=ignore_case,
                include_verifiers=False,
            )
        except DagValidationError as exc:
            raise EngineError(str(exc)) from exc

        if not ready_tasks:
            workers_in_progress = any(
                run.task_states[t.id].status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                for t in run.tasks
                if t.role == TaskRole.WORKER
            )
            if not workers_in_progress:
                try:
                    ready_verifiers = select_ready_tasks(
                        run,
                        ignore_case=ignore_case,
                        include_verifiers=True,
                    )
                except DagValidationError as exc:
                    raise EngineError(str(exc)) from exc
                ready_tasks = [t for t in ready_verifiers if t.role == TaskRole.VERIFIER]

                if (
                    ready_tasks
                    and run.isolation_mode == IsolationMode.WORKTREE
                    and run.integration.status not in ("ready", "verified")
                ):
                    task_map = run.task_map()
                    required_workers: set[str] = set()
                    for vtask in ready_tasks:
                        for pid in prerequisite_order(run.tasks, vtask.id):
                            if task_map[pid].role == TaskRole.WORKER:
                                required_workers.add(pid)
                    self._integrate_locked(
                        run,
                        require_all_workers=False,
                        required_worker_ids=required_workers,
                    )
                    self._save_run(run)

        dispatched: list[dict[str, Any]] = []
        if ready_tasks:
            adapter = self._get_adapter(run.host, Path(run.workspace))
            task_map = run.task_map()
            integration_info = self._build_integration_info(run)

            for task in ready_tasks:
                ts = run.task_states.setdefault(task.id, TaskRuntimeState(task_id=task.id))
                attempt_number = len(ts.attempt_ids) + 1
                attempt_id = f"{task.id}-a{attempt_number}"
                try:
                    paths = self.store.attempt_paths(run.run_id, attempt_id)
                except StoreError as exc:
                    raise EngineError(str(exc)) from exc

                input_tree: str | None = None
                patch_path_str: str | None = None
                if run.isolation_mode == IsolationMode.WORKTREE:
                    if task.role == TaskRole.WORKER:
                        if not run.base_commit:
                            raise EngineError(
                                f"Run {run.run_id!r} missing base_commit in worktree mode"
                            )
                        prereq_ids = prerequisite_order(run.tasks, task.id)
                        prereq_patches: list[Path] = []
                        for pid in prereq_ids:
                            if task_map[pid].role != TaskRole.WORKER:
                                continue
                            pts = run.task_states.get(pid)
                            if pts is None or not pts.succeeded_attempt_id:
                                raise EngineError(
                                    f"Prerequisite task {pid!r} has no succeeded attempt for {task.id!r}"
                                )
                            patt = run.attempts.get(pts.succeeded_attempt_id)
                            if patt is not None and patt.patch_path:
                                pfile = Path(patt.patch_path).resolve()
                                if not pfile.is_file():
                                    raise EngineError(
                                        f"Missing prerequisite patch for {pid!r}: {pfile}"
                                    )
                                prereq_patches.append(pfile)
                        try:
                            attempt_worktree, input_tree = worktree.prepare(
                                Path(run.workspace),
                                paths.worktrees_root,
                                attempt_id,
                                run.base_commit,
                                prereq_patches,
                            )
                        except WorktreeError as exc:
                            raise EngineError(
                                f"Failed to prepare worktree for {attempt_id!r}: {exc}"
                            ) from exc
                        patch_path_str = str(paths.patch_path.resolve())
                    else:
                        if not run.integration.worktree_path:
                            raise EngineError(
                                f"Integration worktree is not prepared for verifier {task.id!r}"
                            )
                        attempt_worktree = Path(run.integration.worktree_path).resolve()
                else:
                    attempt_worktree = Path(run.workspace).resolve()

                attempt = AttemptRecord(
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    task_id=task.id,
                    host=run.host,
                    worktree_path=str(attempt_worktree),
                    input_tree=input_tree,
                    patch_path=patch_path_str,
                    prompt_path=str(paths.prompt_path.resolve()),
                    result_path=str(paths.result_path.resolve()),
                    last_message_path=str(paths.last_message_path.resolve()),
                    exit_code_path=str(paths.exit_code_path.resolve()),
                    session=None,
                    status=TaskStatus.RUNNING,
                    started_at=utc_now_iso(),
                    ended_at=None,
                    cleanup_confirmed=False,
                )

                prereq_summaries = self._collect_prereq_summaries(run, task)
                if task.role == TaskRole.VERIFIER:
                    prompt_text = build_verifier_prompt(
                        run,
                        task,
                        attempt,
                        integration_info,
                        prereq_summaries,
                    )
                else:
                    prompt_text = build_worker_prompt(
                        run,
                        task,
                        attempt,
                        prereq_summaries,
                    )

                try:
                    paths.prompt_path.write_text(prompt_text, encoding="utf-8")
                except OSError as exc:
                    raise EngineError(
                        f"Failed to write prompt file {paths.prompt_path}: {exc}"
                    ) from exc

                read_only = (task.role == TaskRole.VERIFIER) or (len(task.write_paths) == 0)
                request = WindowStartRequest(
                    run_id=run.run_id,
                    task=task,
                    attempt_id=attempt_id,
                    workspace=attempt_worktree,
                    prompt_text=prompt_text,
                    prompt_path=paths.prompt_path.resolve(),
                    result_path=paths.result_path.resolve(),
                    last_message_path=paths.last_message_path.resolve(),
                    log_path=paths.log_path.resolve(),
                    exit_code_path=paths.exit_code_path.resolve(),
                    read_only=read_only,
                    env=self._env,
                )

                try:
                    session = adapter.start_window(request)
                except AdapterError as exc:
                    raise EngineError(
                        f"Failed to start window for task {task.id!r} ({attempt_id}): {exc}"
                    ) from exc

                attempt.session = session
                run.attempts[attempt_id] = attempt
                ts.status = TaskStatus.RUNNING
                ts.active_attempt_id = attempt_id
                ts.attempt_ids.append(attempt_id)
                ts.blocked_reason = None
                run.status = (
                    RunStatus.VERIFYING
                    if task.role == TaskRole.VERIFIER
                    else RunStatus.RUNNING
                )
                self._save_run(run)
                self._append_event(
                    run.run_id,
                    "attempt_dispatched",
                    {
                        "task_id": task.id,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "role": task.role.value,
                        "host": run.host,
                        "session_id": session.session_id,
                        "resume_hint": session.resume_hint,
                        "worktree_path": attempt.worktree_path,
                    },
                )
                dispatched.append(
                    {
                        "task_id": task.id,
                        "attempt_id": attempt_id,
                        "session_id": session.session_id,
                        "resume_hint": session.resume_hint,
                        "worktree_path": attempt.worktree_path,
                        "role": task.role.value,
                    }
                )

        self._refresh_run_status(run)
        self._save_run(run)
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "dispatched": dispatched,
            "blocked": blocked,
            "active_count": self._active_task_count(run),
            "task_statuses": {tid: ts.status.value for tid, ts in run.task_states.items()},
        }

    def poll_run(self, run_id: str) -> dict[str, Any]:
        """Poll active windows and unconfirmed cancellations, capture patches, and advance states."""
        run = self._load_run(run_id)
        adapter = self._get_adapter(run.host, Path(run.workspace))
        changed_tasks: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        # 1. Re-poll TIMED_OUT / CANCELLED attempts with unconfirmed cleanup.
        for attempt in run.attempts.values():
            if attempt.status not in (TaskStatus.TIMED_OUT, TaskStatus.CANCELLED):
                continue
            if attempt.cleanup_confirmed:
                continue
            if attempt.session is None:
                continue
            try:
                poll_res = adapter.poll_window(attempt.session, env=self._env)
            except AdapterError as exc:
                raise EngineError(
                    f"Failed to poll unconfirmed attempt {attempt.attempt_id!r}: {exc}"
                ) from exc

            if poll_res.session_id:
                attempt.session.session_id = poll_res.session_id
            if poll_res.resume_hint:
                attempt.session.resume_hint = poll_res.resume_hint
            if poll_res.status:
                attempt.session.status = poll_res.status
            if poll_res.metadata:
                attempt.session.metadata.update(poll_res.metadata)

            if poll_res.cleanup_confirmed:
                attempt.cleanup_confirmed = True
                if attempt.ended_at is None:
                    attempt.ended_at = utc_now_iso()
                self._append_event(
                    run.run_id,
                    "attempt_cleanup_confirmed",
                    {
                        "task_id": attempt.task_id,
                        "attempt_id": attempt.attempt_id,
                        "status": attempt.status.value,
                    },
                )
                changed_tasks.append(
                    {
                        "task_id": attempt.task_id,
                        "attempt_id": attempt.attempt_id,
                        "status": attempt.status.value,
                        "cleanup_confirmed": True,
                        "event": "cleanup_confirmed",
                    }
                )

        # 2. Poll each currently RUNNING task/attempt.
        for task in run.tasks:
            ts = run.task_states.get(task.id)
            if ts is None or ts.status != TaskStatus.RUNNING:
                continue
            attempt = (
                run.attempts.get(ts.active_attempt_id)
                if ts.active_attempt_id
                else None
            )
            if attempt is None:
                ts.status = TaskStatus.FAILED
                ts.active_attempt_id = None
                ts.failure_reason = "Running task is missing its active attempt record"
                changed_tasks.append(
                    {
                        "task_id": task.id,
                        "attempt_id": None,
                        "status": ts.status.value,
                        "error": ts.failure_reason,
                    }
                )
                continue

            # Timeout check.
            elapsed = (now - _parse_iso_utc(attempt.started_at)).total_seconds()
            if elapsed >= task.timeout_sec:
                if attempt.session is not None:
                    try:
                        cancel_res = adapter.cancel_window(attempt.session, env=self._env)
                    except AdapterError as exc:
                        cancel_res = CancelResult(confirmed=False, detail=str(exc))
                else:
                    cancel_res = CancelResult(confirmed=True, detail="No session handle")

                attempt.status = TaskStatus.TIMED_OUT
                attempt.ended_at = utc_now_iso()
                attempt.cleanup_confirmed = bool(cancel_res.confirmed)
                attempt.error = f"Timed out after {task.timeout_sec}s: {cancel_res.detail}"
                if attempt.session is not None:
                    attempt.session.status = "cancelled" if cancel_res.confirmed else "running"

                ts.status = TaskStatus.TIMED_OUT
                ts.active_attempt_id = None
                ts.failure_reason = attempt.error

                self._append_event(
                    run.run_id,
                    "attempt_timed_out",
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "timeout_sec": task.timeout_sec,
                        "cleanup_confirmed": attempt.cleanup_confirmed,
                        "detail": cancel_res.detail,
                    },
                )
                changed_tasks.append(
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "status": ts.status.value,
                        "cleanup_confirmed": attempt.cleanup_confirmed,
                        "error": attempt.error,
                    }
                )
                continue

            if attempt.session is None:
                continue

            try:
                poll_res = adapter.poll_window(attempt.session, env=self._env)
            except AdapterError as exc:
                raise EngineError(
                    f"Failed to poll attempt {attempt.attempt_id!r}: {exc}"
                ) from exc

            if poll_res.session_id:
                attempt.session.session_id = poll_res.session_id
            if poll_res.resume_hint:
                attempt.session.resume_hint = poll_res.resume_hint
            if poll_res.status:
                attempt.session.status = poll_res.status
            if poll_res.metadata:
                attempt.session.metadata.update(poll_res.metadata)

            if poll_res.status in ("running", "unknown"):
                continue

            attempt.ended_at = utc_now_iso()
            attempt.cleanup_confirmed = bool(poll_res.cleanup_confirmed)

            result: ResultContract | None = None
            parse_errors: list[str] = []
            if attempt.result_path:
                result, parse_errors = parse_and_validate_result(
                    Path(attempt.result_path),
                    Path(attempt.last_message_path) if attempt.last_message_path else None,
                    task_id=task.id,
                    session_id=attempt.session.session_id,
                )
            else:
                parse_errors = ["Attempt has no result_path configured"]

            if poll_res.status == "cancelled":
                attempt.status = TaskStatus.CANCELLED
                attempt.result = result
                attempt.error = poll_res.error or "Window session was cancelled"
                ts.status = TaskStatus.CANCELLED
                ts.active_attempt_id = None
                ts.failure_reason = attempt.error
                self._append_event(
                    run.run_id,
                    "attempt_cancelled",
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "cleanup_confirmed": attempt.cleanup_confirmed,
                        "error": attempt.error,
                    },
                )
                changed_tasks.append(
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "status": ts.status.value,
                        "cleanup_confirmed": attempt.cleanup_confirmed,
                        "error": attempt.error,
                    }
                )
                continue

            if (
                result is not None
                and result.status == "succeeded"
                and poll_res.status == "completed"
            ):
                if (
                    run.isolation_mode == IsolationMode.WORKTREE
                    and task.role == TaskRole.WORKER
                ):
                    if not attempt.worktree_path or not attempt.input_tree or not attempt.patch_path:
                        err_msg = "worktree_capture_failed: missing worktree metadata on attempt"
                        attempt.status = TaskStatus.FAILED
                        attempt.result = result
                        attempt.error = err_msg
                        ts.status = TaskStatus.FAILED
                        ts.active_attempt_id = None
                        ts.failure_reason = err_msg
                        self._append_event(
                            run.run_id,
                            "attempt_failed",
                            {
                                "task_id": task.id,
                                "attempt_id": attempt.attempt_id,
                                "error": err_msg,
                            },
                        )
                        changed_tasks.append(
                            {
                                "task_id": task.id,
                                "attempt_id": attempt.attempt_id,
                                "status": ts.status.value,
                                "error": err_msg,
                            }
                        )
                        continue

                    try:
                        worktree.capture(
                            Path(attempt.worktree_path),
                            attempt.input_tree,
                            task.write_paths,
                            Path(attempt.patch_path),
                        )
                    except WorktreeError as exc:
                        is_scope_escape = "outside write scope" in str(exc).lower()
                        prefix = "scope_violation" if is_scope_escape else "worktree_capture_failed"
                        err_msg = f"{prefix}: {exc}"
                        attempt.status = TaskStatus.FAILED
                        attempt.result = result
                        attempt.error = err_msg
                        ts.status = TaskStatus.FAILED
                        ts.active_attempt_id = None
                        ts.failure_reason = err_msg
                        self._append_event(
                            run.run_id,
                            "attempt_scope_violation" if is_scope_escape else "attempt_failed",
                            {
                                "task_id": task.id,
                                "attempt_id": attempt.attempt_id,
                                "error": err_msg,
                                "retryable": False,
                            },
                        )
                        changed_tasks.append(
                            {
                                "task_id": task.id,
                                "attempt_id": attempt.attempt_id,
                                "status": ts.status.value,
                                "error": err_msg,
                            }
                        )
                        continue

                attempt.status = TaskStatus.SUCCEEDED
                attempt.result = result
                attempt.error = None
                ts.status = TaskStatus.SUCCEEDED
                ts.active_attempt_id = None
                ts.succeeded_attempt_id = attempt.attempt_id
                ts.failure_reason = None
                ts.blocked_reason = None
                if task.role == TaskRole.VERIFIER and run.integration.status == "ready":
                    run.integration.status = "verified"
                self._append_event(
                    run.run_id,
                    "attempt_succeeded",
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "session_id": attempt.session.session_id,
                        "patch_path": attempt.patch_path,
                    },
                )
                changed_tasks.append(
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "status": ts.status.value,
                        "session_id": attempt.session.session_id,
                    }
                )
                continue

            # Failure path: poll failed, invalid/missing result, or non-succeeded result status.
            if result is not None:
                if result.status == "succeeded" and poll_res.status != "completed":
                    error_msg = (
                        poll_res.error
                        or f"Window ended with status '{poll_res.status}' (exit_code={poll_res.exit_code})"
                    )
                else:
                    error_msg = (
                        "; ".join(result.issues)
                        or result.summary
                        or poll_res.error
                        or f"Task reported status '{result.status}'"
                    )
            else:
                parts: list[str] = []
                if poll_res.error:
                    parts.append(poll_res.error)
                if poll_res.exit_code not in (None, 0):
                    parts.append(f"exit_code={poll_res.exit_code}")
                if parse_errors:
                    parts.append("; ".join(parse_errors))
                error_msg = " | ".join(parts) or "Window finished without a valid result contract"

            attempt.status = TaskStatus.FAILED
            attempt.result = result
            attempt.error = error_msg
            ts.active_attempt_id = None

            max_auto_retries = min(1, max(0, task.max_retries))
            should_retry = (
                attempt.cleanup_confirmed
                and ts.retries_used < max_auto_retries
                and _is_retryable_failure(result, poll_res)
            )

            if should_retry:
                ts.retries_used += 1
                ts.status = TaskStatus.PENDING
                ts.failure_reason = error_msg
                self._append_event(
                    run.run_id,
                    "attempt_retry_scheduled",
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "retries_used": ts.retries_used,
                        "error": error_msg,
                    },
                )
                changed_tasks.append(
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "status": ts.status.value,
                        "retry_scheduled": True,
                        "retries_used": ts.retries_used,
                        "error": error_msg,
                    }
                )
            else:
                ts.status = TaskStatus.FAILED
                ts.failure_reason = error_msg
                self._append_event(
                    run.run_id,
                    "attempt_failed",
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "cleanup_confirmed": attempt.cleanup_confirmed,
                        "error": error_msg,
                    },
                )
                changed_tasks.append(
                    {
                        "task_id": task.id,
                        "attempt_id": attempt.attempt_id,
                        "status": ts.status.value,
                        "error": error_msg,
                    }
                )

        # 3. Propagate blocked downstream tasks & refresh run status.
        try:
            newly_blocked = propagate_blocked(run)
        except DagValidationError as exc:
            raise EngineError(str(exc)) from exc

        if newly_blocked:
            self._append_event(run.run_id, "tasks_blocked", {"task_ids": newly_blocked})
            for btid in newly_blocked:
                changed_tasks.append(
                    {
                        "task_id": btid,
                        "status": TaskStatus.BLOCKED.value,
                        "blocked_reason": run.task_states[btid].blocked_reason,
                    }
                )

        self._refresh_run_status(run)
        self._save_run(run)

        occupied_slots = occupied_slot_count(run)
        all_terminal = (
            all(ts.status in TERMINAL_TASK_STATUSES for ts in run.task_states.values())
            and occupied_slots == 0
        )

        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "changed_tasks": changed_tasks,
            "active_count": self._active_task_count(run),
            "occupied_slots": occupied_slots,
            "ready_count": self._ready_count(run),
            "task_statuses": {tid: ts.status.value for tid, ts in run.task_states.items()},
            "all_terminal": all_terminal,
        }

    def wait_step(
        self,
        run_id: str,
        *,
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 1.0,
    ) -> dict[str, Any]:
        """Poll in a bounded loop until a task changes state, run finishes, or timeout expires."""
        bounded_timeout = max(0.0, float(timeout_sec))
        bounded_interval = max(0.01, float(poll_interval_sec))
        deadline = time.monotonic() + bounded_timeout

        while True:
            result = self.poll_run(run_id)
            if result["changed_tasks"] or result["all_terminal"]:
                result["timed_out_waiting"] = False
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["timed_out_waiting"] = True
                return result
            time.sleep(min(bounded_interval, remaining))

    def retry_task(
        self,
        run_id: str,
        task_id: str,
        *,
        reset_dependents: bool = True,
    ) -> dict[str, Any]:
        """Reset a non-succeeded task (and optionally its blocked/failed dependents) for retry."""
        run = self._load_run(run_id)
        if run.status == RunStatus.DRAFT:
            raise EngineError(f"Cannot retry task {task_id!r} in draft run {run_id!r}")

        task_map = run.task_map()
        if task_id not in task_map:
            raise EngineError(f"Unknown task_id {task_id!r} in run {run_id!r}")

        ts = run.task_states[task_id]
        if ts.status == TaskStatus.SUCCEEDED:
            raise EngineError(
                f"Refusing to retry task {task_id!r}: task has already succeeded"
            )
        if ts.status == TaskStatus.RUNNING:
            raise EngineError(
                f"Refusing to retry task {task_id!r}: task is currently running"
            )

        latest = self._latest_attempt_for_task(run, task_id)
        if latest is not None and not latest.cleanup_confirmed:
            raise EngineError(
                f"Cannot retry task {task_id!r}: latest attempt {latest.attempt_id!r} "
                "does not have confirmed cleanup yet"
            )

        reset_downstream: list[str] = []
        if reset_dependents:
            try:
                downstream_ids = downstream_task_ids(run.tasks, task_id)
            except DagValidationError as exc:
                raise EngineError(str(exc)) from exc

            for did in downstream_ids:
                dts = run.task_states.get(did)
                if dts is None:
                    continue
                dtask = task_map[did]
                if dts.status in (
                    TaskStatus.BLOCKED,
                    TaskStatus.FAILED,
                    TaskStatus.TIMED_OUT,
                    TaskStatus.CANCELLED,
                ):
                    d_latest = self._latest_attempt_for_task(run, did)
                    if d_latest is not None and not d_latest.cleanup_confirmed:
                        raise EngineError(
                            f"Cannot reset downstream task {did!r}: latest attempt "
                            f"{d_latest.attempt_id!r} has unconfirmed cleanup"
                        )
                    dts.status = TaskStatus.PENDING
                    dts.failure_reason = None
                    dts.blocked_reason = None
                    dts.active_attempt_id = None
                    dts.retries_used = 0
                    reset_downstream.append(did)
                elif dts.status == TaskStatus.SUCCEEDED and dtask.role == TaskRole.VERIFIER:
                    dts.status = TaskStatus.PENDING
                    dts.failure_reason = None
                    dts.blocked_reason = None
                    dts.active_attempt_id = None
                    dts.succeeded_attempt_id = None
                    dts.retries_used = 0
                    reset_downstream.append(did)

        ts.status = TaskStatus.PENDING
        ts.failure_reason = None
        ts.blocked_reason = None
        ts.active_attempt_id = None
        ts.retries_used = 0

        run.integration = IntegrationState()
        run.status = RunStatus.RUNNING
        self._save_run(run)
        self._append_event(
            run.run_id,
            "task_retry_requested",
            {
                "task_id": task_id,
                "reset_dependents": reset_dependents,
                "reset_downstream": reset_downstream,
            },
        )
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "retried_task_id": task_id,
            "reset_dependents": reset_downstream,
            "task_statuses": {tid: state.status.value for tid, state in run.task_states.items()},
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Cancel pending and running tasks in a run, retaining slots for unconfirmed cancels."""
        run = self._load_run(run_id)
        adapter = self._get_adapter(run.host, Path(run.workspace))

        cancelled_tasks: list[str] = []
        unconfirmed_by_id: dict[str, dict[str, Any]] = {}

        for task in run.tasks:
            ts = run.task_states.get(task.id)
            if ts is None:
                continue
            if ts.status == TaskStatus.PENDING:
                ts.status = TaskStatus.CANCELLED
                ts.failure_reason = "Run cancelled"
                cancelled_tasks.append(task.id)
            elif ts.status == TaskStatus.RUNNING:
                attempt = (
                    run.attempts.get(ts.active_attempt_id)
                    if ts.active_attempt_id
                    else None
                )
                if attempt is not None:
                    if attempt.session is not None:
                        try:
                            cancel_res = adapter.cancel_window(attempt.session, env=self._env)
                        except AdapterError as exc:
                            cancel_res = CancelResult(confirmed=False, detail=str(exc))
                    else:
                        cancel_res = CancelResult(confirmed=True, detail="No session handle")
                    attempt.status = TaskStatus.CANCELLED
                    attempt.ended_at = utc_now_iso()
                    attempt.cleanup_confirmed = bool(cancel_res.confirmed)
                    attempt.error = (
                        f"Cancelled: {cancel_res.detail}" if cancel_res.detail else "Cancelled"
                    )
                    if attempt.session is not None:
                        attempt.session.status = "cancelled" if cancel_res.confirmed else "running"
                    if not attempt.cleanup_confirmed:
                        unconfirmed_by_id[attempt.attempt_id] = {
                            "task_id": task.id,
                            "attempt_id": attempt.attempt_id,
                            "detail": cancel_res.detail,
                        }
                ts.status = TaskStatus.CANCELLED
                ts.active_attempt_id = None
                ts.failure_reason = attempt.error if attempt is not None else "Run cancelled"
                cancelled_tasks.append(task.id)

        # Include any already-timed-out/cancelled attempts whose cleanup is still unconfirmed.
        for attempt in run.attempts.values():
            if (
                attempt.status in (TaskStatus.TIMED_OUT, TaskStatus.CANCELLED)
                and not attempt.cleanup_confirmed
                and attempt.attempt_id not in unconfirmed_by_id
            ):
                unconfirmed_by_id[attempt.attempt_id] = {
                    "task_id": attempt.task_id,
                    "attempt_id": attempt.attempt_id,
                    "detail": attempt.error or "Cleanup not yet confirmed",
                }

        unconfirmed_attempts = list(unconfirmed_by_id.values())
        run.status = RunStatus.CANCELLED
        self._save_run(run)
        self._append_event(
            run.run_id,
            "run_cancelled",
            {
                "cancelled_tasks": cancelled_tasks,
                "unconfirmed_attempts": unconfirmed_attempts,
            },
        )
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "cancelled_tasks": cancelled_tasks,
            "unconfirmed_attempts": unconfirmed_attempts,
            "occupied_slots": occupied_slot_count(run),
            "reserved_write_paths": reserved_write_paths(run),
            "task_statuses": {tid: ts.status.value for tid, ts in run.task_states.items()},
        }

    def integrate_run(self, run_id: str) -> dict[str, Any]:
        """Build a consolidated integration worktree and final.patch in worktree mode."""
        run = self._load_run(run_id)
        if run.isolation_mode == IsolationMode.SHARED:
            return {
                "status": "shared_mode",
                "workspace": run.workspace,
                "message": "Shared workspace mode does not produce a git patch",
            }

        integration = self._integrate_locked(run, require_all_workers=True)
        self._save_run(run)
        result = integration.to_dict()
        result["run_id"] = run.run_id
        return result

    def summarize_run(self, run_id: str) -> dict[str, Any]:
        """Return a complete structured summary and Markdown report for the run."""
        run = self._load_run(run_id)
        warnings: list[str] = []
        task_entries: list[dict[str, Any]] = []

        for task in run.tasks:
            ts = run.task_states.get(task.id) or TaskRuntimeState(task_id=task.id)
            att = self._best_attempt_for_task(run, task.id)
            res = att.result if att is not None else None

            session_id: str | None = None
            resume_hint: str | None = None
            if att is not None and att.session is not None:
                session_id = att.session.session_id
                resume_hint = att.session.resume_hint
            if not session_id and res is not None:
                session_id = res.session_id

            normalized_notes = list(res.normalized_notes) if res is not None else []
            for note in normalized_notes:
                warnings.append(f"Task '{task.id}': {note}")

            task_entries.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "role": task.role.value,
                    "status": ts.status.value,
                    "attempts_count": len(ts.attempt_ids),
                    "session_id": session_id,
                    "resume_hint": resume_hint,
                    "summary": res.summary if res is not None else "",
                    "files_changed": list(res.files_changed) if res is not None else [],
                    "tests": [t.to_dict() for t in res.tests] if res is not None else [],
                    "issues": list(res.issues) if res is not None else [],
                    "normalized_notes": normalized_notes,
                    "failure_reason": ts.failure_reason,
                    "blocked_reason": ts.blocked_reason,
                }
            )

        for attempt in run.attempts.values():
            if (
                attempt.status in (TaskStatus.TIMED_OUT, TaskStatus.CANCELLED)
                and not attempt.cleanup_confirmed
            ):
                warnings.append(
                    f"Attempt '{attempt.attempt_id}' ({attempt.status.value}) cleanup is not "
                    "confirmed; concurrency slot and write scope remain reserved."
                )

        integration_dict = run.integration.to_dict()
        if run.integration.patch_path:
            stat = _patch_diff_stat(Path(run.workspace), Path(run.integration.patch_path))
            if stat is not None:
                integration_dict["diff_stat"] = stat

        if (
            run.isolation_mode == IsolationMode.WORKTREE
            and run.integration.status in ("ready", "verified")
            and run.integration.applied_at is None
        ):
            warnings.append(
                f"Consolidated patch is ready at {run.integration.patch_path} and has not been "
                f"applied to source workspace {run.workspace}. Call apply_integration explicitly to apply."
            )
        elif run.integration.status == "failed" and run.integration.error:
            warnings.append(f"Integration failed: {run.integration.error}")

        verifier_entries = [e for e in task_entries if e["role"] == TaskRole.VERIFIER.value]
        verification = {
            "has_verifier": bool(verifier_entries),
            "passed": bool(verifier_entries)
            and all(e["status"] == TaskStatus.SUCCEEDED.value for e in verifier_entries),
            "tasks": verifier_entries,
        }

        md_lines = [
            f"### Super-Agent Run Summary: `{run.run_id}`",
            "",
            f"- **Status**: `{run.status.value}`",
            f"- **Host**: `{run.host}`",
            f"- **Isolation**: `{run.isolation_mode.value}`",
            f"- **Workspace**: `{run.workspace}`",
            "",
            "| Task | Role | Status | Attempts | Session | Summary |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for entry in task_entries:
            detail = (
                entry["summary"]
                or entry["failure_reason"]
                or entry["blocked_reason"]
                or "-"
            )
            sess = entry["session_id"] or "-"
            md_lines.append(
                f"| `{_escape_md_cell(entry['task_id'])}` | {entry['role']} | "
                f"`{entry['status']}` | {entry['attempts_count']} | "
                f"`{_escape_md_cell(sess)}` | {_escape_md_cell(detail)} |"
            )

        if verification["has_verifier"]:
            md_lines.extend(
                [
                    "",
                    f"**Verification**: `{'passed' if verification['passed'] else 'not_passed'}`",
                ]
            )
        if run.isolation_mode == IsolationMode.WORKTREE and run.integration.patch_path:
            md_lines.extend(
                [
                    f"**Integration**: `{run.integration.status}` (`{run.integration.patch_path}`)",
                ]
            )
        if warnings:
            md_lines.append("")
            md_lines.append("**Warnings**:")
            for warning in warnings:
                md_lines.append(f"- {warning}")

        return {
            "run_id": run.run_id,
            "host": run.host,
            "workspace": run.workspace,
            "isolation_mode": run.isolation_mode.value,
            "status": run.status.value,
            "base_commit": run.base_commit,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "tasks": task_entries,
            "integration": integration_dict,
            "verification": verification,
            "warnings": warnings,
            "markdown": "\n".join(md_lines),
        }

    def apply_integration(
        self,
        run_id: str,
        *,
        check_only: bool = False,
    ) -> dict[str, Any]:
        """Check or explicitly apply the verified consolidated patch to the source workspace."""
        run = self._load_run(run_id)
        if run.isolation_mode != IsolationMode.WORKTREE:
            raise EngineError(
                f"Cannot apply integration for run {run_id!r}: isolation_mode is "
                f"{run.isolation_mode.value!r}, expected 'worktree'"
            )
        if not run.integration.patch_path or not Path(run.integration.patch_path).is_file():
            raise EngineError(
                f"Cannot apply integration for run {run_id!r}: consolidated patch does not exist"
            )

        verifier_tasks = [t for t in run.tasks if t.role == TaskRole.VERIFIER]
        if verifier_tasks:
            unverified = [
                t.id
                for t in verifier_tasks
                if run.task_states[t.id].status != TaskStatus.SUCCEEDED
            ]
            if unverified:
                raise EngineError(
                    f"Cannot apply integration for run {run_id!r}: verifier tasks have not "
                    f"succeeded: {', '.join(unverified)}"
                )

        if run.integration.status == "applied" and not check_only:
            raise EngineError(f"Integration for run {run_id!r} has already been applied")

        patch_path = Path(run.integration.patch_path).resolve()
        try:
            worktree.apply_patch_to_workspace(
                Path(run.workspace),
                patch_path,
                check_only=check_only,
            )
        except WorktreeError as exc:
            raise EngineError(
                f"Failed to {'check' if check_only else 'apply'} integration patch for "
                f"run {run_id!r}: {exc}"
            ) from exc

        if not check_only:
            run.integration.status = "applied"
            run.integration.applied_at = utc_now_iso()
            self._save_run(run)
            self._append_event(
                run.run_id,
                "integration_applied",
                {
                    "workspace": run.workspace,
                    "patch_path": str(patch_path),
                    "applied_at": run.integration.applied_at,
                },
            )

        return {
            "run_id": run.run_id,
            "workspace": run.workspace,
            "patch_path": str(patch_path),
            "check_only": check_only,
            "applied": not check_only,
            "status": "check_passed" if check_only else run.integration.status,
            "applied_at": run.integration.applied_at,
        }

    def list_runs(self) -> list[dict[str, Any]]:
        """List all persisted runs sorted by updated_at descending."""
        try:
            return self.store.list_runs()
        except StoreError as exc:
            raise EngineError(str(exc)) from exc


__all__ = ["EngineError", "SuperAgentEngine"]
