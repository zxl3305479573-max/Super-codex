"""Zero-dependency MCP JSON-RPC 2.0 Stdio server for Super-Agent."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import io
import json
from pathlib import Path
import sys
from typing import Any

from super_agent import __version__

JSONRPC_VERSION = "2.0"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class _JsonRpcError(Exception):
    """Internal exception mapped directly to a JSON-RPC 2.0 error response."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class _ToolInputError(ValueError):
    """Raised when arguments supplied to a known MCP tool fail validation."""


def _make_error_response(
    req_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "error": err,
    }


def _make_result_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "result": result,
    }


def _check_allowed_keys(arguments: dict[str, Any], allowed: set[str], tool_name: str) -> None:
    extra = sorted(set(arguments.keys()) - allowed)
    if extra:
        raise _ToolInputError(
            f"Unexpected argument(s) for {tool_name}: {', '.join(extra)}"
        )


def _req_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _ToolInputError(f"Argument '{key}' is required and must be a non-empty string")
    return value


def _opt_str(arguments: dict[str, Any], key: str) -> str | None:
    if key not in arguments or arguments[key] is None:
        return None
    value = arguments[key]
    if not isinstance(value, str):
        raise _ToolInputError(f"Argument '{key}' must be a string")
    return value


def _opt_bool(arguments: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in arguments or arguments[key] is None:
        return default
    value = arguments[key]
    if not isinstance(value, bool):
        raise _ToolInputError(f"Argument '{key}' must be a boolean")
    return value


def _opt_int(
    arguments: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int | None = None,
) -> int:
    if key not in arguments or arguments[key] is None:
        return default
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ToolInputError(f"Argument '{key}' must be an integer")
    if minimum is not None and value < minimum:
        raise _ToolInputError(f"Argument '{key}' must be >= {minimum}")
    return value


def _opt_number(
    arguments: dict[str, Any],
    key: str,
    *,
    default: float,
    minimum: float | None = None,
    exclusive_minimum: float | None = None,
) -> float:
    if key not in arguments or arguments[key] is None:
        return default
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ToolInputError(f"Argument '{key}' must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise _ToolInputError(f"Argument '{key}' must be >= {minimum}")
    if exclusive_minimum is not None and number <= exclusive_minimum:
        raise _ToolInputError(f"Argument '{key}' must be > {exclusive_minimum}")
    return number


def _req_task_list(arguments: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = arguments.get(key)
    if not isinstance(value, list) or not value:
        raise _ToolInputError(
            f"Argument '{key}' is required and must be a non-empty list of task objects"
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _ToolInputError(f"Task at index {index} must be a JSON object")
    return value


TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "super_agent_doctor",
        "description": (
            "Inspect active host detection, single-host adapter health and capabilities, "
            "Git worktree support, and whether Antigravity shell fallback is recommended."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace directory path to inspect.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_plan_create",
        "description": (
            "Validate a module task DAG and persist a draft Super-Agent run waiting for "
            "explicit user approval before any independent windows are launched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Target workspace directory path.",
                },
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Task specifications forming an acyclic execution DAG.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Unique task identifier (e.g. 'module-a').",
                            },
                            "title": {
                                "type": "string",
                                "description": "Short human-readable task title.",
                            },
                            "goal": {
                                "type": "string",
                                "description": "Detailed task objective and instructions.",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Upstream task IDs that must succeed first.",
                            },
                            "write_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Exclusive repository-relative paths this task may edit "
                                    "(must be empty for verifier tasks)."
                                ),
                            },
                            "acceptance_checks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Commands or checks required to verify completion.",
                            },
                            "role": {
                                "type": "string",
                                "enum": ["worker", "verifier"],
                                "default": "worker",
                            },
                            "model": {
                                "type": "string",
                                "description": "Optional model override for this window.",
                            },
                            "timeout_sec": {
                                "type": "integer",
                                "minimum": 1,
                                "default": 1800,
                            },
                            "max_retries": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 1,
                                "default": 1,
                            },
                        },
                        "required": ["id", "title", "goal"],
                    },
                },
                "max_concurrency": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 3,
                    "description": "Maximum concurrent independent windows.",
                },
                "host": {
                    "type": "string",
                    "enum": ["antigravity", "codex", "claude", "opencode", "custom"],
                    "description": "Optional explicit single-host adapter binding.",
                },
                "isolation_mode": {
                    "type": "string",
                    "enum": ["auto", "worktree", "shared"],
                    "default": "auto",
                    "description": "Workspace isolation mode ('auto' prefers Git worktrees).",
                },
                "auto_add_verifier": {
                    "type": "boolean",
                    "default": True,
                    "description": "Automatically append a read-only verifier task if none exists.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Optional custom run identifier.",
                },
            },
            "required": ["workspace", "tasks"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_plan_approve",
        "description": "Approve a draft run after explicit user confirmation so tasks can be dispatched.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier to approve.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_dispatch_ready",
        "description": (
            "Launch ready pending tasks in fresh independent host windows up to "
            "max_concurrency while respecting dependencies and exclusive write scopes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier to dispatch ready tasks for.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_poll_run",
        "description": "Poll active independent windows once, harvest finished results, and update run state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier to poll.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_wait_step",
        "description": (
            "Poll an active run in bounded intervals until a task changes state, "
            "no active tasks remain, or timeout_sec elapses."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier to wait on.",
                },
                "timeout_sec": {
                    "type": "number",
                    "minimum": 0,
                    "default": 30.0,
                    "description": "Maximum seconds to wait in this bounded step.",
                },
                "poll_interval_sec": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 1.0,
                    "description": "Seconds between poll checks.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_retry_task",
        "description": (
            "Retry a failed, timed-out, or cancelled task in a fresh window without "
            "rerunning already-succeeded tasks, optionally resetting blocked dependents."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier containing the task.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task identifier to retry.",
                },
                "reset_dependents": {
                    "type": "boolean",
                    "default": True,
                    "description": "Reset downstream blocked tasks back to pending.",
                },
            },
            "required": ["run_id", "task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_cancel_run",
        "description": "Request cancellation of active independent windows and mark the run cancelled.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier to cancel.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_integrate_run",
        "description": (
            "Consolidate succeeded worker patches in DAG order inside an integration worktree "
            "and generate final.patch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier to integrate.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_summarize_run",
        "description": (
            "Return a structured summary of run status, per-task results, window session IDs, "
            "resume hints, verification evidence, and final.patch status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier to summarize.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_apply_integration",
        "description": (
            "Dry-run check or apply the consolidated final.patch to the primary workspace "
            "after explicit user confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier whose integration patch should be checked or applied.",
                },
                "check_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, only verify that final.patch applies cleanly without modifying files.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "super_agent_list_runs",
        "description": "List persisted Super-Agent runs sorted by most recently updated first.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)


def _read_exact(reader: Any, num_bytes: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = num_bytes
    while remaining > 0:
        chunk = reader.read(remaining)
        if not chunk:
            return None
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _readline_bytes(reader: Any) -> bytes:
    line = reader.readline()
    if isinstance(line, str):
        return line.encode("utf-8")
    return line


def _read_framed_message(reader: Any) -> tuple[bytes | None, str, str | None]:
    """Read one JSON-RPC message supporting both newline-delimited and Content-Length framing.

    Returns (payload_bytes_or_None_on_EOF, framing_mode, framing_error_or_None).
    """
    while True:
        first_line = _readline_bytes(reader)
        if not first_line:
            return None, "newline", None
        stripped = first_line.strip()
        if not stripped:
            continue
        if stripped.startswith(b"\xef\xbb\xbf"):
            stripped = stripped[3:].strip()
            if not stripped:
                continue

        lower = stripped.lower()
        if lower.startswith((b"content-length:", b"content-type:")):
            headers: dict[str, str] = {}
            current_line = stripped
            while True:
                if b":" in current_line:
                    key_b, val_b = current_line.split(b":", 1)
                    headers[key_b.decode("latin-1").strip().lower()] = val_b.decode("latin-1").strip()
                next_line = _readline_bytes(reader)
                if not next_line:
                    return None, "content-length", None
                current_line = next_line.strip()
                if not current_line:
                    break

            raw_length = headers.get("content-length")
            if raw_length is None:
                return b"", "content-length", "Missing Content-Length header"
            try:
                length = int(raw_length)
            except ValueError:
                return b"", "content-length", f"Invalid Content-Length header: {raw_length!r}"
            if length < 0:
                return b"", "content-length", f"Negative Content-Length header: {length}"

            body = _read_exact(reader, length)
            if body is None:
                return None, "content-length", None
            return body, "content-length", None

        return stripped, "newline", None


def _write_framed_message(writer: Any, message: dict[str, Any], framing: str) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == "content-length":
        payload = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    else:
        payload = body + b"\n"

    if isinstance(writer, io.TextIOBase):
        writer.write(payload.decode("utf-8"))
    else:
        writer.write(payload)
    writer.flush()


class McpServer:
    """Zero-dependency MCP JSON-RPC 2.0 Stdio server wrapping SuperAgentEngine."""

    def __init__(
        self,
        engine: Any | None = None,
        *,
        run_root: str | Path | None = None,
    ) -> None:
        self._engine: Any | None = engine
        self._run_root: Path | None = Path(run_root) if run_root is not None else None
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "super_agent_doctor": self._tool_doctor,
            "super_agent_plan_create": self._tool_plan_create,
            "super_agent_plan_approve": self._tool_plan_approve,
            "super_agent_dispatch_ready": self._tool_dispatch_ready,
            "super_agent_poll_run": self._tool_poll_run,
            "super_agent_wait_step": self._tool_wait_step,
            "super_agent_retry_task": self._tool_retry_task,
            "super_agent_cancel_run": self._tool_cancel_run,
            "super_agent_integrate_run": self._tool_integrate_run,
            "super_agent_summarize_run": self._tool_summarize_run,
            "super_agent_apply_integration": self._tool_apply_integration,
            "super_agent_list_runs": self._tool_list_runs,
        }

    @property
    def engine(self) -> Any:
        if self._engine is None:
            from super_agent.engine import SuperAgentEngine

            if self._run_root is not None:
                from super_agent.store import RunStore

                self._engine = SuperAgentEngine(store=RunStore(self._run_root))
            else:
                self._engine = SuperAgentEngine()
        return self._engine

    # ------------------------------------------------------------------
    # Tool dispatchers
    # ------------------------------------------------------------------

    def _tool_doctor(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, {"workspace"}, "super_agent_doctor")
        workspace = _opt_str(arguments, "workspace")
        return self.engine.doctor(workspace=workspace)

    def _tool_plan_create(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(
            arguments,
            {
                "workspace",
                "tasks",
                "max_concurrency",
                "host",
                "isolation_mode",
                "auto_add_verifier",
                "run_id",
            },
            "super_agent_plan_create",
        )
        workspace = _req_str(arguments, "workspace")
        tasks = _req_task_list(arguments, "tasks")
        max_concurrency = _opt_int(arguments, "max_concurrency", default=3, minimum=1)
        host = _opt_str(arguments, "host")
        isolation_mode = _opt_str(arguments, "isolation_mode") or "auto"
        auto_add_verifier = _opt_bool(arguments, "auto_add_verifier", default=True)
        run_id = _opt_str(arguments, "run_id")
        return self.engine.create_plan(
            workspace,
            tasks,
            max_concurrency=max_concurrency,
            host=host,
            isolation_mode=isolation_mode,
            auto_add_verifier=auto_add_verifier,
            run_id=run_id,
        )

    def _tool_plan_approve(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, {"run_id"}, "super_agent_plan_approve")
        return self.engine.approve_plan(_req_str(arguments, "run_id"))

    def _tool_dispatch_ready(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, {"run_id"}, "super_agent_dispatch_ready")
        return self.engine.dispatch_ready(_req_str(arguments, "run_id"))

    def _tool_poll_run(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, {"run_id"}, "super_agent_poll_run")
        return self.engine.poll_run(_req_str(arguments, "run_id"))

    def _tool_wait_step(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(
            arguments,
            {"run_id", "timeout_sec", "poll_interval_sec"},
            "super_agent_wait_step",
        )
        run_id = _req_str(arguments, "run_id")
        timeout_sec = _opt_number(arguments, "timeout_sec", default=30.0, minimum=0.0)
        poll_interval_sec = _opt_number(
            arguments,
            "poll_interval_sec",
            default=1.0,
            exclusive_minimum=0.0,
        )
        return self.engine.wait_step(
            run_id,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    def _tool_retry_task(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(
            arguments,
            {"run_id", "task_id", "reset_dependents"},
            "super_agent_retry_task",
        )
        run_id = _req_str(arguments, "run_id")
        task_id = _req_str(arguments, "task_id")
        reset_dependents = _opt_bool(arguments, "reset_dependents", default=True)
        return self.engine.retry_task(
            run_id,
            task_id,
            reset_dependents=reset_dependents,
        )

    def _tool_cancel_run(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, {"run_id"}, "super_agent_cancel_run")
        return self.engine.cancel_run(_req_str(arguments, "run_id"))

    def _tool_integrate_run(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, {"run_id"}, "super_agent_integrate_run")
        return self.engine.integrate_run(_req_str(arguments, "run_id"))

    def _tool_summarize_run(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, {"run_id"}, "super_agent_summarize_run")
        return self.engine.summarize_run(_req_str(arguments, "run_id"))

    def _tool_apply_integration(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(
            arguments,
            {"run_id", "check_only"},
            "super_agent_apply_integration",
        )
        run_id = _req_str(arguments, "run_id")
        check_only = _opt_bool(arguments, "check_only", default=False)
        return self.engine.apply_integration(run_id, check_only=check_only)

    def _tool_list_runs(self, arguments: dict[str, Any]) -> Any:
        _check_allowed_keys(arguments, set(), "super_agent_list_runs")
        runs = self.engine.list_runs()
        if isinstance(runs, dict):
            return runs
        return {"runs": runs}

    # ------------------------------------------------------------------
    # JSON-RPC 2.0 / MCP request dispatch
    # ------------------------------------------------------------------

    def _handle_initialize(self, params: Any) -> dict[str, Any]:
        if params is not None and not isinstance(params, dict):
            raise _JsonRpcError(INVALID_PARAMS, "Invalid params: initialize params must be an object")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
            negotiated = requested
        else:
            negotiated = DEFAULT_PROTOCOL_VERSION
        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {
                    "listChanged": False,
                },
            },
            "serverInfo": {
                "name": "super-agent",
                "version": __version__,
            },
        }

    def _handle_tools_call(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _JsonRpcError(INVALID_PARAMS, "Invalid params: tools/call params must be an object")

        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise _JsonRpcError(INVALID_PARAMS, "Invalid params: 'name' must be a non-empty string")

        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            raise _JsonRpcError(INVALID_PARAMS, f"Unknown tool: {tool_name}")

        raw_arguments = params.get("arguments")
        if raw_arguments is None:
            arguments: dict[str, Any] = {}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            raise _JsonRpcError(INVALID_PARAMS, "Invalid params: 'arguments' must be an object")

        try:
            payload = handler(arguments)
        except Exception as exc:  # noqa: BLE001 — tool failures must return isError without crashing server
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False, indent=2),
                }
            ],
            "structuredContent": payload,
            "isError": False,
        }

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC 2.0 message. Returns None for notifications."""
        if not isinstance(message, dict):
            return _make_error_response(None, INVALID_REQUEST, "Invalid Request: expected JSON object")

        is_notification = "id" not in message
        req_id = message.get("id")

        if message.get("jsonrpc") != JSONRPC_VERSION:
            if is_notification:
                return None
            return _make_error_response(
                req_id,
                INVALID_REQUEST,
                "Invalid Request: 'jsonrpc' must be '2.0'",
            )

        method = message.get("method")
        if not isinstance(method, str) or not method:
            if is_notification:
                return None
            return _make_error_response(
                req_id,
                INVALID_REQUEST,
                "Invalid Request: 'method' must be a non-empty string",
            )

        # JSON-RPC 2.0 notifications (no "id" member) receive no response.
        if is_notification:
            return None

        params = message.get("params")
        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                if params is not None and not isinstance(params, dict):
                    raise _JsonRpcError(
                        INVALID_PARAMS,
                        "Invalid params: tools/list params must be an object",
                    )
                result = {"tools": list(TOOL_DEFINITIONS)}
            elif method == "tools/call":
                result = self._handle_tools_call(params)
            else:
                raise _JsonRpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        except _JsonRpcError as exc:
            return _make_error_response(req_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001
            return _make_error_response(req_id, INTERNAL_ERROR, f"Internal error: {exc}")

        return _make_result_response(req_id, result)

    def handle_raw_bytes(self, raw: bytes) -> dict[str, Any] | None:
        """Decode raw UTF-8 JSON bytes and process the JSON-RPC message."""
        try:
            text = raw.decode("utf-8-sig")
            decoded = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _make_error_response(None, PARSE_ERROR, f"Parse error: {exc}")
        return self.handle_message(decoded)

    def serve_stdio(self, stdin: Any = None, stdout: Any = None) -> int:
        """Run the blocking stdio JSON-RPC loop until EOF."""
        reader = stdin if stdin is not None else sys.stdin.buffer
        writer = stdout if stdout is not None else sys.stdout.buffer
        if hasattr(reader, "buffer") and reader.buffer is not None:
            reader = reader.buffer
        if hasattr(writer, "buffer") and writer.buffer is not None:
            writer = writer.buffer

        try:
            while True:
                raw_payload, framing, framing_error = _read_framed_message(reader)
                if raw_payload is None:
                    break

                if framing_error is not None:
                    response: dict[str, Any] | None = _make_error_response(
                        None,
                        PARSE_ERROR,
                        f"Parse error: {framing_error}",
                    )
                else:
                    response = self.handle_raw_bytes(raw_payload)

                if response is not None:
                    _write_framed_message(writer, response, framing)
        except (KeyboardInterrupt, BrokenPipeError):
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"super-agent mcp_server fatal error: {exc}", file=sys.stderr)
            return 1
        return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="super-agent-mcp",
        description="Run the Super-Agent MCP JSON-RPC 2.0 stdio server.",
    )
    parser.add_argument(
        "--run-root",
        default=None,
        help="Optional override directory for persisted Super-Agent runs.",
    )
    return parser


def main(argv: list[str] | None = None, *, engine: Any | None = None) -> int:
    """CLI entrypoint for `python -m super_agent.mcp_server`."""
    args = _build_parser().parse_args(argv)
    server = McpServer(engine=engine, run_root=args.run_root)
    return server.serve_stdio()


__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "McpServer",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOL_DEFINITIONS",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
