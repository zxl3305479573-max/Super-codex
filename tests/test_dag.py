"""Unit tests for super_agent.dag."""

from __future__ import annotations

import pytest

from super_agent.dag import (
    DagValidationError,
    downstream_task_ids,
    normalize_rel_path,
    occupied_slot_count,
    paths_overlap,
    prerequisite_order,
    propagate_blocked,
    reserved_write_paths,
    select_ready_tasks,
    validate_tasks,
)
from super_agent.models import (
    AttemptRecord,
    IsolationMode,
    RunState,
    TaskRole,
    TaskRuntimeState,
    TaskSpec,
    TaskStatus,
)


def _make_run(
    tasks: list[TaskSpec],
    *,
    max_concurrency: int = 3,
    task_states: dict[str, TaskRuntimeState] | None = None,
    attempts: dict[str, AttemptRecord] | None = None,
) -> RunState:
    states = (
        dict(task_states)
        if task_states is not None
        else {task.id: TaskRuntimeState(task_id=task.id) for task in tasks}
    )
    return RunState(
        run_id="run-test",
        host="codex",
        workspace="/repo",
        run_dir="/runs/run-test",
        isolation_mode=IsolationMode.WORKTREE,
        base_commit="abc123",
        max_concurrency=max_concurrency,
        tasks=tasks,
        task_states=states,
        attempts=dict(attempts or {}),
    )


class TestNormalizeRelPath:
    def test_valid_paths_and_normalization(self) -> None:
        assert normalize_rel_path("src/api/auth.py") == ("src", "api", "auth.py")
        assert normalize_rel_path(r"src\api\auth.py") == ("src", "api", "auth.py")
        assert normalize_rel_path("./src/./api//auth.py/") == ("src", "api", "auth.py")
        assert normalize_rel_path("src/.env") == ("src", ".env")
        assert normalize_rel_path("src/..hidden") == ("src", "..hidden")

    def test_ignore_case(self) -> None:
        assert normalize_rel_path("Src/API/Auth.py", ignore_case=False) == ("Src", "API", "Auth.py")
        assert normalize_rel_path("Src/API/Auth.py", ignore_case=True) == ("src", "api", "auth.py")

    @pytest.mark.parametrize(
        "bad_path",
        [
            "",
            "   ",
            ".",
            "./",
            "./.",
            "/",
            "/src/a.py",
            "\\",
            r"\src\a.py",
            r"\\server\share\file.txt",
            "C:",
            "C:src/a.py",
            r"C:\src\a.py",
            "d:/repo/file.py",
            "..",
            "../a.py",
            "src/../a.py",
            "src/api/..",
            "src/a\x00.py",
        ],
    )
    def test_rejects_invalid_paths(self, bad_path: str) -> None:
        with pytest.raises(DagValidationError):
            normalize_rel_path(bad_path)


