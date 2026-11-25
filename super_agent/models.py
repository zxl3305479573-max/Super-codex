"""Core data models, state enums, and JSON serialization for Super-Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    """Return an RFC 3339 UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RunStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.TIMED_OUT,
    TaskStatus.CANCELLED,
})

FAILED_LIKE_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.TIMED_OUT,
    TaskStatus.CANCELLED,
})


class TaskRole(str, Enum):
    WORKER = "worker"
    VERIFIER = "verifier"


class IsolationMode(str, Enum):
    WORKTREE = "worktree"
    SHARED = "shared"


@dataclass
class TaskSpec:
    id: str
    title: str
    goal: str
    depends_on: list[str] = field(default_factory=list)
    write_paths: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    role: TaskRole = TaskRole.WORKER
    model: str | None = None
    timeout_sec: int = 1800
    max_retries: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "depends_on": list(self.depends_on),
            "write_paths": list(self.write_paths),
            "acceptance_checks": list(self.acceptance_checks),
            "role": self.role.value,
            "model": self.model,
            "timeout_sec": self.timeout_sec,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        raw_retries = int(data.get("max_retries", 1))
        clamped_retries = max(0, min(1, raw_retries))
        role_raw = data.get("role", TaskRole.WORKER.value)
        role = role_raw if isinstance(role_raw, TaskRole) else TaskRole(str(role_raw))
        return cls(
            id=str(data["id"]),
            title=str(data.get("title") or data["id"]),
            goal=str(data.get("goal") or ""),
            depends_on=[str(dep) for dep in data.get("depends_on", [])],
            write_paths=[str(p) for p in data.get("write_paths", [])],
            acceptance_checks=[str(c) for c in data.get("acceptance_checks", [])],
            role=role,
            model=str(data["model"]) if data.get("model") is not None else None,
            timeout_sec=max(1, int(data.get("timeout_sec", 1800))),
            max_retries=clamped_retries,
        )


@dataclass
class SessionHandle:
    host: str
    session_id: str | None
    title: str
    status: str = "running"
    pid: int | None = None
    resume_hint: str | None = None
    log_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "session_id": self.session_id,
            "title": self.title,
            "status": self.status,
            "pid": self.pid,
            "resume_hint": self.resume_hint,
            "log_path": self.log_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionHandle:
        return cls(
            host=str(data["host"]),
            session_id=str(data["session_id"]) if data.get("session_id") is not None else None,
            title=str(data.get("title", "")),
            status=str(data.get("status", "running")),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            resume_hint=str(data["resume_hint"]) if data.get("resume_hint") is not None else None,
            log_path=str(data["log_path"]) if data.get("log_path") is not None else None,
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class TestCheckResult:
    check: str
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "outcome": self.outcome}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestCheckResult:
        return cls(check=str(data.get("check", "")), outcome=str(data.get("outcome", "")))


