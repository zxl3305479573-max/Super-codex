---
name: super-agent
description: When explicitly invoked as $super-agent, orchestrate complex tasks inside the current host by planning a module DAG, waiting for user approval, launching real independent conversation windows with isolated worktrees, running independent read-only verification, and reporting structured results.
---

# Super-Agent Single-Host Multi-Window Orchestrator

## Core Rules

1. **Main conversation role**: The main conversation ONLY inspects context (read-only), plans the module DAG, waits for explicit user approval, dispatches ready tasks, polls progress, triggers integration and independent verification, and summarizes results back to the user.
2. **No direct edits**: The main conversation MUST NEVER directly edit target deliverable files. All edits, builds, and tests happen inside worker windows.
3. **Real independent windows only**: NEVER substitute in-process subagent tools (`spawn_agent`, `invoke_subagent`, or similar) for real independent conversation windows launched through `super-agent`.
4. **Strict single-host execution**: All module worker windows and the independent verifier window MUST run inside the SAME host agent bound to `SUPER_AGENT_HOST`. Never mix hosts within a run.

## Phase 1: Plan & Confirm

1. Call `super_agent_doctor` to verify host readiness, adapter capabilities, Git worktree isolation support, and whether `shell_fallback_recommended` is set. If the host adapter is unavailable, report the blocker and stop.
2. Inspect the workspace read-only to identify relevant modules, file ownership boundaries, and verification commands.
3. Design an acyclic task DAG:
   - Assign each module a unique `id`, clear `title` and `goal`, explicit `depends_on` edges, exclusive `write_paths` (non-overlapping for parallel tasks), and concrete `acceptance_checks`.
   - Include a downstream read-only verification task (`role: "verifier"`, `write_paths: []`) that depends on all worker tasks.
4. Call `super_agent_plan_create` to persist the draft run.
5. Present the module plan table to the user (task ID, role, dependencies, exclusive write scope, acceptance checks, isolation mode, concurrency limit) and **WAIT for explicit user approval** before dispatching any window.

## Phase 2: Approve & Dispatch Loop

1. After the user explicitly approves the plan, call `super_agent_plan_approve`.
2. Loop until all worker tasks reach terminal status:
   - Call `super_agent_dispatch_ready` to launch ready module tasks in fresh independent conversation windows within the concurrency limit.
   - Call `super_agent_wait_step` to poll active windows in bounded intervals until state changes or active windows finish.
3. **Antigravity shell fallback**: If `super_agent_doctor` reports `shell_fallback_recommended: true`, invoke the CLI from the workspace shell instead of MCP dispatch/wait so the child process inherits `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN` directly from process environment without passing secrets through chat text or command arguments:
   - `python -m super_agent dispatch --run-id <id> --json`
   - `python -m super_agent wait --run-id <id> --json`

## Phase 3: Integrate & Independent Verification

1. Once all worker tasks finish, ensure integration in `worktree` mode by calling `super_agent_integrate_run` to consolidate worker patches in DAG order and produce `final.patch`.
2. Dispatch the read-only verifier window via `super_agent_dispatch_ready` and collect its terminal evidence via `super_agent_wait_step` (or the shell fallback commands above when `shell_fallback_recommended: true`).
3. Claim verified success only when supported by the independent verifier's evidence.

## Phase 4: Summarize & Optional Apply

1. Call `super_agent_summarize_run`.
2. Report to the user:
   - Every module's final status, summary, and test results.
   - Each independent window's session ID and resume link or command (`resume_hint`).
   - Independent verification evidence and any remaining issues or blocked tasks.
   - The `final.patch` path and consolidated change summary (when using `worktree` isolation).
3. Ask the user for explicit confirmation before calling `super_agent_apply_integration` to apply `final.patch` to the primary workspace.

## Failure, Retry & Cancel

- **Retry**: When a task fails, times out, or fails acceptance checks, use `super_agent_retry_task` to recover. It ONLY retries the failed task in a fresh independent window and unblocks its blocked dependents; it NEVER reruns tasks that already succeeded.
- **Cancel**: To abort an active run, call `super_agent_cancel_run` to request termination of active windows and report confirmation status and any outstanding session IDs.