class TestValidateTasks:
    def test_rejects_empty_list(self) -> None:
        with pytest.raises(DagValidationError, match="empty"):
            validate_tasks([])

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",
            "-start",
            ".start",
            "_start",
            "has space",
            "has/slash",
            "a" * 65,
            "trailing\n",
            "bad$id",
        ],
    )
    def test_rejects_invalid_task_ids(self, bad_id: str) -> None:
        task = TaskSpec(id=bad_id, title="T", goal="G")
        with pytest.raises(DagValidationError, match="Invalid task ID"):
            validate_tasks([task])

    def test_accepts_boundary_valid_task_ids(self) -> None:
        tasks = [
            TaskSpec(id="a", title="A", goal="G"),
            TaskSpec(id="T_1.2-beta", title="B", goal="G"),
            TaskSpec(id="x" * 64, title="C", goal="G"),
        ]
        assert validate_tasks(tasks) == ["a", "T_1.2-beta", "x" * 64]

    def test_rejects_duplicate_ids(self) -> None:
        tasks = [
            TaskSpec(id="t1", title="One", goal="G"),
            TaskSpec(id="t1", title="Two", goal="G"),
        ]
        with pytest.raises(DagValidationError, match="Duplicate"):
            validate_tasks(tasks)

    def test_rejects_missing_dependency(self) -> None:
        tasks = [TaskSpec(id="t1", title="One", goal="G", depends_on=["missing"])]
        with pytest.raises(DagValidationError, match="missing"):
            validate_tasks(tasks)

    def test_rejects_self_dependency(self) -> None:
        tasks = [TaskSpec(id="t1", title="One", goal="G", depends_on=["t1"])]
        with pytest.raises(DagValidationError, match="itself"):
            validate_tasks(tasks)

    def test_rejects_cycles(self) -> None:
        two_cycle = [
            TaskSpec(id="a", title="A", goal="G", depends_on=["b"]),
            TaskSpec(id="b", title="B", goal="G", depends_on=["a"]),
        ]
        with pytest.raises(DagValidationError, match="cycle"):
            validate_tasks(two_cycle)

        three_cycle_with_root = [
            TaskSpec(id="root", title="R", goal="G"),
            TaskSpec(id="a", title="A", goal="G", depends_on=["root", "c"]),
            TaskSpec(id="b", title="B", goal="G", depends_on=["a"]),
            TaskSpec(id="c", title="C", goal="G", depends_on=["b"]),
        ]
        with pytest.raises(DagValidationError, match="cycle"):
            validate_tasks(three_cycle_with_root)

    def test_rejects_invalid_write_paths_and_verifier_writes(self) -> None:
        bad_writer = [TaskSpec(id="t1", title="T1", goal="G", write_paths=["../escape"])]
        with pytest.raises(DagValidationError):
            validate_tasks(bad_writer)

        bad_verifier = [
            TaskSpec(
                id="v1",
                title="Verify",
                goal="G",
                role=TaskRole.VERIFIER,
                write_paths=["src/a.py"],
            )
        ]
        with pytest.raises(DagValidationError, match="Verifier"):
            validate_tasks(bad_verifier)

        good_verifier = [
            TaskSpec(id="t1", title="W", goal="G", write_paths=["src/a.py"]),
            TaskSpec(
                id="v1",
                title="Verify",
                goal="G",
                depends_on=["t1"],
                role=TaskRole.VERIFIER,
                write_paths=[],
            ),
        ]
        assert validate_tasks(good_verifier) == ["t1", "v1"]

    def test_deterministic_topological_order_and_duplicate_deps(self) -> None:
        tasks = [
            TaskSpec(id="d", title="D", goal="G", depends_on=["b", "c", "b"]),
            TaskSpec(id="c", title="C", goal="G", depends_on=["a"]),
            TaskSpec(id="b", title="B", goal="G", depends_on=["a"]),
            TaskSpec(id="a", title="A", goal="G"),
            TaskSpec(id="e", title="E", goal="G"),
        ]
        # Zero-indegree in input order: a, e. Unlocking a yields c then b (input order), then d.
        assert validate_tasks(tasks) == ["a", "e", "c", "b", "d"]


class TestPathsOverlap:
    def test_empty_lists_never_overlap(self) -> None:
        assert paths_overlap([], []) is False
        assert paths_overlap([], ["src/a.py"]) is False
        assert paths_overlap(["src/a.py"], []) is False

    def test_directory_containment_and_prefix_boundary(self) -> None:
        assert paths_overlap(["src"], ["src/api/auth.py"]) is True
        assert paths_overlap(["src/api/auth.py"], ["src"]) is True
        assert paths_overlap(["src"], ["src-extra/a.py"]) is False
        assert paths_overlap(["src/a.py"], ["src/b.py"]) is False
        assert paths_overlap([r"src\api"], ["./src/api/auth.py"]) is True

    def test_case_sensitivity(self) -> None:
        assert paths_overlap(["Src/API"], ["src/api/auth.py"], ignore_case=False) is False
        assert paths_overlap(["Src/API"], ["src/api/auth.py"], ignore_case=True) is True


