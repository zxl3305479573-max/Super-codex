---
name: orchestrator
description: When explicitly invoked as $orchestrator, coordinate native Codex Workers with task dependencies, exclusive write scopes, and independent verification.
---

# Codex Orchestrator

The main session plans, dispatches, tracks, and reports. Workers make changes, build, run tests, and inspect deliverables. The main session may read task context for planning and review Worker reports. A Worker handles one assigned task and delegates nothing. This skill runs standalone with native Worker tools.

## Plan and dispatch

1. Check the tools actually available in this session. Use `spawn_agent(message, fork_context:false)` for independent context, record the returned UUID, collect terminal evidence with the exposed `wait` or `wait_agent` tool using its actual schema, and release terminal slots with `close_agent(target)`. On other hosts use the exposed equivalents with the same capabilities. If independent dispatch, result collection, or required cleanup is unavailable, or the user allows zero Workers, report the blocker, outline the tasks and checks, and identify a compatible native-Worker session or a separate manual handoff. Keep execution in Worker sessions.
2. Keep a small dependency graph in the main conversation. Give each node a task ID, prerequisites, workspace, permitted write paths (including any build or test output), acceptance checks, and a state. Track attempts with their host IDs and outcomes. Resolve missing dependencies or cycles before dispatch. Use three concurrent Workers by default and honor a lower limit for this invocation. Count verification and unresolved Workers against the same limit.
3. Fill free slots with ready nodes before waiting. A node is ready when every prerequisite succeeds and its write scope does not overlap a reserved scope. Compare paths using workspace filesystem semantics, including directory containment. Add dependencies when a task needs outputs that another task is still changing. Each fresh Worker gets the task ID, absolute workspace path, relevant context and prerequisite evidence, exact write scope (empty for read-only work), expected deliverables, checks, deadline if set, and the result format below. Tell it to report any necessary scope expansion and to delegate nothing.
4. Wait on active host IDs in bounded intervals, respecting any deadline. A wait timeout leaves the Worker active and its scope reserved. If dispatch is ambiguous, reconcile it with the host before retrying or reusing the reservation. Record each terminal result against the host ID and assigned task, then close the terminal Worker where required and refill free slots. Release a slot only when the host confirms it is free.

## Handle failures and verify

- Treat a terminal failure or a failed acceptance check as unsuccessful. Handle missing or unusable evidence through the result protocol below. Carry the failure reason forward and mark its dependents `blocked` when no retry remains. Continue independent work. Retry at most once, with a fresh Worker, only for a clearly transient failure after the previous attempt has ended and its slot is free. Confirm the first attempt left no conflicting effects or that repeating the action is safe. Preserve both attempt outcomes. Use fresh spawns for retries.
- On cancellation, stop dispatching affected tasks. On a deadline, stop dispatching affected tasks and record `timed_out` when the deadline actually expires. Use native cancellation when the host supports it and collect terminal evidence before releasing the scope or allowing dependent work. If cancellation or cleanup remains unconfirmed, report the outstanding host ID and last known state, keep its scope reserved, and finish work that can still safely proceed.
- Include verification nodes downstream of the work they check. Give a separate Worker the acceptance criteria, artifact paths, and relevant test evidence. Have it inspect the actual deliverables and check or rerun relevant tests within its scope. Use an empty write scope when the checks can run read-only. Gate any task that requires verified inputs on the verification node. Claim verified success only for outcomes supported by that Worker’s evidence and report failures or checks left pending.

## Result protocol

Each Worker returns one compact JSON object with all fields shown below. Use `succeeded`, `failed`, `blocked`, `timed_out`, or `cancelled` for task `status`. Record `passed` or `failed` check outcomes in `tests`. Never use `passed` as task `status`. Always return `files_changed`, `tests`, `issues`, and `artifacts` as arrays, using `[]` for empty lists. Put only artifact paths or IDs in `artifacts`. Put test and timing details in `tests` or `summary`. Use `null` when no next action is needed. Keep evidence short.

```json
{"task_id":"task-id","agent_id":null,"status":"succeeded","summary":"Short outcome","files_changed":[],"tests":[{"check":"Command or inspection","outcome":"Exit code or observed result"}],"issues":[],"artifacts":[],"next_action":null}
```

The main session collects structured Worker results by task, matches each result to its dispatch, attaches the host-returned `agent_id`, and uses `null` when no host ID was returned. Check required fields, their types, and allowed status values. Normalize clearly recoverable evidence in place, such as moving timing fields from a malformed `artifacts` object into `summary` and keeping any identifiable references in the `artifacts` array. Repair `status: "passed"` to `succeeded` only when the host result and check evidence together establish task success. Send missing or unreliable evidence to read-only verification before accepting the result. Preserve successful writes during evidence recovery and verification. Present a table or compact summary that maps each task to its outcome and test or verification evidence. Note any normalization and the reasons for blocked or unresolved work.
