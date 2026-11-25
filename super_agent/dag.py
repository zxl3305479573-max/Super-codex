"""DAG validation, path overlap detection, and task scheduling for Super-Agent."""

from __future__ import annotations

from collections import deque
from pathlib import PureWindowsPath
import re

from super_agent.models import (
    AttemptRecord,
    FAILED_LIKE_TASK_STATUSES,
    RunState,
    TaskRole,
    TaskRuntimeState,
    TaskSpec,
    TaskStatus,
)


class DagValidationError(ValueError):
    """Raised when a task DAG or write-path specification is invalid."""


_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

_UNCONFIRMED_SLOT_ATTEMPT_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.RUNNING,
    TaskStatus.TIMED_OUT,
    TaskStatus.CANCELLED,
})


def normalize_rel_path(raw: str, *, ignore_case: bool = False) -> tuple[str, ...]:
    """Validate and normalize a repository-relative path into path segments."""
    if not isinstance(raw, str) or not raw or not raw.strip():
        raise DagValidationError(f"Write path must be a non-empty relative path: {raw!r}")
    if "\x00" in raw:
        raise DagValidationError(f"Write path must not contain NUL bytes: {raw!r}")
    if raw.startswith(("/", "\\")) or PureWindowsPath(raw).drive:
        raise DagValidationError(
            f"Write path must be repository-relative (no leading slash or Windows drive): {raw!r}"
        )

    normalized = raw.replace("\\", "/")
    raw_parts = normalized.split("/")
    if ".." in raw_parts:
        raise DagValidationError(f"Write path must not contain '..' traversal: {raw!r}")

    parts = tuple(part for part in raw_parts if part and part != ".")
    if not parts:
        raise DagValidationError(f"Write path must not resolve to empty/current directory: {raw!r}")

    if ignore_case:
        return tuple(part.casefold() for part in parts)
    return parts