class TestSlotAndReservationTracking:
    def test_running_and_unconfirmed_attempts_reserve_paths_and_slots(self) -> None:
        tasks = [
            TaskSpec(id="t-run", title="R", goal="G", write_paths=["src/run"]),
            TaskSpec(id="t-to-unconf", title="T", goal="G", write_paths=["src/timeout"]),
            TaskSpec(id="t-to-conf", title="TC", goal="G", write_paths=["src/timeout_ok"]),
            TaskSpec(id="t-cancel-unconf", title="C", goal="G", write_paths=["src/cancel"]),
            TaskSpec(id="t-failed", title="F", goal="G", write_paths=["src/failed"]),
            TaskSpec(id="t-retried", title="RT", goal="G", write_paths=["src/retried"]),
            TaskSpec(id="v-run", title="V", goal="G", role=TaskRole.VERIFIER, write_paths=[]),
        ]
        attempts = {
            "a-run": AttemptRecord(
                attempt_id="a-run", attempt_number=1, task_id="t-run", host="codex",
                status=TaskStatus.RUNNING, cleanup_confirmed=False,
            ),
            "a-to-1": AttemptRecord(
                attempt_id="a-to-1", attempt_number=1, task_id="t-to-unconf", host="codex",
                status=TaskStatus.TIMED_OUT, cleanup_confirmed=False,
            ),
            "a-to-2": AttemptRecord(
                attempt_id="a-to-2", attempt_number=1, task_id="t-to-conf", host="codex",
                status=TaskStatus.TIMED_OUT, cleanup_confirmed=True,
            ),
            "a-c-1": AttemptRecord(
                attempt_id="a-c-1", attempt_number=1, task_id="t-cancel-unconf", host="codex",
                status=TaskStatus.CANCELLED, cleanup_confirmed=False,
            ),
            "a-f-1": AttemptRecord(
                attempt_id="a-f-1", attempt_number=1, task_id="t-failed", host="codex",
                status=TaskStatus.FAILED, cleanup_confirmed=False,
            ),
            "a-rt-1": AttemptRecord(
                attempt_id="a-rt-1", attempt_number=1, task_id="t-retried", host="codex",
                status=TaskStatus.TIMED_OUT, cleanup_confirmed=False,
            ),
            "a-rt-2": AttemptRecord(
                attempt_id="a-rt-2", attempt_number=2, task_id="t-retried", host="codex",
                status=TaskStatus.SUCCEEDED, cleanup_confirmed=True,
            ),
        }
        task_states = {
            "t-run": TaskRuntimeState(
                task_id="t-run", status=TaskStatus.RUNNING, attempt_ids=["a-run"], active_attempt_id="a-run",
            ),
            "t-to-unconf": TaskRuntimeState(
                task_id="t-to-unconf", status=TaskStatus.TIMED_OUT, attempt_ids=["a-to-1"],
            ),
            "t-to-conf": TaskRuntimeState(
                task_id="t-to-conf", status=TaskStatus.TIMED_OUT, attempt_ids=["a-to-2"],
            ),
            "t-cancel-unconf": TaskRuntimeState(
                task_id="t-cancel-unconf", status=TaskStatus.CANCELLED, attempt_ids=["a-c-1"],
            ),
            "t-failed": TaskRuntimeState(
                task_id="t-failed", status=TaskStatus.FAILED, attempt_ids=["a-f-1"],
            ),
            "t-retried": TaskRuntimeState(
                task_id="t-retried", status=TaskStatus.SUCCEEDED, attempt_ids=["a-rt-1", "a-rt-2"],
            ),
            "v-run": TaskRuntimeState(task_id="v-run", status=TaskStatus.RUNNING),
        }
        run = _make_run(tasks, task_states=task_states, attempts=attempts)

        assert reserved_write_paths(run) == ["src/run", "src/timeout", "src/cancel"]
        # t-run + t-to-unconf + t-cancel-unconf + v-run = 4 slots (no double-counting t-run)
        assert occupied_slot_count(run) == 4