@dataclass
class ResultContract:
    task_id: str
    session_id: str | None = None
    status: str = "succeeded"
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    tests: list[TestCheckResult] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    next_action: str | None = None
    retryable: bool = False
    normalized_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "summary": self.summary,
            "files_changed": list(self.files_changed),
            "tests": [item.to_dict() for item in self.tests],
            "issues": list(self.issues),
            "artifacts": list(self.artifacts),
            "next_action": self.next_action,
            "retryable": self.retryable,
            "normalized_notes": list(self.normalized_notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResultContract:
        tests = [
            item if isinstance(item, TestCheckResult) else TestCheckResult.from_dict(item)
            for item in (data.get("tests") or [])
            if isinstance(item, (dict, TestCheckResult))
        ]
        return cls(
            task_id=str(data.get("task_id", "")),
            session_id=str(data["session_id"]) if data.get("session_id") is not None else None,
            status=str(data.get("status", "failed")),
            summary=str(data.get("summary", "")),
            files_changed=[str(x) for x in (data.get("files_changed") or [])],
            tests=tests,
            issues=[str(x) for x in (data.get("issues") or [])],
            artifacts=[str(x) for x in (data.get("artifacts") or [])],
            next_action=str(data["next_action"]) if data.get("next_action") is not None else None,
            retryable=bool(data.get("retryable", False)),
            normalized_notes=[str(x) for x in (data.get("normalized_notes") or [])],
        )


@dataclass
class AttemptRecord:
    attempt_id: str
    attempt_number: int
    task_id: str
    host: str
    worktree_path: str | None = None
    input_tree: str | None = None
    patch_path: str | None = None
    prompt_path: str | None = None
    result_path: str | None = None
    last_message_path: str | None = None
    exit_code_path: str | None = None
    session: SessionHandle | None = None
    status: TaskStatus = TaskStatus.RUNNING
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None
    cleanup_confirmed: bool = False
    result: ResultContract | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "task_id": self.task_id,
            "host": self.host,
            "worktree_path": self.worktree_path,
            "input_tree": self.input_tree,
            "patch_path": self.patch_path,
            "prompt_path": self.prompt_path,
            "result_path": self.result_path,
            "last_message_path": self.last_message_path,
            "exit_code_path": self.exit_code_path,
            "session": self.session.to_dict() if self.session is not None else None,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cleanup_confirmed": self.cleanup_confirmed,
            "result": self.result.to_dict() if self.result is not None else None,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttemptRecord:
        status_raw = data.get("status", TaskStatus.RUNNING.value)
        status = status_raw if isinstance(status_raw, TaskStatus) else TaskStatus(str(status_raw))
        return cls(
            attempt_id=str(data["attempt_id"]),
            attempt_number=int(data["attempt_number"]),
            task_id=str(data["task_id"]),
            host=str(data["host"]),
            worktree_path=data.get("worktree_path"),
            input_tree=data.get("input_tree"),
            patch_path=data.get("patch_path"),
            prompt_path=data.get("prompt_path"),
            result_path=data.get("result_path"),
            last_message_path=data.get("last_message_path"),
            exit_code_path=data.get("exit_code_path"),
            session=SessionHandle.from_dict(data["session"]) if data.get("session") else None,
            status=status,
            started_at=str(data.get("started_at") or utc_now_iso()),
            ended_at=data.get("ended_at"),
            cleanup_confirmed=bool(data.get("cleanup_confirmed", False)),
            result=ResultContract.from_dict(data["result"]) if data.get("result") else None,
            error=data.get("error"),
        )


@dataclass
class TaskRuntimeState:
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    attempt_ids: list[str] = field(default_factory=list)
    active_attempt_id: str | None = None
    succeeded_attempt_id: str | None = None
    blocked_reason: str | None = None
    failure_reason: str | None = None
    retries_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "attempt_ids": list(self.attempt_ids),
            "active_attempt_id": self.active_attempt_id,
            "succeeded_attempt_id": self.succeeded_attempt_id,
            "blocked_reason": self.blocked_reason,
            "failure_reason": self.failure_reason,
            "retries_used": self.retries_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRuntimeState:
        status_raw = data.get("status", TaskStatus.PENDING.value)
        status = status_raw if isinstance(status_raw, TaskStatus) else TaskStatus(str(status_raw))
        return cls(
            task_id=str(data["task_id"]),
            status=status,
            attempt_ids=[str(a) for a in data.get("attempt_ids", [])],
            active_attempt_id=data.get("active_attempt_id"),
            succeeded_attempt_id=data.get("succeeded_attempt_id"),
            blocked_reason=data.get("blocked_reason"),
            failure_reason=data.get("failure_reason"),
            retries_used=int(data.get("retries_used", 0)),
        )


@dataclass
class IntegrationState:
    status: str = "pending"
    worktree_path: str | None = None
    patch_path: str | None = None
    source_patch_paths: list[str] = field(default_factory=list)
    applied_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "worktree_path": self.worktree_path,
            "patch_path": self.patch_path,
            "source_patch_paths": list(self.source_patch_paths),
            "applied_at": self.applied_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntegrationState:
        return cls(
            status=str(data.get("status", "pending")),
            worktree_path=data.get("worktree_path"),
            patch_path=data.get("patch_path"),
            source_patch_paths=[str(p) for p in data.get("source_patch_paths", [])],
            applied_at=data.get("applied_at"),
            error=data.get("error"),
        )


@dataclass
class RunState:
    run_id: str
    host: str
    workspace: str
    run_dir: str
    isolation_mode: IsolationMode
    base_commit: str | None
    max_concurrency: int = 3
    status: RunStatus = RunStatus.DRAFT
    tasks: list[TaskSpec] = field(default_factory=list)
    task_states: dict[str, TaskRuntimeState] = field(default_factory=dict)
    attempts: dict[str, AttemptRecord] = field(default_factory=dict)
    integration: IntegrationState = field(default_factory=IntegrationState)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def task_map(self) -> dict[str, TaskSpec]:
        return {task.id: task for task in self.tasks}

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "host": self.host,
            "workspace": self.workspace,
            "run_dir": self.run_dir,
            "isolation_mode": self.isolation_mode.value,
            "base_commit": self.base_commit,
            "max_concurrency": self.max_concurrency,
            "status": self.status.value,
            "tasks": [task.to_dict() for task in self.tasks],
            "task_states": {tid: ts.to_dict() for tid, ts in self.task_states.items()},
            "attempts": {aid: att.to_dict() for aid, att in self.attempts.items()},
            "integration": self.integration.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        iso_raw = data.get("isolation_mode", IsolationMode.SHARED.value)
        iso = iso_raw if isinstance(iso_raw, IsolationMode) else IsolationMode(str(iso_raw))
        status_raw = data.get("status", RunStatus.DRAFT.value)
        status = status_raw if isinstance(status_raw, RunStatus) else RunStatus(str(status_raw))
        tasks = [TaskSpec.from_dict(item) for item in data.get("tasks", [])]
        task_states = {
            str(tid): TaskRuntimeState.from_dict(ts)
            for tid, ts in (data.get("task_states") or {}).items()
        }
        attempts = {
            str(aid): AttemptRecord.from_dict(att)
            for aid, att in (data.get("attempts") or {}).items()
        }
        integration = (
            IntegrationState.from_dict(data["integration"])
            if data.get("integration")
            else IntegrationState()
        )
        return cls(
            run_id=str(data["run_id"]),
            host=str(data["host"]),
            workspace=str(data["workspace"]),
            run_dir=str(data["run_dir"]),
            isolation_mode=iso,
            base_commit=data.get("base_commit"),
            max_concurrency=max(1, int(data.get("max_concurrency", 3))),
            status=status,
            tasks=tasks,
            task_states=task_states,
            attempts=attempts,
            integration=integration,
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            metadata=dict(data.get("metadata") or {}),
        )