def validate_tasks(tasks: list[TaskSpec]) -> list[str]:
    """Validate task specs and return a deterministic topological order of task IDs."""
    if not tasks:
        raise DagValidationError("Task list must not be empty")

    task_map: dict[str, TaskSpec] = {}
    for task in tasks:
        if not isinstance(task.id, str) or _TASK_ID_RE.fullmatch(task.id) is None:
            raise DagValidationError(
                f"Invalid task ID {task.id!r}: must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$"
            )
        if task.id in task_map:
            raise DagValidationError(f"Duplicate task ID: {task.id!r}")
        if task.role == TaskRole.VERIFIER and task.write_paths:
            raise DagValidationError(
                f"Verifier task {task.id!r} must have empty write_paths, got {task.write_paths!r}"
            )
        for raw_path in task.write_paths:
            normalize_rel_path(raw_path)
        task_map[task.id] = task

    indegree: dict[str, int] = {task.id: 0 for task in tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}

    for task in tasks:
        seen_deps: set[str] = set()
        for dep in task.depends_on:
            if dep == task.id:
                raise DagValidationError(f"Task {task.id!r} cannot depend on itself")
            if dep not in task_map:
                raise DagValidationError(
                    f"Task {task.id!r} depends on missing task {dep!r}"
                )
            if dep in seen_deps:
                continue
            seen_deps.add(dep)
            dependents[dep].append(task.id)
            indegree[task.id] += 1

    queue: deque[str] = deque(task.id for task in tasks if indegree[task.id] == 0)
    topo_order: list[str] = []

    while queue:
        current = queue.popleft()
        topo_order.append(current)
        for nxt in dependents[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(topo_order) != len(tasks):
        cyclic = [task.id for task in tasks if indegree[task.id] > 0]
        raise DagValidationError(f"Dependency cycle detected involving tasks: {', '.join(cyclic)}")

    return topo_order


def _paths_conflict(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    prefix_len = min(len(a), len(b))
    return a[:prefix_len] == b[:prefix_len]


def paths_overlap(
    paths_a: list[str],
    paths_b: list[str],
    *,
    ignore_case: bool = False,
) -> bool:
    """Return True if any path in paths_a equals or contains any path in paths_b (or vice versa)."""
    if not paths_a or not paths_b:
        return False

    norm_a = [normalize_rel_path(p, ignore_case=ignore_case) for p in paths_a]
    norm_b = [normalize_rel_path(p, ignore_case=ignore_case) for p in paths_b]

    for seg_a in norm_a:
        for seg_b in norm_b:
            if _paths_conflict(seg_a, seg_b):
                return True
    return False


def _latest_attempt(run: RunState, task_id: str) -> AttemptRecord | None:
    ts = run.task_states.get(task_id)
    if ts is not None:
        for aid in reversed(ts.attempt_ids):
            if aid in run.attempts:
                return run.attempts[aid]
        if ts.active_attempt_id and ts.active_attempt_id in run.attempts:
            return run.attempts[ts.active_attempt_id]

    matching = [att for att in run.attempts.values() if att.task_id == task_id]
    if not matching:
        return None
    return max(matching, key=lambda att: (att.attempt_number, att.started_at))


def _task_occupies_slot(run: RunState, task_id: str) -> bool:
    ts = run.task_states.get(task_id)
    if ts is not None and ts.status == TaskStatus.RUNNING:
        return True
    latest = _latest_attempt(run, task_id)
    if (
        latest is not None
        and latest.cleanup_confirmed is False
        and latest.status in _UNCONFIRMED_SLOT_ATTEMPT_STATUSES
    ):
        return True
    return False


def reserved_write_paths(run: RunState) -> list[str]:
    """Collect write_paths held by running tasks or tasks with unconfirmed cleanup."""
    reserved: list[str] = []
    for task in run.tasks:
        if _task_occupies_slot(run, task.id):
            reserved.extend(task.write_paths)
    return reserved


def occupied_slot_count(run: RunState) -> int:
    """Count tasks currently occupying a concurrency slot."""
    task_ids = dict.fromkeys([task.id for task in run.tasks] + list(run.task_states.keys()))
    return sum(1 for tid in task_ids if _task_occupies_slot(run, tid))


def select_ready_tasks(
    run: RunState,
    *,
    ignore_case: bool = False,
    include_verifiers: bool = True,
) -> list[TaskSpec]:
    """Select ready PENDING tasks in topological order respecting concurrency and write scopes."""
    if not run.tasks:
        return []

    topo_order = validate_tasks(run.tasks)
    available_slots = max(0, run.max_concurrency - occupied_slot_count(run))
    if available_slots == 0:
        return []

    task_map = run.task_map()
    reserved: list[str] = list(reserved_write_paths(run))
    selected: list[TaskSpec] = []

    for task_id in topo_order:
        if len(selected) >= available_slots:
            break
        task = task_map[task_id]
        ts = run.task_states.get(task_id)
        status = ts.status if ts is not None else TaskStatus.PENDING
        if status != TaskStatus.PENDING:
            continue
        if not include_verifiers and task.role == TaskRole.VERIFIER:
            continue

        deps_ready = all(
            (dep_ts := run.task_states.get(dep_id)) is not None
            and dep_ts.status == TaskStatus.SUCCEEDED
            for dep_id in task.depends_on
        )
        if not deps_ready:
            continue

        if paths_overlap(task.write_paths, reserved, ignore_case=ignore_case):
            continue

        selected.append(task)
        reserved.extend(task.write_paths)

    return selected


def propagate_blocked(run: RunState) -> list[str]:
    """Mark PENDING tasks with failed-like upstream dependencies as BLOCKED in topological order."""
    if not run.tasks:
        return []

    topo_order = validate_tasks(run.tasks)
    task_map = run.task_map()
    newly_blocked: list[str] = []

    for task_id in topo_order:
        ts = run.task_states.get(task_id)
        if ts is None:
            ts = TaskRuntimeState(task_id=task_id)
            run.task_states[task_id] = ts
        if ts.status != TaskStatus.PENDING:
            continue

        task = task_map[task_id]
        reasons: list[str] = []
        for dep_id in dict.fromkeys(task.depends_on):
            dep_ts = run.task_states.get(dep_id)
            if dep_ts is not None and dep_ts.status in FAILED_LIKE_TASK_STATUSES:
                detail = dep_ts.failure_reason or dep_ts.blocked_reason
                if not detail:
                    latest = _latest_attempt(run, dep_id)
                    if latest is not None and latest.error:
                        detail = latest.error
                if detail:
                    reasons.append(f"Upstream task '{dep_id}' {dep_ts.status.value}: {detail}")
                else:
                    reasons.append(f"Upstream task '{dep_id}' {dep_ts.status.value}")

        if reasons:
            ts.status = TaskStatus.BLOCKED
            ts.blocked_reason = "; ".join(reasons)
            newly_blocked.append(task_id)

    return newly_blocked


def downstream_task_ids(tasks: list[TaskSpec], root_task_id: str) -> list[str]:
    """Return all transitive dependents of root_task_id in topological order (excluding root)."""
    topo_order = validate_tasks(tasks)
    if root_task_id not in topo_order:
        raise DagValidationError(f"Unknown task ID: {root_task_id!r}")

    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dep in dict.fromkeys(task.depends_on):
            dependents[dep].append(task.id)

    visited: set[str] = set()
    queue: deque[str] = deque(dependents[root_task_id])
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for nxt in dependents[current]:
            if nxt not in visited:
                queue.append(nxt)

    return [tid for tid in topo_order if tid in visited]


def prerequisite_order(tasks: list[TaskSpec], target_task_id: str) -> list[str]:
    """Return all transitive prerequisites of target_task_id in topological order (excluding target)."""
    topo_order = validate_tasks(tasks)
    task_map = {task.id: task for task in tasks}
    if target_task_id not in task_map:
        raise DagValidationError(f"Unknown task ID: {target_task_id!r}")

    ancestors: set[str] = set()
    queue: deque[str] = deque(task_map[target_task_id].depends_on)
    while queue:
        current = queue.popleft()
        if current in ancestors:
            continue
        ancestors.add(current)
        for dep in task_map[current].depends_on:
            if dep not in ancestors:
                queue.append(dep)

    return [tid for tid in topo_order if tid in ancestors]