class TestSelectReadyTasks:
    def test_concurrency_overlap_and_verifier_filter(self) -> None:
        tasks = [
            TaskSpec(id="t1", title="T1", goal="G", write_paths=["src/a"]),
            TaskSpec(id="t2", title="T2", goal="G", write_paths=["src/a/sub.py"]),
            TaskSpec(id="t3", title="T3", goal="G", write_paths=["src/b"]),
            TaskSpec(id="t4", title="T4", goal="G", write_paths=["src/c"], depends_on=["t1"]),
            TaskSpec(id="v1", title="V1", goal="G", role=TaskRole.VERIFIER, write_paths=[]),
        ]
        run = _make_run(tasks, max_concurrency=3)

        # t1 selected (reserves src/a); t2 overlaps t1 -> skipped; t3 selected; v1 selected (read-only)
        ready = select_ready_tasks(run)
        assert [t.id for t in ready] == ["t1", "t3", "v1"]

        # Excluding verifiers picks only t1 and t3
        ready_no_v = select_ready_tasks(run, include_verifiers=False)
        assert [t.id for t in ready_no_v] == ["t1", "t3"]

        # Lower max_concurrency limits batch size
        run.max_concurrency = 1
        assert [t.id for t in select_ready_tasks(run)] == ["t1"]

    def test_respects_reserved_paths_case_insensitivity_and_over_capacity(self) -> None:
        tasks = [
            TaskSpec(id="t1", title="T1", goal="G", write_paths=["Src/API"]),
            TaskSpec(id="t2", title="T2", goal="G", write_paths=["src/api/auth.py"]),
            TaskSpec(id="t3", title="T3", goal="G", write_paths=["docs"]),
        ]
        run = _make_run(tasks, max_concurrency=3)
        run.task_states["t1"].status = TaskStatus.RUNNING

        # Case-sensitive: t2 does NOT overlap Src/API
        assert [t.id for t in select_ready_tasks(run, ignore_case=False)] == ["t2", "t3"]
        # Case-insensitive: t2 overlaps Src/API -> skipped
        assert [t.id for t in select_ready_tasks(run, ignore_case=True)] == ["t3"]

        # Over capacity (max_concurrency=1, occupied=1 -> 0 slots)
        run.max_concurrency = 1
        assert select_ready_tasks(run) == []


class TestPropagateBlockedAndTraversal:
    def test_propagate_blocked_transitively_and_idempotently(self) -> None:
        tasks = [
            TaskSpec(id="a", title="A", goal="G", write_paths=["a.py"]),
            TaskSpec(id="b", title="B", goal="G", depends_on=["a"], write_paths=["b.py"]),
            TaskSpec(id="c", title="C", goal="G", depends_on=["b"], write_paths=["c.py"]),
            TaskSpec(id="x", title="X", goal="G", write_paths=["x.py"]),
            TaskSpec(id="y", title="Y", goal="G", depends_on=["x"], write_paths=["y.py"]),
        ]
        run = _make_run(tasks)
        run.task_states["a"].status = TaskStatus.FAILED
        run.task_states["a"].failure_reason = "unit tests failed"

        blocked = propagate_blocked(run)
        assert blocked == ["b", "c"]
        assert run.task_states["b"].status == TaskStatus.BLOCKED
        assert "a" in (run.task_states["b"].blocked_reason or "")
        assert "unit tests failed" in (run.task_states["b"].blocked_reason or "")
        assert run.task_states["c"].status == TaskStatus.BLOCKED
        assert "b" in (run.task_states["c"].blocked_reason or "")
        # Independent branch remains PENDING
        assert run.task_states["x"].status == TaskStatus.PENDING
        assert run.task_states["y"].status == TaskStatus.PENDING

        # Second pass is idempotent
        assert propagate_blocked(run) == []

    def test_downstream_and_prerequisite_order(self) -> None:
        tasks = [
            TaskSpec(id="t1", title="1", goal="G"),
            TaskSpec(id="t2", title="2", goal="G", depends_on=["t1"]),
            TaskSpec(id="t3", title="3", goal="G", depends_on=["t1"]),
            TaskSpec(id="t4", title="4", goal="G", depends_on=["t2", "t3"]),
            TaskSpec(id="t5", title="5", goal="G", depends_on=["t4"]),
            TaskSpec(id="other", title="O", goal="G"),
        ]
        assert downstream_task_ids(tasks, "t1") == ["t2", "t3", "t4", "t5"]
        assert downstream_task_ids(tasks, "t2") == ["t4", "t5"]
        assert downstream_task_ids(tasks, "t5") == []

        assert prerequisite_order(tasks, "t5") == ["t1", "t2", "t3", "t4"]
        assert prerequisite_order(tasks, "t2") == ["t1"]
        assert prerequisite_order(tasks, "t1") == []

        with pytest.raises(DagValidationError, match="Unknown task ID"):
            downstream_task_ids(tasks, "nope")
        with pytest.raises(DagValidationError, match="Unknown task ID"):
            prerequisite_order(tasks, "nope")
