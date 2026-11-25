"""Unit tests for super_agent.store and super_agent.protocol."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from super_agent.models import (
    AttemptRecord,
    IsolationMode,
    ResultContract,
    RunState,
    RunStatus,
    SessionHandle,
    TaskRole,
    TaskRuntimeState,
    TaskSpec,
    TaskStatus,
    TestCheckResult as CheckResult,
)
from super_agent.protocol import (
    ALLOWED_RESULT_STATUSES,
    build_verifier_prompt,
    build_worker_prompt,
    parse_and_validate_result,
)
from super_agent.store import (
    AttemptPaths,
    RunStore,
    StoreError,
    default_runs_root,
)


def _sample_run(run_id: str = "run-001", workspace: str = "D:/repo") -> RunState:
    tasks = [
        TaskSpec(
            id="t1",
            title="Implement parser",
            goal="Write parser module",
            write_paths=["super_agent/parser.py"],
            acceptance_checks=["pytest tests/test_parser.py"],
        ),
        TaskSpec(
            id="v1",
            title="Verify run",
            goal="Verify consolidated patch and tests",
            depends_on=["t1"],
            write_paths=[],
            acceptance_checks=["pytest -v"],
            role=TaskRole.VERIFIER,
        ),
    ]
    return RunState(
        run_id=run_id,
        host="codex",
        workspace=workspace,
        run_dir="",
        isolation_mode=IsolationMode.WORKTREE,
        base_commit="deadbeef",
        max_concurrency=3,
        status=RunStatus.DRAFT,
        tasks=tasks,
        task_states={t.id: TaskRuntimeState(task_id=t.id) for t in tasks},
    )


class TestDefaultRunsRoot:
    def test_respects_super_agent_home_override(self, tmp_path: Path) -> None:
        custom = str(tmp_path / "custom-home")
        assert default_runs_root({"SUPER_AGENT_HOME": custom}) == Path(custom) / "runs"

    def test_falls_back_to_user_home_when_unset_or_empty(self) -> None:
        expected = Path.home() / ".super-agent" / "runs"
        assert default_runs_root({}) == expected
        assert default_runs_root({"SUPER_AGENT_HOME": ""}) == expected
        assert default_runs_root({"SUPER_AGENT_HOME": "   "}) == expected


class TestRunStore:
    def test_run_dir_and_attempt_id_validation(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path / "runs")
        assert store.run_dir("run-123") == tmp_path / "runs" / "run-123"

        bad_names = ["", ".", "..", "../escape", "a/b", r"a\b", "C:run", "bad\x00id", " lead", "trail "]
        if os.name == "nt":
            bad_names.append("trailing.")

        for bad in bad_names:
            with pytest.raises(StoreError):
                store.run_dir(bad)
            with pytest.raises(StoreError):
                store.attempt_paths("run-123", bad)

    def test_attempt_and_integration_paths_layout(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path / "runs")
        ap = store.attempt_paths("run-1", "t1-1")
        assert isinstance(ap, AttemptPaths)
        assert ap.attempt_dir.is_dir()
        assert ap.prompt_path.parent == ap.attempt_dir
        assert ap.result_path.parent == ap.attempt_dir
        assert ap.last_message_path.parent == ap.attempt_dir
        assert ap.log_path.parent == ap.attempt_dir
        assert ap.exit_code_path.parent == ap.attempt_dir
        assert ap.patch_path.parent == ap.attempt_dir
        assert ap.worktrees_root == store.run_dir("run-1") / "worktrees"
        # Ensure patch_path is outside worktrees_root
        assert not ap.patch_path.is_relative_to(ap.worktrees_root)

        wt_root, final_patch = store.integration_paths("run-1")
        assert wt_root == store.run_dir("run-1") / "worktrees"
        assert final_patch == store.run_dir("run-1") / "integration" / "final.patch"
        assert final_patch.parent.is_dir()
        assert not final_patch.is_relative_to(wt_root)

    def test_create_save_load_and_atomic_replace(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path / "runs")
        state = _sample_run("run-alpha")
        created = store.create_run(state)

        rdir = store.run_dir("run-alpha")
        assert created.run_dir == str(rdir)
        assert (rdir / "state.json").is_file()
        assert (rdir / "attempts").is_dir()
        assert (rdir / "worktrees").is_dir()
        assert (rdir / "integration").is_dir()

        # Duplicate create_run rejected
        with pytest.raises(StoreError, match="already exists"):
            store.create_run(_sample_run("run-alpha"))

        # Verify run_created event logged
        events = store.read_events("run-alpha")
        assert len(events) == 1
        assert events[0]["event_type"] == "run_created"
        assert events[0]["run_id"] == "run-alpha"
        assert events[0]["payload"]["task_count"] == 2

        # Mutate and save_run atomically
        prev_updated = created.updated_at
        time.sleep(0.01)
        created.status = RunStatus.RUNNING
        created.attempts["t1-1"] = AttemptRecord(
            attempt_id="t1-1",
            attempt_number=1,
            task_id="t1",
            host="codex",
            session=SessionHandle(host="codex", session_id="sess-1", title="t1"),
            result=ResultContract(
                task_id="t1",
                session_id="sess-1",
                status="succeeded",
                summary="Implemented parser",
                tests=[CheckResult(check="pytest", outcome="passed")],
            ),
        )
        store.save_run(created)
        assert created.updated_at > prev_updated

        # No leftover temporary files
        leftovers = [p for p in rdir.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

        loaded = store.load_run("run-alpha")
        assert loaded.status == RunStatus.RUNNING
        assert loaded.run_id == "run-alpha"
        assert loaded.attempts["t1-1"].session is not None
        assert loaded.attempts["t1-1"].session.session_id == "sess-1"
        assert loaded.attempts["t1-1"].result is not None
        assert loaded.attempts["t1-1"].result.summary == "Implemented parser"

    def test_load_run_errors_on_missing_or_corrupt(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path / "runs")
        with pytest.raises(StoreError, match="not found"):
            store.load_run("missing-run")

        rdir = store.run_dir("corrupt-json")
        rdir.mkdir(parents=True)
        (rdir / "state.json").write_text("{not valid json", encoding="utf-8")
        with pytest.raises(StoreError, match="Corrupt"):
            store.load_run("corrupt-json")

        rdir2 = store.run_dir("bad-shape")
        rdir2.mkdir(parents=True)
        (rdir2 / "state.json").write_text("[]", encoding="utf-8")
        with pytest.raises(StoreError, match="JSON object"):
            store.load_run("bad-shape")

    def test_list_runs_sorted_descending_and_resilient(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path / "runs")
        assert store.list_runs() == []

        r1 = store.create_run(_sample_run("run-1"))
        time.sleep(0.01)
        r2 = store.create_run(_sample_run("run-2"))
        time.sleep(0.01)
        r1.status = RunStatus.COMPLETED
        store.save_run(r1)  # r1 is now newest

        # Create a junk folder and a corrupt run folder; list_runs should skip them
        (store.base_dir / "random-dir").mkdir()
        corrupt = store.base_dir / "corrupt-run"
        corrupt.mkdir()
        (corrupt / "state.json").write_text("{bad", encoding="utf-8")

        listing = store.list_runs()
        assert [item["run_id"] for item in listing] == ["run-1", "run-2"]
        assert listing[0]["status"] == "completed"
        assert listing[1]["status"] == "draft"
        assert listing[0]["updated_at"] >= listing[1]["updated_at"]

    def test_events_append_read_and_corrupt_handling(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path / "runs")
        assert store.read_events("no-events-yet") == []

        ev1 = store.append_event("run-ev", "task_started", {"task_id": "t1"})
        ev2 = store.append_event("run-ev", "task_finished", {"task_id": "t1", "status": "succeeded"})
        read_back = store.read_events("run-ev")
        assert read_back == [ev1, ev2]

        # Corrupt line raises StoreError
        events_path = store.run_dir("run-ev") / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write("{corrupt\n")
        with pytest.raises(StoreError, match="Corrupt event JSON"):
            store.read_events("run-ev")


class TestPromptBuilders:
    def test_build_worker_prompt_writer_and_read_only(self) -> None:
        run = _sample_run("run-p1", workspace=r"D:\repo")
        task = run.tasks[0]
        attempt = AttemptRecord(
            attempt_id="t1-1",
            attempt_number=1,
            task_id="t1",
            host="codex",
            worktree_path=r"D:\runs\run-p1\worktrees\t1-1",
            result_path=r"D:\runs\run-p1\attempts\t1-1\result.json",
        )
        prereqs = [
            {
                "task_id": "t0",
                "status": "succeeded",
                "summary": "Prepared base schema",
                "files_changed": ["super_agent/models.py"],
                "tests": [{"check": "pytest", "outcome": "passed"}],
            }
        ]
        prompt = build_worker_prompt(run, task, attempt, prereqs)
        assert "t1" in prompt
        assert "Implement parser" in prompt
        assert "Write parser module" in prompt
        assert r"D:\runs\run-p1\worktrees\t1-1" in prompt
        assert "super_agent/parser.py" in prompt
        assert "Prepared base schema" in prompt
        assert "pytest tests/test_parser.py" in prompt
        assert "Do NOT delegate to subagents" in prompt
        assert r"D:\runs\run-p1\attempts\t1-1\result.json" in prompt

        # Read-only worker falls back to run.workspace when worktree_path is None
        ro_task = TaskSpec(id="ro1", title="Audit", goal="Read code only", write_paths=[])
        ro_attempt = AttemptRecord(
            attempt_id="ro1-1",
            attempt_number=1,
            task_id="ro1",
            host="claude",
            worktree_path=None,
            result_path=r"D:\runs\run-p1\attempts\ro1-1\result.json",
        )
        ro_prompt = build_worker_prompt(run, ro_task, ro_attempt, [])
        assert r"D:\repo" in ro_prompt
        assert "READ-ONLY" in ro_prompt

    def test_build_verifier_prompt_includes_integration_info(self) -> None:
        run = _sample_run("run-v1", workspace=r"D:\repo")
        verifier = run.tasks[1]
        attempt = AttemptRecord(
            attempt_id="v1-1",
            attempt_number=1,
            task_id="v1",
            host="codex",
            result_path=r"D:\runs\run-v1\attempts\v1-1\result.json",
        )
        integration_info = {
            "worktree_path": r"D:\runs\run-v1\worktrees\integration",
            "patch_path": r"D:\runs\run-v1\integration\final.patch",
            "verify_command": "python -m super_agent.worktree verify --workspace ...",
            "source_patch_paths": [r"D:\runs\run-v1\attempts\t1-1\changes.patch"],
        }
        prereqs = [{"task_id": "t1", "status": "succeeded", "summary": "Parser implemented"}]
        prompt = build_verifier_prompt(run, verifier, attempt, integration_info, prereqs)

        assert "READ-ONLY" in prompt
        assert r"D:\runs\run-v1\worktrees\integration" in prompt
        assert r"D:\runs\run-v1\integration\final.patch" in prompt
        assert "python -m super_agent.worktree verify" in prompt
        assert "Parser implemented" in prompt
        assert "pytest -v" in prompt
        assert "Inspect the actual files" in prompt
        assert "Do NOT delegate to subagents" in prompt
        assert r"D:\runs\run-v1\attempts\v1-1\result.json" in prompt


class TestParseAndValidateResult:
    def test_allowed_result_statuses_constant(self) -> None:
        assert ALLOWED_RESULT_STATUSES == frozenset(
            {"succeeded", "failed", "blocked", "timed_out", "cancelled"}
        )

    def test_valid_result_file_attaches_missing_session_id(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"
        payload = {
            "task_id": "t1",
            "session_id": None,
            "status": "succeeded",
            "summary": "Implemented module cleanly",
            "files_changed": ["super_agent/dag.py"],
            "tests": [{"check": "pytest tests/test_dag.py", "outcome": "12 passed"}],
            "issues": [],
            "artifacts": ["changes.patch"],
            "next_action": None,
            "retryable": False,
        }
        result_path.write_text(json.dumps(payload), encoding="utf-8")

        contract, errors = parse_and_validate_result(
            result_path, None, task_id="t1", session_id="sess-42"
        )
        assert errors == []
        assert contract is not None
        assert contract.task_id == "t1"
        assert contract.session_id == "sess-42"
        assert contract.status == "succeeded"
        assert contract.files_changed == ["super_agent/dag.py"]
        assert len(contract.tests) == 1
        assert contract.tests[0].check == "pytest tests/test_dag.py"
        assert contract.normalized_notes == []

        # Preserves explicit session_id already in JSON
        payload["session_id"] = "sess-original"
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        contract2, errors2 = parse_and_validate_result(
            result_path, None, task_id="t1", session_id="sess-42"
        )
        assert errors2 == []
        assert contract2 is not None
        assert contract2.session_id == "sess-original"

    def test_fallback_to_last_message_raw_and_fenced_json(self, tmp_path: Path) -> None:
        missing_result = tmp_path / "missing_result.json"
        last_msg = tmp_path / "last_message.txt"

        # Fenced ```json block inside markdown prose
        data = {
            "task_id": "t2",
            "status": "succeeded",
            "summary": "All checks passed",
            "files_changed": ["a.py"],
            "tests": [{"check": "pytest", "outcome": "exit 0"}],
            "issues": [],
            "artifacts": [],
            "next_action": None,
        }
        last_msg.write_text(
            "Done! Here is the structured result:\n\n```json\n"
            + json.dumps(data, indent=2)
            + "\n```\nThanks!\n",
            encoding="utf-8",
        )
        contract, errors = parse_and_validate_result(
            missing_result, last_msg, task_id="t2", session_id="sess-2"
        )
        assert errors == []
        assert contract is not None
        assert contract.status == "succeeded"
        assert contract.session_id == "sess-2"
        assert any("last_message_path" in note for note in contract.normalized_notes)

        # Raw JSON in last_message_path
        last_msg.write_text(json.dumps(data), encoding="utf-8")
        contract_raw, errors_raw = parse_and_validate_result(
            missing_result, last_msg, task_id="t2"
        )
        assert errors_raw == []
        assert contract_raw is not None

        # Empty result_path + inline JSON embedded in prose
        empty_result = tmp_path / "empty_result.json"
        empty_result.write_text("   \n", encoding="utf-8")
        last_msg.write_text(
            "Ran pytest and finished. Final payload: " + json.dumps(data) + " — all done.",
            encoding="utf-8",
        )
        contract_inline, errors_inline = parse_and_validate_result(
            empty_result, last_msg, task_id="t2"
        )
        assert errors_inline == []
        assert contract_inline is not None
        assert contract_inline.task_id == "t2"

    @pytest.mark.parametrize("raw_status", ["passed", "completed"])
    def test_status_passed_and_completed_normalization_rules(
        self, tmp_path: Path, raw_status: str
    ) -> None:
        result_path = tmp_path / "result.json"

        # Succeeds when tests has >= 1 passing entry (including "0 failed")
        good = {
            "task_id": "t1",
            "status": raw_status,
            "summary": "Tests passed",
            "tests": [{"check": "pytest", "outcome": "8 passed, 0 failed (exit 0)"}],
        }
        result_path.write_text(json.dumps(good), encoding="utf-8")
        contract, errors = parse_and_validate_result(result_path, None, task_id="t1")
        assert errors == []
        assert contract is not None
        assert contract.status == "succeeded"
        assert any(raw_status in note and "succeeded" in note for note in contract.normalized_notes)

        # Fails when tests is empty
        no_tests = {"task_id": "t1", "status": raw_status, "summary": "Done", "tests": []}
        result_path.write_text(json.dumps(no_tests), encoding="utf-8")
        contract_none, errs_empty = parse_and_validate_result(result_path, None, task_id="t1")
        assert contract_none is None
        assert errs_empty

        # Fails when any test outcome indicates failure
        for bad_outcome in [
            "1 failed, 3 passed",
            "fail",
            "error running suite",
            "AssertionError in test_api",
            "exit 1",
            "exit code 2",
            "timed out",
            "not passed",
        ]:
            bad = {
                "task_id": "t1",
                "status": raw_status,
                "summary": "Claimed done",
                "tests": [
                    {"check": "lint", "outcome": "passed"},
                    {"check": "pytest", "outcome": bad_outcome},
                ],
            }
            result_path.write_text(json.dumps(bad), encoding="utf-8")
            contract_bad, errs_bad = parse_and_validate_result(result_path, None, task_id="t1")
            assert contract_bad is None, f"Should have rejected outcome {bad_outcome!r}"
            assert errs_bad

    def test_normalizes_artifacts_dict_and_scalar_fields(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"
        payload = {
            "status": "succeeded",
            "summary": "Built deliverables",
            "files_changed": "super_agent/store.py",
            "issues": "minor deprecation warning",
            "artifacts": {
                "patch": "attempts/t1-1/changes.patch",
                "reports": ["docs/report.md", "art-123"],
                "duration_sec": 4.2,
                "elapsed": "4.2s",
            },
            "tests": [{"check": "pytest", "outcome": "passed"}],
        }
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        contract, errors = parse_and_validate_result(
            result_path, None, task_id="t1", session_id="s1"
        )
        assert errors == []
        assert contract is not None
        assert contract.task_id == "t1"
        assert contract.files_changed == ["super_agent/store.py"]
        assert contract.issues == ["minor deprecation warning"]
        assert contract.artifacts == ["attempts/t1-1/changes.patch", "docs/report.md", "art-123"]
        assert len(contract.normalized_notes) >= 4

        # Scalar string artifacts
        payload2 = {
            "task_id": "t1",
            "status": "succeeded",
            "summary": "Single artifact",
            "artifacts": "dist/output.whl",
        }
        result_path.write_text(json.dumps(payload2), encoding="utf-8")
        contract2, errors2 = parse_and_validate_result(result_path, None, task_id="t1")
        assert errors2 == []
        assert contract2 is not None
        assert contract2.artifacts == ["dist/output.whl"]
        assert any("artifacts" in note for note in contract2.normalized_notes)

    def test_rejects_mismatched_task_id_and_invalid_types(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"

        # Mismatched task_id
        result_path.write_text(
            json.dumps({"task_id": "other-task", "status": "succeeded", "summary": "ok"}),
            encoding="utf-8",
        )
        contract, errors = parse_and_validate_result(result_path, None, task_id="t1")
        assert contract is None
        assert any("task_id" in err for err in errors)

        # Missing required fields & bad types
        result_path.write_text(
            json.dumps(
                {
                    "task_id": "t1",
                    "status": "bogus_status",
                    "summary": "",
                    "files_changed": 123,
                    "tests": ["not-a-dict"],
                    "artifacts": 999,
                    "next_action": ["nope"],
                    "retryable": "yes",
                }
            ),
            encoding="utf-8",
        )
        contract2, errors2 = parse_and_validate_result(result_path, None, task_id="t1")
        assert contract2 is None
        assert len(errors2) >= 7

    def test_missing_or_unparseable_files_return_errors(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.json"
        contract, errors = parse_and_validate_result(missing, None, task_id="t1")
        assert contract is None
        assert errors

        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{broken json", encoding="utf-8")
        contract2, errors2 = parse_and_validate_result(corrupt, None, task_id="t1")
        assert contract2 is None
        assert any("Invalid JSON" in err for err in errors2)

        bad_msg = tmp_path / "last_msg.txt"
        bad_msg.write_text("Sorry, I forgot to produce JSON.", encoding="utf-8")
        contract3, errors3 = parse_and_validate_result(missing, bad_msg, task_id="t1")
        assert contract3 is None
        assert errors3
