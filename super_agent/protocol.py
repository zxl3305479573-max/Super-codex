"""Prompt builders and structured ResultContract parser/validator for Super-Agent."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from super_agent.models import (
    AttemptRecord,
    ResultContract,
    RunState,
    TaskSpec,
    TestCheckResult,
)


ALLOWED_RESULT_STATUSES: frozenset[str] = frozenset({
    "succeeded",
    "failed",
    "blocked",
    "timed_out",
    "cancelled",
})

_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)

_ZERO_FAILURE_RE = re.compile(
    r"\b(?:0|no|zero)\s+(?:failed|failures?|errors?|issues?|errored)\b",
    re.IGNORECASE,
)
_FAILURE_KEYWORD_RE = re.compile(
    r"\b(?:\w*error(?:s|ed)?|fail(?:ed|ure|ures|ing|s)?|exception|traceback|"
    r"timed[\s_-]?out|timeout|aborted|crash(?:ed)?|broken|non-?zero)\b",
    re.IGNORECASE,
)
_NONZERO_EXIT_RE = re.compile(
    r"\b(?:exit(?:ed)?(?:\s+with)?(?:\s+(?:code|status))?|exit[_-]?code|"
    r"return[\s_-]?code|rc)\s*[:=]?\s*(-?[1-9]\d*)\b",
    re.IGNORECASE,
)
_NEGATIVE_PASS_RE = re.compile(
    r"\b(?:not\s+pass(?:ed)?|did\s+not\s+pass|unverified|inconclusive)\b",
    re.IGNORECASE,
)

_TIMING_OR_META_KEYS: frozenset[str] = frozenset({
    "duration",
    "duration_s",
    "duration_sec",
    "duration_ms",
    "elapsed",
    "elapsed_s",
    "elapsed_sec",
    "elapsed_ms",
    "runtime",
    "runtime_sec",
    "time",
    "timing",
    "timestamp",
    "started_at",
    "ended_at",
    "exit_code",
    "returncode",
    "status",
})


def _result_schema_block(task_id: str, result_path: str | None) -> str:
    example = {
        "task_id": task_id,
        "session_id": None,
        "status": "succeeded",
        "summary": "Concise summary of actual changes and verification results",
        "files_changed": [],
        "tests": [{"check": "command or inspection performed", "outcome": "passed (exit 0)"}],
        "issues": [],
        "artifacts": [],
        "next_action": None,
        "retryable": False,
    }
    target = result_path or "<attempt.result_path>"
    return "\n".join([
        "## Result Output Contract",
        f"You MUST write a single UTF-8 JSON object to the exact file path: `{target}`",
        "Also include the exact same JSON object in your final message as a fallback.",
        "",
        "Schema requirements:",
        f"- `task_id`: MUST be `{task_id}`.",
        "- `session_id`: string or `null`.",
        "- `status`: MUST be one of `\"succeeded\"`, `\"failed\"`, `\"blocked\"`, `\"timed_out\"`, or `\"cancelled\"`.",
        "  NEVER use `\"passed\"` or `\"completed\"` as `status`.",
        "- `summary`: non-empty string summarizing concrete work and evidence.",
        "- `files_changed`: JSON array of repository-relative file path strings (`[]` if none).",
        "- `tests`: JSON array of `{\"check\": \"...\", \"outcome\": \"...\"}` objects for checks actually run.",
        "- `issues`: JSON array of issue strings (`[]` if none).",
        "- `artifacts`: JSON array of artifact path or ID strings (`[]` if none; never an object).",
        "- `next_action`: string or `null`.",
        "- `retryable`: boolean (`true` only for clearly transient failures, else `false`).",
        "",
        "```json",
        json.dumps(example, ensure_ascii=False, indent=2),
        "```",
    ])


def _format_prereq_summaries(prereq_summaries: list[dict[str, Any]]) -> str:
    if not prereq_summaries:
        return "None (this task has no prerequisite evidence)."
    lines: list[str] = []
    for item in prereq_summaries:
        tid = item.get("task_id", "unknown")
        status = item.get("status", "unknown")
        summary = item.get("summary", "")
        lines.append(f"- **{tid}** (`{status}`): {summary}")
        files = item.get("files_changed")
        if files:
            lines.append(f"  - files_changed: {', '.join(str(f) for f in files)}")
        tests = item.get("tests")
        if tests:
            formatted_tests = []
            for entry in tests:
                if isinstance(entry, dict):
                    formatted_tests.append(f"{entry.get('check', '')} -> {entry.get('outcome', '')}")
                else:
                    formatted_tests.append(str(entry))
            lines.append(f"  - tests: {'; '.join(formatted_tests)}")
        artifacts = item.get("artifacts")
        if artifacts:
            lines.append(f"  - artifacts: {', '.join(str(a) for a in artifacts)}")
        issues = item.get("issues")
        if issues:
            lines.append(f"  - issues: {'; '.join(str(i) for i in issues)}")
    return "\n".join(lines)


def _format_acceptance_checks(checks: list[str]) -> str:
    if not checks:
        return "- No explicit acceptance checks listed; verify your own deliverables directly."
    return "\n".join(f"- {check}" for check in checks)


def build_worker_prompt(
    run: RunState,
    task: TaskSpec,
    attempt: AttemptRecord,
    prereq_summaries: list[dict[str, Any]],
) -> str:
    """Build the self-contained prompt for an independent worker window."""
    working_dir = attempt.worktree_path or run.workspace
    if task.write_paths:
        write_scope_section = "\n".join([
            "Allowed `write_paths` (repository-relative; you may ONLY create, modify, or delete within these paths):",
            *[f"- `{path}`" for path in task.write_paths],
            "If you discover that changes outside these paths are required, do NOT edit them; "
            "stop and report `status: \"blocked\"` with the required scope expansion in `issues`.",
        ])
    else:
        write_scope_section = (
            "READ-ONLY MODE (`write_paths` is empty `[]`): You MUST NOT create, modify, or delete "
            "any files inside the working directory. Only write your JSON result to the designated result file path."
        )

    sections = [
        f"# Super-Agent Worker Task: {task.id} — {task.title}",
        "",
        "## Assignment",
        f"- **Run ID**: `{run.run_id}`",
        f"- **Task ID**: `{task.id}`",
        f"- **Title**: {task.title}",
        f"- **Attempt**: `{attempt.attempt_id}` (attempt #{attempt.attempt_number})",
        f"- **Host**: `{attempt.host}`",
        f"- **Isolation Mode**: `{run.isolation_mode.value}`",
        f"- **Working Directory (absolute)**: `{working_dir}`",
        f"- **Timeout**: `{task.timeout_sec}s`",
        "",
        "## Goal",
        task.goal,
        "",
        "## Write Scope",
        write_scope_section,
        "",
        "## Prerequisite Evidence",
        _format_prereq_summaries(prereq_summaries),
        "",
        "## Acceptance Checks",
        _format_acceptance_checks(task.acceptance_checks),
        "",
        "## Strict Execution Rules",
        "- Work strictly inside the assigned working directory above.",
        "- Do NOT delegate to subagents or spawn nested worker sessions.",
        "- Stay strictly within allowed write paths (or remain strictly read-only if write paths are empty).",
        "- Run real commands to verify your work and record exact observed outcomes in `tests`.",
        "- Never fabricate test outcomes, file paths, or verification claims.",
        "- If blocked or unable to complete cleanly, report `failed` or `blocked` accurately with concrete `issues`.",
        "",
        _result_schema_block(task.id, attempt.result_path),
    ]
    return "\n".join(sections) + "\n"


def build_verifier_prompt(
    run: RunState,
    task: TaskSpec,
    attempt: AttemptRecord,
    integration_info: dict[str, Any],
    prereq_summaries: list[dict[str, Any]],
) -> str:
    """Build the strictly read-only independent verification prompt."""
    worktree_path = (
        integration_info.get("worktree_path")
        or attempt.worktree_path
        or run.workspace
    )
    patch_path = integration_info.get("patch_path")
    verify_command = integration_info.get("verify_command")
    source_patches = integration_info.get("source_patch_paths") or []

    integration_lines = [
        f"- **Inspection Working Directory (absolute)**: `{worktree_path}`",
        f"- **Isolation Mode**: `{run.isolation_mode.value}`",
    ]
    if patch_path:
        integration_lines.append(f"- **Consolidated Patch Path**: `{patch_path}`")
    if source_patches:
        integration_lines.append(
            "- **Source Task Patches**: " + ", ".join(f"`{p}`" for p in source_patches)
        )
    if verify_command:
        integration_lines.append(f"- **Integration Verification Command**: `{verify_command}`")

    verify_steps = [
        "1. Inspect the actual files and deliverables in the inspection working directory (do NOT rely solely on worker summaries).",
        "2. Run real verification commands to confirm tests, builds, and acceptance checks pass.",
    ]
    if verify_command:
        verify_steps.append(
            f"3. Run the integration verification command `{verify_command}` and record its exact outcome in `tests`."
        )
    verify_steps.append(
        "4. Report `status: \"succeeded\"` ONLY if every required inspection and command passes; "
        "otherwise report `status: \"failed\"` with concrete failing details in `issues` and `tests`."
    )

    sections = [
        f"# Super-Agent Independent Verifier Task: {task.id} — {task.title}",
        "",
        "## Role & Read-Only Boundary",
        "You are an **independent READ-ONLY verifier** (`write_paths` is empty `[]`).",
        "You MUST NOT modify, create, or delete any files in the workspace or integration worktree.",
        "The ONLY file you may write is the structured JSON result at the designated result path below.",
        "",
        "## Assignment",
        f"- **Run ID**: `{run.run_id}`",
        f"- **Task ID**: `{task.id}`",
        f"- **Title**: {task.title}",
        f"- **Attempt**: `{attempt.attempt_id}` (attempt #{attempt.attempt_number})",
        f"- **Host**: `{attempt.host}`",
        f"- **Timeout**: `{task.timeout_sec}s`",
        "",
        "## Verification Goal",
        task.goal,
        "",
        "## Integration Target",
        *integration_lines,
        "",
        "## Prerequisite Task Summaries & Checks",
        _format_prereq_summaries(prereq_summaries),
        "",
        "## Acceptance Checks to Verify",
        _format_acceptance_checks(task.acceptance_checks),
        "",
        "## Verification Instructions & Strict Rules",
        *verify_steps,
        "- Do NOT delegate to subagents or spawn nested sessions.",
        "- Keep `files_changed` empty (`[]`) because verification is strictly read-only.",
        "- Never fabricate inspection or test results; report exact commands and observed exit codes/outputs.",
        "",
        _result_schema_block(task.id, attempt.result_path),
    ]
    return "\n".join(sections) + "\n"


def _extract_json_object_from_text(text: str) -> tuple[dict[str, Any] | None, str | None]:
    stripped = text.strip()
    if not stripped:
        return None, "Content is empty"

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed, None
    if parsed is not None:
        return None, f"Expected a JSON object, got {type(parsed).__name__}"

    fenced_matches = list(_FENCED_BLOCK_RE.finditer(text))
    for match in reversed(fenced_matches):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, None

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    idx = 0
    length = len(text)
    while idx < length:
        brace_pos = text.find("{", idx)
        if brace_pos == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace_pos)
        except json.JSONDecodeError:
            idx = brace_pos + 1
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
        idx = max(brace_pos + 1, end)

    if candidates:
        for obj in reversed(candidates):
            if "status" in obj or "task_id" in obj:
                return obj, None
        return candidates[-1], None

    return None, "No valid JSON object found"


def _test_indicates_failure(test: TestCheckResult) -> bool:
    outcome = test.outcome.strip()
    if not outcome:
        return True
    cleaned = _ZERO_FAILURE_RE.sub("", outcome)
    if _NEGATIVE_PASS_RE.search(cleaned):
        return True
    if _FAILURE_KEYWORD_RE.search(cleaned):
        return True
    if _NONZERO_EXIT_RE.search(cleaned):
        return True
    return False


def _extract_artifact_refs(
    value: Any,
    refs: list[str],
    ignored_keys: list[str],
    *,
    key: str | None = None,
) -> None:
    if key is not None and key.strip().lower() in _TIMING_OR_META_KEYS:
        ignored_keys.append(key)
        return
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            refs.append(cleaned)
        return
    if isinstance(value, list):
        for item in value:
            _extract_artifact_refs(item, refs, ignored_keys)
        return
    if isinstance(value, dict):
        for child_key, child_val in value.items():
            _extract_artifact_refs(child_val, refs, ignored_keys, key=str(child_key))
        return
    if key is not None:
        ignored_keys.append(key)


def _normalize_string_list_field(
    raw: dict[str, Any],
    field_name: str,
    errors: list[str],
    notes: list[str],
) -> list[str]:
    if field_name not in raw or raw[field_name] is None:
        return []
    val = raw[field_name]
    if isinstance(val, str):
        if val.strip():
            notes.append(f"Normalized scalar '{field_name}' string to list")
            return [val]
        notes.append(f"Normalized empty scalar '{field_name}' string to empty list")
        return []
    if isinstance(val, list):
        result: list[str] = []
        for idx, item in enumerate(val):
            if not isinstance(item, str):
                errors.append(
                    f"Field '{field_name}[{idx}]' must be a string, got {type(item).__name__}"
                )
            else:
                result.append(item)
        return result
    errors.append(f"Field '{field_name}' must be a list of strings, got {type(val).__name__}")
    return []


def _normalize_artifacts_field(
    raw: dict[str, Any],
    errors: list[str],
    notes: list[str],
) -> list[str]:
    if "artifacts" not in raw or raw["artifacts"] is None:
        return []
    val = raw["artifacts"]
    if isinstance(val, list):
        result: list[str] = []
        for idx, item in enumerate(val):
            if not isinstance(item, str):
                errors.append(
                    f"Field 'artifacts[{idx}]' must be a string, got {type(item).__name__}"
                )
            else:
                result.append(item)
        return result
    if isinstance(val, str):
        if val.strip():
            notes.append("Normalized scalar 'artifacts' string to list")
            return [val.strip()]
        notes.append("Normalized empty scalar 'artifacts' string to empty list")
        return []
    if isinstance(val, dict):
        refs: list[str] = []
        ignored: list[str] = []
        _extract_artifact_refs(val, refs, ignored)
        deduped = list(dict.fromkeys(refs))
        if ignored:
            notes.append(
                f"Normalized 'artifacts' dict to list[str] (ignored non-reference metadata: {', '.join(ignored)})"
            )
        else:
            notes.append("Normalized 'artifacts' dict to list[str]")
        return deduped
    errors.append(f"Field 'artifacts' must be a list of strings, got {type(val).__name__}")
    return []


def parse_and_validate_result(
    result_path: Path,
    last_message_path: Path | None,
    *,
    task_id: str,
    session_id: str | None = None,
) -> tuple[ResultContract | None, list[str]]:
    """Load, validate, and safely normalize a worker/verifier ResultContract."""
    rpath = Path(result_path)
    lpath = Path(last_message_path) if last_message_path is not None else None
    notes: list[str] = []
    raw: dict[str, Any] | None = None

    if rpath.is_file():
        try:
            text = rpath.read_text(encoding="utf-8")
        except OSError as exc:
            return None, [f"Failed to read result file {rpath}: {exc}"]
        if text.strip():
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                return None, [f"Invalid JSON in result file {rpath}: {exc}"]
            if not isinstance(loaded, dict):
                return None, [
                    f"Result file {rpath} must contain a JSON object, got {type(loaded).__name__}"
                ]
            raw = loaded

    if raw is None and lpath is not None and lpath.is_file():
        try:
            msg_text = lpath.read_text(encoding="utf-8")
        except OSError as exc:
            return None, [f"Failed to read last_message_path {lpath}: {exc}"]
        extracted, extract_err = _extract_json_object_from_text(msg_text)
        if extracted is None:
            return None, [
                f"Result file {rpath} missing/empty and failed to extract JSON from {lpath}: {extract_err}"
            ]
        raw = extracted
        notes.append("Extracted result JSON from last_message_path because result_path was missing or empty")

    if raw is None:
        return None, [f"Result file not found and no usable last_message_path fallback: {rpath}"]

    errors: list[str] = []

    # Preserve any pre-existing normalized_notes if valid
    existing_notes = raw.get("normalized_notes")
    if existing_notes is not None:
        if isinstance(existing_notes, list) and all(isinstance(n, str) for n in existing_notes):
            notes = list(existing_notes) + notes
        else:
            errors.append("Field 'normalized_notes' must be a list of strings")

    # Enforce task_id
    raw_task_id = raw.get("task_id")
    if raw_task_id is None or raw_task_id == "":
        resolved_task_id = task_id
        notes.append(f"Filled missing 'task_id' with expected task_id '{task_id}'")
    elif not isinstance(raw_task_id, str):
        errors.append(f"Field 'task_id' must be a string, got {type(raw_task_id).__name__}")
        resolved_task_id = task_id
    elif raw_task_id != task_id:
        errors.append(f"Result task_id {raw_task_id!r} does not match expected task_id {task_id!r}")
        resolved_task_id = raw_task_id
    else:
        resolved_task_id = raw_task_id

    # Attach session_id if missing
    raw_session_id = raw.get("session_id")
    if raw_session_id is not None and not isinstance(raw_session_id, str):
        errors.append(
            f"Field 'session_id' must be a string or null, got {type(raw_session_id).__name__}"
        )
        resolved_session_id = session_id
    elif isinstance(raw_session_id, str) and raw_session_id.strip():
        resolved_session_id = raw_session_id
    elif session_id is not None:
        resolved_session_id = session_id
    else:
        raw_agent_id = raw.get("agent_id")
        if isinstance(raw_agent_id, str) and raw_agent_id.strip():
            resolved_session_id = raw_agent_id
            notes.append("Normalized 'agent_id' field to 'session_id'")
        else:
            resolved_session_id = None

    # Validate summary (required non-empty string)
    raw_summary = raw.get("summary")
    if raw_summary is None:
        errors.append("Missing required field 'summary'")
        resolved_summary = ""
    elif not isinstance(raw_summary, str):
        errors.append(f"Field 'summary' must be a string, got {type(raw_summary).__name__}")
        resolved_summary = ""
    elif not raw_summary.strip():
        errors.append("Field 'summary' must be a non-empty string")
        resolved_summary = ""
    else:
        resolved_summary = raw_summary

    # Validate tests before status normalization so passed/completed can inspect test evidence
    tests_valid = True
    resolved_tests: list[TestCheckResult] = []
    if "tests" in raw and raw["tests"] is not None:
        raw_tests = raw["tests"]
        if not isinstance(raw_tests, list):
            errors.append(f"Field 'tests' must be a list, got {type(raw_tests).__name__}")
            tests_valid = False
        else:
            for idx, entry in enumerate(raw_tests):
                if not isinstance(entry, dict):
                    errors.append(
                        f"Field 'tests[{idx}]' must be an object with 'check' and 'outcome'"
                    )
                    tests_valid = False
                    continue
                check_val = entry.get("check")
                outcome_val = entry.get("outcome")
                if not isinstance(check_val, str) or not check_val.strip():
                    errors.append(f"Field 'tests[{idx}].check' must be a non-empty string")
                    tests_valid = False
                    continue
                if not isinstance(outcome_val, str) or not outcome_val.strip():
                    errors.append(f"Field 'tests[{idx}].outcome' must be a non-empty string")
                    tests_valid = False
                    continue
                resolved_tests.append(TestCheckResult(check=check_val, outcome=outcome_val))

    # Validate & normalize status
    raw_status = raw.get("status")
    resolved_status = ""
    if raw_status is None:
        errors.append("Missing required field 'status'")
    elif not isinstance(raw_status, str):
        errors.append(f"Field 'status' must be a string, got {type(raw_status).__name__}")
    else:
        lowered = raw_status.strip().lower()
        if raw_status in ALLOWED_RESULT_STATUSES:
            resolved_status = raw_status
        elif lowered in ALLOWED_RESULT_STATUSES:
            resolved_status = lowered
            notes.append(f"Normalized status {raw_status!r} to {lowered!r}")
        elif lowered in ("passed", "completed"):
            if (
                tests_valid
                and len(resolved_tests) >= 1
                and not any(_test_indicates_failure(test) for test in resolved_tests)
            ):
                resolved_status = "succeeded"
                notes.append(
                    f"Normalized status {raw_status!r} to 'succeeded' based on passing test evidence"
                )
            else:
                errors.append(
                    f"Invalid/unverified status {raw_status!r}: cannot normalize to 'succeeded' "
                    "without at least one passing test entry and no failing outcomes"
                )
        else:
            errors.append(
                f"Invalid status {raw_status!r}; allowed values: {sorted(ALLOWED_RESULT_STATUSES)}"
            )

    # Validate & normalize list fields
    resolved_files_changed = _normalize_string_list_field(raw, "files_changed", errors, notes)
    resolved_issues = _normalize_string_list_field(raw, "issues", errors, notes)
    resolved_artifacts = _normalize_artifacts_field(raw, errors, notes)

    # Validate next_action
    raw_next_action = raw.get("next_action")
    if raw_next_action is not None and not isinstance(raw_next_action, str):
        errors.append(
            f"Field 'next_action' must be a string or null, got {type(raw_next_action).__name__}"
        )
        resolved_next_action = None
    else:
        resolved_next_action = raw_next_action

    # Validate retryable
    raw_retryable = raw.get("retryable", False)
    if raw_retryable is None:
        resolved_retryable = False
    elif not isinstance(raw_retryable, bool):
        errors.append(f"Field 'retryable' must be a boolean, got {type(raw_retryable).__name__}")
        resolved_retryable = False
    else:
        resolved_retryable = raw_retryable

    if errors:
        return None, errors

    return (
        ResultContract(
            task_id=resolved_task_id,
            session_id=resolved_session_id,
            status=resolved_status,
            summary=resolved_summary,
            files_changed=resolved_files_changed,
            tests=resolved_tests,
            issues=resolved_issues,
            artifacts=resolved_artifacts,
            next_action=resolved_next_action,
            retryable=resolved_retryable,
            normalized_notes=notes,
        ),
        [],
    )
