"""Deterministic end-to-end tests for super_agent.engine.SuperAgentEngine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from super_agent.adapters.base import (
    AdapterCapabilities,
    AdapterHealth,
    CancelResult,
    HostSessionAdapter,
    WindowPollResult,
    WindowStartRequest,
)
from super_agent.engine import EngineError, SuperAgentEngine
from super_agent.models import (
    IsolationMode,
    RunStatus,
    TaskRole,
    TaskSpec,
    TaskStatus,
)
from super_agent.store import RunStore


def _git(workspace: Path, *args: str) -> bytes:
    res = subprocess.run(
        ["git", "-C", str(workspace), *args],
        shell=False,
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", errors="replace")
    return res.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Engine Test")
    _git(repo, "config", "user.email", "engine@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("baseline\n", encoding="utf-8")
    (repo / "outside.txt").write_text("do-not-touch\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "existing.md").write_text("# Existing\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "Initial baseline")
    return repo


@dataclass
class _FakeWindowState:
    request: WindowStartRequest
    session_id: str
    poll_result: WindowPollResult = field(
        default_factory=lambda: WindowPollResult(status="running", cleanup_confirmed=False)
    )
    cancel_result: CancelResult = field(
        default_factory=lambda: CancelResult(confirmed=True, detail="fake terminated")
    )
    cancel_calls: int = 0
    poll_calls: int = 0


class FakeAdapter(HostSessionAdapter):
    """Deterministic in-memory HostSessionAdapter for end-to-end engine testing."""

    host_name = "codex"

    def __init__(
        self,
        *,
        host_name: str = "codex",
        on_start: Callable[[WindowStartRequest, _FakeWindowState], None] | None = None,
    ) -> None:
        self.host_name = host_name
        self.on_start = on_start
        self.windows: dict[str, _FakeWindowState] = {}
        self.start_history: list[WindowStartRequest] = []
        self._seq = 0

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            ui_native_window=False,
            resumable=True,
            supports_model=True,
            supports_cancel=True,
            transport="fake",
        )

    def health_check(self, env: Mapping[str, str] | None = None) -> AdapterHealth:
        return AdapterHealth(host=self.host_name, available=True, version="fake-1.0")

    def start_window(self, request: WindowStartRequest) -> Any:
        from super_agent.models import SessionHandle

        self._seq += 1
        session_id = f"fake-sess-{self._seq}"
        win = _FakeWindowState(request=request, session_id=session_id)
        self.windows[request.attempt_id] = win
        self.start_history.append(request)
        if self.on_start is not None:
            self.on_start(request, win)
        return SessionHandle(
            host=self.host_name,
            session_id=session_id,
            title=f"{request.task.id}:{request.attempt_id}",
            status="running",
            pid=10000 + self._seq,
            resume_hint=f"fake resume {session_id}",
            log_path=str(request.log_path),
            metadata={"attempt_id": request.attempt_id},
        )

    def poll_window(
        self, handle: Any, env: Mapping[str, str] | None = None
    ) -> WindowPollResult:
        attempt_id = handle.metadata["attempt_id"]
        win = self.windows[attempt_id]
        win.poll_calls += 1
        res = win.poll_result
        return WindowPollResult(
            status=res.status,
            session_id=res.session_id or win.session_id,
            exit_code=res.exit_code,
            resume_hint=res.resume_hint or f"fake resume {win.session_id}",
            summary=res.summary,
            error=res.error,
            cleanup_confirmed=res.cleanup_confirmed,
            metadata=dict(res.metadata),
        )

    def cancel_window(
        self, handle: Any, env: Mapping[str, str] | None = None
    ) -> CancelResult:
        attempt_id = handle.metadata["attempt_id"]
        win = self.windows[attempt_id]
        win.cancel_calls += 1
        if win.cancel_result.confirmed:
            win.poll_result = WindowPollResult(
                status="cancelled",
                session_id=win.session_id,
                cleanup_confirmed=True,
            )
        return win.cancel_result

    def complete_success(
        self,
        attempt_id: str,
        *,
        summary: str = "Done",
        files_changed: list[str] | None = None,
        tests: list[dict[str, str]] | None = None,
        issues: list[str] | None = None,
        write_to_last_message_only: bool = False,
        extra_result_fields: dict[str, Any] | None = None,
    ) -> None:
        win = self.windows[attempt_id]
        payload: dict[str, Any] = {
            "task_id": win.request.task.id,
            "session_id": win.session_id,
            "status": "succeeded",
            "summary": summary,
            "files_changed": files_changed or [],
            "tests": tests or [{"check": "unit", "outcome": "passed (exit 0)"}],
            "issues": issues or [],
            "artifacts": [],
            "next_action": None,
            "retryable": False,
        }
        if extra_result_fields:
            payload.update(extra_result_fields)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        if write_to_last_message_only:
            win.request.last_message_path.write_text(
                f"Done!\n```json\n{serialized}\n```\n", encoding="utf-8"
            )
        else:
            win.request.result_path.write_text(serialized, encoding="utf-8")
        win.poll_result = WindowPollResult(
            status="completed",
            session_id=win.session_id,
            exit_code=0,
            cleanup_confirmed=True,
        )

    def complete_failure(
        self,
        attempt_id: str,
        *,
        summary: str = "Task failed",
        issues: list[str] | None = None,
        retryable: bool = False,
        exit_code: int = 1,
        cleanup_confirmed: bool = True,
        write_result: bool = True,
        poll_error: str | None = None,
    ) -> None:
        win = self.windows[attempt_id]
        if write_result:
            payload = {
                "task_id": win.request.task.id,
                "session_id": win.session_id,
                "status": "failed",
                "summary": summary,
                "files_changed": [],
                "tests": [{"check": "unit", "outcome": f"failed (exit {exit_code})"}],
                "issues": issues or [summary],
                "artifacts": [],
                "next_action": None,
                "retryable": retryable,
            }
            win.request.result_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        win.poll_result = WindowPollResult(
            status="failed",
            session_id=win.session_id,
            exit_code=exit_code,
            error=poll_error or summary,
            cleanup_confirmed=cleanup_confirmed,
        )


def _make_engine(
    tmp_path: Path,
    adapter: FakeAdapter,
    *,
    store: RunStore | None = None,
) -> tuple[SuperAgentEngine, RunStore]:
    run_store = store or RunStore(tmp_path / "runs")
    engine = SuperAgentEngine(
        store=run_store,
        adapter_factory=lambda host, env, ws: adapter,
    )
    return engine, run_store


# ---------------------------------------------------------------------------
# P2-1: DAG validation rejects invalid plans with zero dispatch & zero persistence
# ---------------------------------------------------------------------------


class TestPlanValidationAndApprovalGate:
    @pytest.mark.parametrize(
        ("bad_tasks", "error_fragment"),
        [
            (
                [
                    {"id": "dup", "title": "First", "goal": "g1", "write_paths": ["a.txt"]},
                    {"id": "dup", "title": "Second", "goal": "g2", "write_paths": ["b.txt"]},
                ],
                "Duplicate task ID",
            ),
            (
                [
                    {
                        "id": "a",
                        "title": "A",
                        "goal": "g1",
                        "depends_on": ["missing-node"],
                        "write_paths": ["a.txt"],
                    }
                ],
                "missing task",
            ),
            (
                [
                    {"id": "a", "title": "A", "goal": "g1", "depends_on": ["b"]},
                    {"id": "b", "title": "B", "goal": "g2", "depends_on": ["a"]},
                ],
                "cycle",
            ),
            (
                [
                    {
                        "id": "badpath",
                        "title": "Escape",
                        "goal": "g1",
                        "write_paths": ["../escape.txt"],
                    }
                ],
                "traversal",
            ),
            (
                [
                    {
                        "id": "badverify",
                        "title": "Writable verifier",
                        "goal": "g1",
                        "role": "verifier",
                        "write_paths": ["a.txt"],
                    }
                ],
                "empty write_paths",
            ),
        ],
    )
    def test_invalid_dag_rejected_zero_dispatch_zero_persistence(
        self,
        tmp_path: Path,
        git_repo: Path,
        bad_tasks: list[dict[str, Any]],
        error_fragment: str,
    ) -> None:
        adapter = FakeAdapter()
        engine, store = _make_engine(tmp_path, adapter)

        with pytest.raises(EngineError, match=error_fragment):
            engine.create_plan(git_repo, bad_tasks, host="codex")

        assert adapter.start_history == []
        assert store.list_runs() == []
        assert not store.base_dir.exists() or list(store.base_dir.iterdir()) == []

    def test_invalid_workspace_and_isolation_mode_rejected(self, tmp_path: Path) -> None:
        adapter = FakeAdapter()
        engine, _ = _make_engine(tmp_path, adapter)

        with pytest.raises(EngineError, match="does not exist"):
            engine.create_plan(
                tmp_path / "no-such-dir",
                [{"id": "a", "title": "A", "goal": "G"}],
                host="codex",
            )

        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        with pytest.raises(EngineError, match="requires a Git repository"):
            engine.create_plan(
                plain_dir,
                [{"id": "a", "title": "A", "goal": "G"}],
                host="codex",
                isolation_mode="worktree",
            )

    def test_approval_gate_blocks_draft_dispatch_and_is_idempotent(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        engine, _ = _make_engine(tmp_path, adapter)

        plan = engine.create_plan(
            git_repo,
            [{"id": "t1", "title": "Task 1", "goal": "Do work", "write_paths": ["a.txt"]}],
            host="codex",
        )
        run_id = plan["run_id"]
        assert plan["status"] == RunStatus.DRAFT.value
        assert plan["requires_approval"] is True
        assert plan["isolation_mode"] == IsolationMode.WORKTREE.value
        assert plan["base_commit"]
        assert "| Task ID |" in plan["plan_table"]
        assert [t["id"] for t in plan["tasks"]] == ["t1", "verify"]
        assert plan["tasks"][-1]["role"] == TaskRole.VERIFIER.value
        assert plan["tasks"][-1]["write_paths"] == []
        assert plan["tasks"][-1]["depends_on"] == ["t1"]

        # Must refuse dispatch before approval.
        with pytest.raises(EngineError, match="draft"):
            engine.dispatch_ready(run_id)
        assert adapter.start_history == []

        # Approve is idempotent while approved.
        approved_1 = engine.approve_plan(run_id)
        approved_2 = engine.approve_plan(run_id)
        assert approved_1["status"] == RunStatus.APPROVED.value
        assert approved_2["status"] == RunStatus.APPROVED.value

        # Dispatch now succeeds.
        disp = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in disp["dispatched"]] == ["t1"]
        assert disp["status"] == RunStatus.RUNNING.value

        # Approving a running run is rejected.
        with pytest.raises(EngineError, match="running"):
            engine.approve_plan(run_id)


# ---------------------------------------------------------------------------
# P2-1 + P2-4: Full Git worktree E2E lifecycle
# ---------------------------------------------------------------------------


class TestFullGitWorktreeLifecycle:
    def test_parallel_overlap_dependency_integration_verify_and_apply(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        engine, store = _make_engine(tmp_path, adapter)

        tasks = [
            {
                "id": "w1",
                "title": "Create module A",
                "goal": "Create src/a.txt",
                "write_paths": ["src/a.txt"],
            },
            {
                "id": "w2",
                "title": "Write docs one",
                "goal": "Create docs/one.md",
                "write_paths": ["docs/"],
            },
            {
                "id": "w3",
                "title": "Write docs two (overlaps docs/)",
                "goal": "Create docs/two.md",
                "write_paths": ["docs/two.md"],
            },
            {
                "id": "w4",
                "title": "Combine from w1",
                "goal": "Read src/a.txt and write src/combined.txt",
                "depends_on": ["w1"],
                "write_paths": ["src/combined.txt"],
            },
        ]

        plan = engine.create_plan(git_repo, tasks, max_concurrency=3, host="codex")
        run_id = plan["run_id"]
        engine.approve_plan(run_id)

        # Wave 1: w1 and w2 dispatch in parallel; w3 serialized due to docs/ overlap; w4 waits for w1.
        d1 = engine.dispatch_ready(run_id)
        assert {item["task_id"] for item in d1["dispatched"]} == {"w1", "w2"}
        assert d1["active_count"] == 2
        assert d1["task_statuses"]["w3"] == TaskStatus.PENDING.value
        assert d1["task_statuses"]["w4"] == TaskStatus.PENDING.value
        assert d1["task_statuses"]["verify"] == TaskStatus.PENDING.value

        # Workers edit inside their detached worktrees.
        w1_wt = Path(adapter.windows["w1-a1"].request.workspace)
        w2_wt = Path(adapter.windows["w2-a1"].request.workspace)
        assert w1_wt != git_repo and w2_wt != git_repo and w1_wt != w2_wt

        (w1_wt / "src").mkdir(parents=True, exist_ok=True)
        (w1_wt / "src" / "a.txt").write_text("ALPHA-PAYLOAD\n", encoding="utf-8")
        adapter.complete_success("w1-a1", summary="Created src/a.txt", files_changed=["src/a.txt"])

        (w2_wt / "docs" / "one.md").write_text("DOC ONE\n", encoding="utf-8")
        adapter.complete_success("w2-a1", summary="Created docs/one.md", files_changed=["docs/one.md"])

        p1 = engine.poll_run(run_id)
        assert p1["task_statuses"]["w1"] == TaskStatus.SUCCEEDED.value
        assert p1["task_statuses"]["w2"] == TaskStatus.SUCCEEDED.value
        assert p1["active_count"] == 0
        assert p1["ready_count"] == 2
        assert p1["all_terminal"] is False

        # Source workspace must still be completely untouched.
        assert _git(git_repo, "status", "--porcelain") == b""
        assert not (git_repo / "src").exists()
        assert not (git_repo / "docs" / "one.md").exists()

        # Wave 2: w3 (docs/ free now) and w4 (w1 succeeded) dispatch together.
        d2 = engine.dispatch_ready(run_id)
        assert {item["task_id"] for item in d2["dispatched"]} == {"w3", "w4"}

        w3_wt = Path(adapter.windows["w3-a1"].request.workspace)
        w4_wt = Path(adapter.windows["w4-a1"].request.workspace)

        # P2-4: downstream worker w4 MUST read prerequisite w1's deliverable in its worktree!
        assert (w4_wt / "src" / "a.txt").read_text(encoding="utf-8") == "ALPHA-PAYLOAD\n"
        upstream_content = (w4_wt / "src" / "a.txt").read_text(encoding="utf-8").strip()
        (w4_wt / "src" / "combined.txt").write_text(f"COMBINED:{upstream_content}\n", encoding="utf-8")
        adapter.complete_success(
            "w4-a1",
            summary="Combined src/a.txt into src/combined.txt",
            files_changed=["src/combined.txt"],
        )

        (w3_wt / "docs" / "two.md").write_text("DOC TWO\n", encoding="utf-8")
        adapter.complete_success("w3-a1", summary="Created docs/two.md", files_changed=["docs/two.md"])

        p2 = engine.poll_run(run_id)
        assert p2["task_statuses"]["w3"] == TaskStatus.SUCCEEDED.value
        assert p2["task_statuses"]["w4"] == TaskStatus.SUCCEEDED.value
        assert p2["task_statuses"]["verify"] == TaskStatus.PENDING.value
        assert p2["ready_count"] == 1

        # Wave 3: dispatch_ready auto-prepares integration worktree + final.patch, then starts verifier.
        d3 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d3["dispatched"]] == ["verify"]
        assert d3["dispatched"][0]["role"] == TaskRole.VERIFIER.value
        assert d3["status"] == RunStatus.VERIFYING.value

        verify_req = adapter.windows["verify-a1"].request
        assert verify_req.read_only is True
        assert "super_agent.worktree" in verify_req.prompt_text
        assert "--workspace" in verify_req.prompt_text
        assert "--output" in verify_req.prompt_text

        # Applying before verifier succeeds MUST be rejected.
        with pytest.raises(EngineError, match="verifier tasks have not succeeded"):
            engine.apply_integration(run_id)

        # Verifier inspects integrated worktree AND runs the real worktree verify CLI!
        state_before_verify = store.load_run(run_id)
        integ_wt = Path(state_before_verify.integration.worktree_path or "")
        final_patch = Path(state_before_verify.integration.patch_path or "")
        assert verify_req.workspace == integ_wt
        assert (integ_wt / "src" / "a.txt").read_text(encoding="utf-8") == "ALPHA-PAYLOAD\n"
        assert (integ_wt / "src" / "combined.txt").read_text(encoding="utf-8") == "COMBINED:ALPHA-PAYLOAD\n"
        assert (integ_wt / "docs" / "one.md").read_text(encoding="utf-8") == "DOC ONE\n"
        assert (integ_wt / "docs" / "two.md").read_text(encoding="utf-8") == "DOC TWO\n"

        verify_argv = [
            sys.executable,
            "-m",
            "super_agent.worktree",
            "verify",
            "--workspace",
            str(integ_wt),
            "--output",
            str(final_patch),
        ]
        for sp in state_before_verify.integration.source_patch_paths:
            verify_argv.extend(["--patch", sp])
        cli_res = subprocess.run(verify_argv, shell=False, capture_output=True, check=False)
        assert cli_res.returncode == 0, cli_res.stderr.decode("utf-8", errors="replace")

        adapter.complete_success(
            "verify-a1",
            summary="Verified integrated worktree and patch replay",
            files_changed=[],
            tests=[{"check": "super_agent.worktree verify", "outcome": "passed (exit 0)"}],
        )

        p3 = engine.poll_run(run_id)
        assert p3["status"] == RunStatus.COMPLETED.value
        assert p3["all_terminal"] is True
        assert p3["task_statuses"]["verify"] == TaskStatus.SUCCEEDED.value

        # Summarize run: verify JSON serializable, full fields, diff_stat, and pending-apply warning.
        summary = engine.summarize_run(run_id)
        json.dumps(summary)
        assert summary["status"] == RunStatus.COMPLETED.value
        assert summary["verification"]["passed"] is True
        assert summary["integration"]["status"] == "verified"
        assert summary["integration"]["diff_stat"]
        assert any("has not been applied" in w for w in summary["warnings"])
        assert "| Task | Role | Status |" in summary["markdown"]

        # Single-host guarantee: every attempt used 'codex'.
        final_state = store.load_run(run_id)
        assert all(att.host == "codex" for att in final_state.attempts.values())

        # Source workspace STILL untouched until explicit apply_integration!
        assert _git(git_repo, "status", "--porcelain") == b""
        assert not (git_repo / "src").exists()

        # Dry-run check_only passes without modifying source workspace.
        chk = engine.apply_integration(run_id, check_only=True)
        assert chk["check_only"] is True
        assert chk["applied"] is False
        assert chk["status"] == "check_passed"
        assert _git(git_repo, "status", "--porcelain") == b""

        # Explicit apply_integration applies all verified changes to source workspace.
        applied = engine.apply_integration(run_id, check_only=False)
        assert applied["applied"] is True
        assert applied["status"] == "applied"
        assert applied["applied_at"]
        assert (git_repo / "src" / "a.txt").read_text(encoding="utf-8") == "ALPHA-PAYLOAD\n"
        assert (git_repo / "src" / "combined.txt").read_text(encoding="utf-8") == "COMBINED:ALPHA-PAYLOAD\n"
        assert (git_repo / "docs" / "one.md").read_text(encoding="utf-8") == "DOC ONE\n"
        assert (git_repo / "docs" / "two.md").read_text(encoding="utf-8") == "DOC TWO\n"

        # Double apply is refused.
        with pytest.raises(EngineError, match="already been applied"):
            engine.apply_integration(run_id)

        runs = engine.list_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# P2-4: Scope escape interception (no auto-retry, downstream blocked, source untouched)
# ---------------------------------------------------------------------------


class TestScopeEscapeInterception:
    def test_worker_scope_escape_fails_without_retry_and_blocks_downstream(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        engine, store = _make_engine(tmp_path, adapter)

        plan = engine.create_plan(
            git_repo,
            [
                {
                    "id": "rogue",
                    "title": "Rogue worker",
                    "goal": "Only touch allowed.txt",
                    "write_paths": ["allowed.txt"],
                    "max_retries": 1,
                },
                {
                    "id": "after",
                    "title": "Downstream task",
                    "goal": "Depends on rogue",
                    "depends_on": ["rogue"],
                    "write_paths": ["after.txt"],
                },
            ],
            host="codex",
        )
        run_id = plan["run_id"]
        engine.approve_plan(run_id)

        d1 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d1["dispatched"]] == ["rogue"]

        rogue_wt = Path(adapter.windows["rogue-a1"].request.workspace)
        (rogue_wt / "allowed.txt").write_text("ok\n", encoding="utf-8")
        (rogue_wt / "outside.txt").write_text("ESCAPED!\n", encoding="utf-8")
        adapter.complete_success(
            "rogue-a1",
            summary="Claimed success despite escaping write scope",
            files_changed=["allowed.txt"],
            extra_result_fields={"retryable": True},
        )

        p1 = engine.poll_run(run_id)
        assert p1["task_statuses"]["rogue"] == TaskStatus.FAILED.value
        assert p1["task_statuses"]["after"] == TaskStatus.BLOCKED.value
        assert p1["task_statuses"]["verify"] == TaskStatus.BLOCKED.value
        assert p1["status"] == RunStatus.FAILED.value
        assert p1["all_terminal"] is True

        state = store.load_run(run_id)
        assert state.task_states["rogue"].retries_used == 0
        assert len(state.task_states["rogue"].attempt_ids) == 1
        assert "scope_violation" in (state.task_states["rogue"].failure_reason or "")
        assert "outside.txt" in (state.task_states["rogue"].failure_reason or "")
        assert state.task_states["after"].attempt_ids == []

        # Source workspace untouched.
        assert (git_repo / "outside.txt").read_text(encoding="utf-8") == "do-not-touch\n"
        assert _git(git_repo, "status", "--porcelain") == b""

        events = store.read_events(run_id)
        assert any(ev["event_type"] == "attempt_scope_violation" for ev in events)


# ---------------------------------------------------------------------------
# P2-2: Persistence, crash/resume recovery & targeted retry_task
# ---------------------------------------------------------------------------


class TestPersistenceRecoveryAndRetryTask:
    def test_crash_resume_and_targeted_retry_preserves_succeeded_task(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        store = RunStore(tmp_path / "runs")
        adapter1 = FakeAdapter()
        engine1 = SuperAgentEngine(store=store, adapter_factory=lambda h, e, w: adapter1)

        plan = engine1.create_plan(
            git_repo,
            [
                {
                    "id": "indep",
                    "title": "Independent",
                    "goal": "Write indep.txt",
                    "write_paths": ["indep.txt"],
                },
                {
                    "id": "broken",
                    "title": "Initially broken",
                    "goal": "Write broken.txt",
                    "write_paths": ["broken.txt"],
                },
                {
                    "id": "downstream",
                    "title": "Downstream of broken",
                    "goal": "Read broken.txt and write down.txt",
                    "depends_on": ["broken"],
                    "write_paths": ["down.txt"],
                },
            ],
            max_concurrency=3,
            host="codex",
        )
        run_id = plan["run_id"]
        engine1.approve_plan(run_id)

        d1 = engine1.dispatch_ready(run_id)
        assert {item["task_id"] for item in d1["dispatched"]} == {"indep", "broken"}

        indep_wt = Path(adapter1.windows["indep-a1"].request.workspace)
        (indep_wt / "indep.txt").write_text("INDEP-OK\n", encoding="utf-8")
        adapter1.complete_success("indep-a1", summary="Indep done", files_changed=["indep.txt"])

        # Permanent non-retryable failure on broken.
        adapter1.complete_failure(
            "broken-a1",
            summary="Permanent compile error",
            issues=["exit code 23"],
            retryable=False,
            exit_code=23,
        )

        p1 = engine1.poll_run(run_id)
        assert p1["status"] == RunStatus.FAILED.value
        assert p1["task_statuses"] == {
            "indep": TaskStatus.SUCCEEDED.value,
            "broken": TaskStatus.FAILED.value,
            "downstream": TaskStatus.BLOCKED.value,
            "verify": TaskStatus.BLOCKED.value,
        }
        assert p1["all_terminal"] is True

        # Crash / restart simulation: brand-new engine + adapter from same RunStore!
        adapter2 = FakeAdapter()
        engine2 = SuperAgentEngine(store=store, adapter_factory=lambda h, e, w: adapter2)

        pre_summary = engine2.summarize_run(run_id)
        by_id = {entry["task_id"]: entry for entry in pre_summary["tasks"]}
        assert by_id["indep"]["status"] == TaskStatus.SUCCEEDED.value
        assert by_id["indep"]["attempts_count"] == 1
        assert by_id["broken"]["status"] == TaskStatus.FAILED.value
        assert "exit code 23" in (by_id["broken"]["failure_reason"] or "")
        assert by_id["downstream"]["status"] == TaskStatus.BLOCKED.value

        # Refuses retrying an already-succeeded node.
        with pytest.raises(EngineError, match="already succeeded"):
            engine2.retry_task(run_id, "indep")

        # Targeted retry of broken resets broken + downstream + verify, leaving indep untouched.
        retried = engine2.retry_task(run_id, "broken")
        assert retried["status"] == RunStatus.RUNNING.value
        assert retried["retried_task_id"] == "broken"
        assert set(retried["reset_dependents"]) == {"downstream", "verify"}
        assert retried["task_statuses"]["indep"] == TaskStatus.SUCCEEDED.value
        assert retried["task_statuses"]["broken"] == TaskStatus.PENDING.value
        assert retried["task_statuses"]["downstream"] == TaskStatus.PENDING.value
        assert retried["task_statuses"]["verify"] == TaskStatus.PENDING.value

        # Dispatch broken attempt 2 ONLY (indep MUST NOT be re-dispatched).
        d2 = engine2.dispatch_ready(run_id)
        assert [item["task_id"] for item in d2["dispatched"]] == ["broken"]
        assert d2["dispatched"][0]["attempt_id"] == "broken-a2"

        broken_wt = Path(adapter2.windows["broken-a2"].request.workspace)
        (broken_wt / "broken.txt").write_text("FIXED-BROKEN\n", encoding="utf-8")
        adapter2.complete_success("broken-a2", summary="Fixed", files_changed=["broken.txt"])

        p2 = engine2.poll_run(run_id)
        assert p2["task_statuses"]["broken"] == TaskStatus.SUCCEEDED.value

        # Now downstream unlocks and reads broken.txt!
        d3 = engine2.dispatch_ready(run_id)
        assert [item["task_id"] for item in d3["dispatched"]] == ["downstream"]
        down_wt = Path(adapter2.windows["downstream-a1"].request.workspace)
        assert (down_wt / "broken.txt").read_text(encoding="utf-8") == "FIXED-BROKEN\n"
        (down_wt / "down.txt").write_text("DOWN-AFTER-FIXED\n", encoding="utf-8")
        adapter2.complete_success("downstream-a1", summary="Down done", files_changed=["down.txt"])
        engine2.poll_run(run_id)

        # Fresh integration and verifier run.
        d4 = engine2.dispatch_ready(run_id)
        assert [item["task_id"] for item in d4["dispatched"]] == ["verify"]
        adapter2.complete_success("verify-a1", summary="All verified")
        p4 = engine2.poll_run(run_id)
        assert p4["status"] == RunStatus.COMPLETED.value

        # Confirm indep STILL has exactly 1 attempt across the entire run!
        state = store.load_run(run_id)
        assert state.task_states["indep"].attempt_ids == ["indep-a1"]
        assert state.task_states["broken"].attempt_ids == ["broken-a1", "broken-a2"]
        assert state.task_states["downstream"].attempt_ids == ["downstream-a1"]

        # Apply and verify all 3 files land in source workspace.
        engine2.apply_integration(run_id)
        assert (git_repo / "indep.txt").read_text(encoding="utf-8") == "INDEP-OK\n"
        assert (git_repo / "broken.txt").read_text(encoding="utf-8") == "FIXED-BROKEN\n"
        assert (git_repo / "down.txt").read_text(encoding="utf-8") == "DOWN-AFTER-FIXED\n"

    def test_retry_after_prior_integration_rebuilds_clean_integration_seq(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        """Verify rebuilding integration after a failed verifier overwrites stale final.patch safely."""
        adapter = FakeAdapter()
        engine, store = _make_engine(tmp_path, adapter)

        plan = engine.create_plan(
            git_repo,
            [{"id": "w1", "title": "W1", "goal": "Write f.txt", "write_paths": ["f.txt"]}],
            host="codex",
        )
        run_id = plan["run_id"]
        engine.approve_plan(run_id)
        engine.dispatch_ready(run_id)
        wt = Path(adapter.windows["w1-a1"].request.workspace)
        (wt / "f.txt").write_text("hello\n", encoding="utf-8")
        adapter.complete_success("w1-a1", summary="ok", files_changed=["f.txt"])
        engine.poll_run(run_id)

        # First integration + verifier fails.
        engine.dispatch_ready(run_id)
        first_wt = store.load_run(run_id).integration.worktree_path
        adapter.complete_failure("verify-a1", summary="Verifier check failed", retryable=False)
        engine.poll_run(run_id)
        assert store.load_run(run_id).task_states["verify"].status == TaskStatus.FAILED

        # Retry verify -> resets integration -> builds integration-2 cleanly even though final.patch exists.
        engine.retry_task(run_id, "verify")
        d_retry = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d_retry["dispatched"]] == ["verify"]
        assert d_retry["dispatched"][0]["attempt_id"] == "verify-a2"

        second_state = store.load_run(run_id)
        assert second_state.integration.worktree_path != first_wt
        assert second_state.integration.status == "ready"

        adapter.complete_success("verify-a2", summary="Verifier passed on retry")
        p_done = engine.poll_run(run_id)
        assert p_done["status"] == RunStatus.COMPLETED.value
        assert store.load_run(run_id).integration.status == "verified"


# ---------------------------------------------------------------------------
# P2-3: Timeout with unconfirmed cancel + retryable transient failure (max 1)
# ---------------------------------------------------------------------------


class TestTimeoutAndRetrySemantics:
    def test_timeout_unconfirmed_cancel_retains_slot_and_write_scope_until_confirmed(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        engine, store = _make_engine(tmp_path, adapter)

        # Part A: concurrency slot retention (max_concurrency=1)
        plan = engine.create_plan(
            git_repo,
            [
                {
                    "id": "slow",
                    "title": "Slow worker",
                    "goal": "Times out",
                    "write_paths": ["shared/a.txt"],
                    "timeout_sec": 5,
                },
                {
                    "id": "disjoint",
                    "title": "Disjoint worker",
                    "goal": "Waits for slot",
                    "write_paths": ["other.txt"],
                },
            ],
            max_concurrency=1,
            host="codex",
            auto_add_verifier=False,
        )
        run_id = plan["run_id"]
        engine.approve_plan(run_id)

        d1 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d1["dispatched"]] == ["slow"]

        # Configure cancel_window to NOT confirm termination yet.
        adapter.windows["slow-a1"].cancel_result = CancelResult(
            confirmed=False, detail="process still exiting"
        )
        # Backdate started_at to force deterministic timeout without sleeping.
        state = store.load_run(run_id)
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        state.attempts["slow-a1"].started_at = old_time
        store.save_run(state)

        p1 = engine.poll_run(run_id)
        assert p1["task_statuses"]["slow"] == TaskStatus.TIMED_OUT.value
        assert p1["occupied_slots"] == 1
        assert p1["ready_count"] == 0
        assert p1["all_terminal"] is False
        assert adapter.windows["slow-a1"].cancel_calls == 1

        # Slot is retained: dispatch_ready MUST NOT start 'disjoint'.
        d2 = engine.dispatch_ready(run_id)
        assert d2["dispatched"] == []

        # Retrying 'slow' while cleanup is unconfirmed MUST be refused.
        with pytest.raises(EngineError, match="confirmed cleanup"):
            engine.retry_task(run_id, "slow")

        # Now process finally exits and poll_window confirms cleanup.
        adapter.windows["slow-a1"].poll_result = WindowPollResult(
            status="cancelled",
            cleanup_confirmed=True,
        )
        p2 = engine.poll_run(run_id)
        assert p2["occupied_slots"] == 0
        assert p2["ready_count"] == 1
        assert any(c.get("event") == "cleanup_confirmed" for c in p2["changed_tasks"])

        # Slot is now released: 'disjoint' dispatches immediately!
        d3 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d3["dispatched"]] == ["disjoint"]

    def test_unconfirmed_timeout_retains_write_scope_while_free_slot_allows_disjoint(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        engine, store = _make_engine(tmp_path, adapter)

        plan = engine.create_plan(
            git_repo,
            [
                {
                    "id": "holder",
                    "title": "Scope holder",
                    "goal": "Holds shared/",
                    "write_paths": ["shared/"],
                    "timeout_sec": 5,
                },
                {
                    "id": "overlap",
                    "title": "Overlaps shared/",
                    "goal": "Needs shared/sub.txt",
                    "write_paths": ["shared/sub.txt"],
                },
                {
                    "id": "free",
                    "title": "Disjoint",
                    "goal": "Needs free.txt",
                    "write_paths": ["free.txt"],
                },
            ],
            max_concurrency=2,
            host="codex",
            auto_add_verifier=False,
        )
        run_id = plan["run_id"]
        engine.approve_plan(run_id)

        # Wave 1: holder + free run (overlap blocked by holder's write_paths).
        d1 = engine.dispatch_ready(run_id)
        assert {item["task_id"] for item in d1["dispatched"]} == {"holder", "free"}

        # Finish 'free' so 1 of 2 concurrency slots is free; time out 'holder' with unconfirmed cancel.
        free_wt = Path(adapter.windows["free-a1"].request.workspace)
        (free_wt / "free.txt").write_text("ok\n", encoding="utf-8")
        adapter.complete_success("free-a1", summary="free done", files_changed=["free.txt"])
        adapter.windows["holder-a1"].cancel_result = CancelResult(
            confirmed=False, detail="still running"
        )
        state = store.load_run(run_id)
        state.attempts["holder-a1"].started_at = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        store.save_run(state)

        p1 = engine.poll_run(run_id)
        assert p1["task_statuses"]["free"] == TaskStatus.SUCCEEDED.value
        assert p1["task_statuses"]["holder"] == TaskStatus.TIMED_OUT.value
        assert p1["occupied_slots"] == 1  # 1 of 2 slots free, BUT shared/ is still reserved!

        # Even with a free slot, 'overlap' MUST NOT dispatch while holder's cleanup is unconfirmed!
        d2 = engine.dispatch_ready(run_id)
        assert d2["dispatched"] == []

        # Confirm holder cleanup -> write scope releases -> 'overlap' dispatches!
        adapter.windows["holder-a1"].poll_result = WindowPollResult(
            status="cancelled", cleanup_confirmed=True
        )
        engine.poll_run(run_id)
        d3 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d3["dispatched"]] == ["overlap"]

    def test_retryable_transient_failure_retries_at_most_once_with_fresh_window(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        engine, store = _make_engine(tmp_path, adapter)

        plan = engine.create_plan(
            git_repo,
            [
                {
                    "id": "transient_ok",
                    "title": "Succeeds on retry",
                    "goal": "Write ok.txt",
                    "write_paths": ["ok.txt"],
                    "max_retries": 1,
                },
                {
                    "id": "transient_exhaust",
                    "title": "Always transient fail",
                    "goal": "Write bad.txt",
                    "write_paths": ["bad.txt"],
                    "max_retries": 3,  # Clamped to 1 by TaskSpec!
                },
                {
                    "id": "after_exhaust",
                    "title": "Downstream of exhaust",
                    "goal": "Depends on transient_exhaust",
                    "depends_on": ["transient_exhaust"],
                    "write_paths": ["after.txt"],
                },
            ],
            max_concurrency=3,
            host="codex",
            auto_add_verifier=False,
        )
        run_id = plan["run_id"]
        engine.approve_plan(run_id)

        # Attempt 1 for both.
        d1 = engine.dispatch_ready(run_id)
        assert {item["task_id"] for item in d1["dispatched"]} == {
            "transient_ok",
            "transient_exhaust",
        }
        first_ok_wt = adapter.windows["transient_ok-a1"].request.workspace
        first_ok_sess = adapter.windows["transient_ok-a1"].session_id

        adapter.complete_failure(
            "transient_ok-a1", summary="HTTP 429 rate limit", retryable=True
        )
        adapter.complete_failure(
            "transient_exhaust-a1", summary="HTTP 503 unavailable", retryable=True
        )

        p1 = engine.poll_run(run_id)
        # Both should be reset to PENDING for their single auto-retry, NOT FAILED, and downstream NOT blocked.
        assert p1["task_statuses"]["transient_ok"] == TaskStatus.PENDING.value
        assert p1["task_statuses"]["transient_exhaust"] == TaskStatus.PENDING.value
        assert p1["task_statuses"]["after_exhaust"] == TaskStatus.PENDING.value

        # Attempt 2: fresh attempts, fresh worktrees, fresh window sessions!
        d2 = engine.dispatch_ready(run_id)
        assert {item["attempt_id"] for item in d2["dispatched"]} == {
            "transient_ok-a2",
            "transient_exhaust-a2",
        }
        second_ok_wt = adapter.windows["transient_ok-a2"].request.workspace
        second_ok_sess = adapter.windows["transient_ok-a2"].session_id
        assert second_ok_wt != first_ok_wt
        assert second_ok_sess != first_ok_sess

        (second_ok_wt / "ok.txt").write_text("RECOVERED\n", encoding="utf-8")
        adapter.complete_success(
            "transient_ok-a2",
            summary="Succeeded on attempt 2",
            files_changed=["ok.txt"],
            write_to_last_message_only=True,  # Also exercise last_message_path fallback!
        )
        # Second retryable failure on transient_exhaust must NOT retry a 3rd time!
        adapter.complete_failure(
            "transient_exhaust-a2", summary="HTTP 503 again", retryable=True
        )

        p2 = engine.poll_run(run_id)
        assert p2["task_statuses"]["transient_ok"] == TaskStatus.SUCCEEDED.value
        assert p2["task_statuses"]["transient_exhaust"] == TaskStatus.FAILED.value
        assert p2["task_statuses"]["after_exhaust"] == TaskStatus.BLOCKED.value
        assert p2["status"] == RunStatus.FAILED.value
        assert p2["all_terminal"] is True

        state = store.load_run(run_id)
        assert state.task_states["transient_exhaust"].retries_used == 1
        assert state.task_states["transient_exhaust"].attempt_ids == [
            "transient_exhaust-a1",
            "transient_exhaust-a2",
        ]
        # Verify fallback normalization note surfaces in summarize_run warnings.
        summary = engine.summarize_run(run_id)
        assert any("last_message_path" in w for w in summary["warnings"])


# ---------------------------------------------------------------------------
# Non-Git fallback to SHARED mode + wait_step + cancel_run + doctor
# ---------------------------------------------------------------------------


class TestSharedModeCancelWaitAndDoctor:
    def test_non_git_dir_falls_back_to_shared_with_overlap_serialization(
        self, tmp_path: Path
    ) -> None:
        plain_ws = tmp_path / "plain_workspace"
        plain_ws.mkdir()
        (plain_ws / "app").mkdir()

        adapter = FakeAdapter()
        engine, _ = _make_engine(tmp_path, adapter)

        plan = engine.create_plan(
            plain_ws,
            [
                {
                    "id": "s1",
                    "title": "Edit app dir",
                    "goal": "Create app/one.txt",
                    "write_paths": ["app/"],
                },
                {
                    "id": "s2",
                    "title": "Edit app/two.txt (overlaps app/)",
                    "goal": "Create app/two.txt",
                    "write_paths": ["app/two.txt"],
                },
            ],
            max_concurrency=2,
            host="codex",
            isolation_mode="auto",
        )
        run_id = plan["run_id"]
        assert plan["isolation_mode"] == IsolationMode.SHARED.value
        assert plan["base_commit"] is None

        engine.approve_plan(run_id)

        # Overlap serialization: only s1 dispatches first, directly in plain_ws.
        d1 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d1["dispatched"]] == ["s1"]
        assert Path(d1["dispatched"][0]["worktree_path"]) == plain_ws.resolve()

        (plain_ws / "app" / "one.txt").write_text("ONE\n", encoding="utf-8")
        adapter.complete_success("s1-a1", summary="Created one.txt", files_changed=["app/one.txt"])
        engine.poll_run(run_id)

        # Now s2 dispatches in plain_ws.
        d2 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d2["dispatched"]] == ["s2"]
        (plain_ws / "app" / "two.txt").write_text("TWO\n", encoding="utf-8")
        adapter.complete_success("s2-a1", summary="Created two.txt", files_changed=["app/two.txt"])
        engine.poll_run(run_id)

        # Verifier dispatches in plain_ws (read-only).
        d3 = engine.dispatch_ready(run_id)
        assert [item["task_id"] for item in d3["dispatched"]] == ["verify"]
        assert adapter.windows["verify-a1"].request.read_only is True
        assert adapter.windows["verify-a1"].request.workspace == plain_ws.resolve()
        adapter.complete_success("verify-a1", summary="Verified shared workspace")

        p3 = engine.poll_run(run_id)
        assert p3["status"] == RunStatus.COMPLETED.value

        integ = engine.integrate_run(run_id)
        assert integ["status"] == "shared_mode"

        with pytest.raises(EngineError, match="expected 'worktree'"):
            engine.apply_integration(run_id)

    def test_wait_step_and_cancel_run_and_doctor(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        store = RunStore(tmp_path / "runs")
        engine = SuperAgentEngine(
            store=store,
            adapter_factory=lambda host, env, ws: adapter,
            env={"PATH": "", "SUPER_AGENT_HOST": "codex"},
        )

        doc = engine.doctor(git_repo)
        assert doc["detected_host"] == "codex"
        assert "hosts" in doc

        plan = engine.create_plan(
            git_repo,
            [
                TaskSpec(id="a", title="A", goal="G1", write_paths=["a.txt"]),
                TaskSpec(
                    id="b",
                    title="B",
                    goal="G2",
                    depends_on=["a"],
                    write_paths=["b.txt"],
                ),
            ],
            host="codex",
            auto_add_verifier=False,
        )
        run_id = plan["run_id"]
        engine.approve_plan(run_id)
        engine.dispatch_ready(run_id)

        # Cannot integrate before workers succeed, and cannot retry a RUNNING task.
        with pytest.raises(EngineError, match="worker tasks not succeeded"):
            engine.integrate_run(run_id)
        with pytest.raises(EngineError, match="currently running"):
            engine.retry_task(run_id, "a")

        # While 'a' is still running with no state change, wait_step times out cleanly.
        wait_res = engine.wait_step(run_id, timeout_sec=0.05, poll_interval_sec=0.02)
        assert wait_res["timed_out_waiting"] is True
        assert wait_res["active_count"] == 1

        # Cancel run with unconfirmed termination retains slot & reports unconfirmed_attempts.
        adapter.windows["a-a1"].cancel_result = CancelResult(
            confirmed=False, detail="still shutting down"
        )
        cancelled = engine.cancel_run(run_id)
        assert cancelled["status"] == RunStatus.CANCELLED.value
        assert set(cancelled["cancelled_tasks"]) == {"a", "b"}
        assert len(cancelled["unconfirmed_attempts"]) == 1
        assert cancelled["unconfirmed_attempts"][0]["attempt_id"] == "a-a1"
        assert cancelled["occupied_slots"] == 1
        assert cancelled["reserved_write_paths"] == ["a.txt"]

        # Once adapter confirms exit, poll_run releases slot and wait_step returns immediately.
        adapter.windows["a-a1"].poll_result = WindowPollResult(
            status="cancelled", cleanup_confirmed=True
        )
        wait_done = engine.wait_step(run_id, timeout_sec=1.0, poll_interval_sec=0.02)
        assert wait_done["timed_out_waiting"] is False
        assert wait_done["occupied_slots"] == 0
        assert wait_done["all_terminal"] is True

    def test_auto_verifier_avoids_id_collision_with_worker_named_verify(
        self, tmp_path: Path, git_repo: Path
    ) -> None:
        adapter = FakeAdapter()
        engine, _ = _make_engine(tmp_path, adapter)

        plan = engine.create_plan(
            git_repo,
            [{"id": "verify", "title": "Worker named verify", "goal": "G", "write_paths": ["v.txt"]}],
            host="codex",
            auto_add_verifier=True,
        )
        ids = [t["id"] for t in plan["tasks"]]
        assert ids == ["verify", "verify-1"]
        assert plan["tasks"][1]["role"] == TaskRole.VERIFIER.value
        assert plan["tasks"][1]["depends_on"] == ["verify"]
