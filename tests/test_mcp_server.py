"""Tests for Super-Agent MCP JSON-RPC 2.0 Stdio server, CLI, and module entrypoint."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from super_agent import __version__
from super_agent import cli
from super_agent.mcp_server import (
    DEFAULT_PROTOCOL_VERSION,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TOOL_DEFINITIONS,
    McpServer,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS: tuple[str, ...] = (
    "super_agent_doctor",
    "super_agent_plan_create",
    "super_agent_plan_approve",
    "super_agent_dispatch_ready",
    "super_agent_poll_run",
    "super_agent_wait_step",
    "super_agent_retry_task",
    "super_agent_cancel_run",
    "super_agent_integrate_run",
    "super_agent_summarize_run",
    "super_agent_apply_integration",
    "super_agent_list_runs",
)


class StubEngine:
    """In-memory stub matching the frozen SuperAgentEngine contract."""

    def __init__(self, *, fail_method: str | None = None, fail_message: str = "boom") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_method = fail_method
        self.fail_message = fail_message
        self.runs: list[dict[str, Any]] = [
            {
                "run_id": "run-001",
                "status": "completed",
                "host": "codex",
                "workspace": "/repo",
                "isolation_mode": "worktree",
                "task_count": 2,
                "created_at": "2026-01-01T00:00:00.000000Z",
                "updated_at": "2026-01-01T00:05:00.000000Z",
                "run_dir": "/runs/run-001",
            }
        ]

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        if self.fail_method == method:
            raise RuntimeError(self.fail_message)

    def doctor(self, workspace: str | Path | None = None) -> dict[str, Any]:
        self._record("doctor", {"workspace": workspace})
        return {
            "detected_host": "codex",
            "recommended_host": "codex",
            "shell_fallback_recommended": False,
            "git_worktree_available": True,
            "workspace": str(workspace) if workspace is not None else None,
            "hosts": {
                "codex": {
                    "available": True,
                    "health": {"available": True, "reason": "ok"},
                }
            },
        }

    def create_plan(
        self,
        workspace: str | Path,
        tasks: list[dict[str, Any]],
        *,
        max_concurrency: int = 3,
        host: str | None = None,
        isolation_mode: str = "auto",
        auto_add_verifier: bool = True,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "create_plan",
            {
                "workspace": workspace,
                "tasks": tasks,
                "max_concurrency": max_concurrency,
                "host": host,
                "isolation_mode": isolation_mode,
                "auto_add_verifier": auto_add_verifier,
                "run_id": run_id,
            },
        )
        return {
            "run_id": run_id or "run-created",
            "status": "draft",
            "workspace": str(workspace),
            "task_count": len(tasks),
            "max_concurrency": max_concurrency,
            "isolation_mode": isolation_mode,
        }

    def approve_plan(self, run_id: str) -> dict[str, Any]:
        self._record("approve_plan", {"run_id": run_id})
        return {"run_id": run_id, "status": "approved"}

    def dispatch_ready(self, run_id: str) -> dict[str, Any]:
        self._record("dispatch_ready", {"run_id": run_id})
        return {"run_id": run_id, "dispatched": ["task-1"], "status": "running"}

    def poll_run(self, run_id: str) -> dict[str, Any]:
        self._record("poll_run", {"run_id": run_id})
        return {"run_id": run_id, "status": "running", "active_tasks": ["task-1"]}

    def wait_step(
        self,
        run_id: str,
        *,
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 1.0,
    ) -> dict[str, Any]:
        self._record(
            "wait_step",
            {
                "run_id": run_id,
                "timeout_sec": timeout_sec,
                "poll_interval_sec": poll_interval_sec,
            },
        )
        return {"run_id": run_id, "changed": True, "status": "running"}

    def retry_task(
        self,
        run_id: str,
        task_id: str,
        *,
        reset_dependents: bool = True,
    ) -> dict[str, Any]:
        self._record(
            "retry_task",
            {
                "run_id": run_id,
                "task_id": task_id,
                "reset_dependents": reset_dependents,
            },
        )
        return {"run_id": run_id, "task_id": task_id, "reset_dependents": reset_dependents}

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        self._record("cancel_run", {"run_id": run_id})
        return {"run_id": run_id, "status": "cancelled"}

    def integrate_run(self, run_id: str) -> dict[str, Any]:
        self._record("integrate_run", {"run_id": run_id})
        return {"run_id": run_id, "patch_path": "/runs/final.patch", "status": "integrated"}

    def summarize_run(self, run_id: str) -> dict[str, Any]:
        self._record("summarize_run", {"run_id": run_id})
        return {"run_id": run_id, "status": "completed", "summary": "all passed"}

    def apply_integration(self, run_id: str, *, check_only: bool = False) -> dict[str, Any]:
        self._record("apply_integration", {"run_id": run_id, "check_only": check_only})
        return {"run_id": run_id, "check_only": check_only, "applied": not check_only}

    def list_runs(self) -> list[dict[str, Any]]:
        self._record("list_runs", {})
        return list(self.runs)


def _encode_lsp(payload: dict[str, Any], *, include_content_type: bool = False) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n"
    if include_content_type:
        header = "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n" + header
    return header.encode("ascii") + b"\r\n" + body


def _parse_lsp_stream(raw: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pos = 0
    while pos < len(raw):
        sep = raw.index(b"\r\n\r\n", pos)
        header_block = raw[pos:sep].decode("ascii")
        length = None
        for line in header_block.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        assert length is not None
        body_start = sep + 4
        body_end = body_start + length
        messages.append(json.loads(raw[body_start:body_end].decode("utf-8")))
        pos = body_end
    return messages


# ---------------------------------------------------------------------------
# McpServer protocol & tool dispatch tests
# ---------------------------------------------------------------------------


def test_mcp_initialize_notifications_and_ping() -> None:
    server = McpServer(engine=StubEngine())

    # Echo supported protocol version
    resp = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        }
    )
    assert resp is not None
    assert resp["id"] == 1
    assert resp["result"] == {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "super-agent", "version": __version__},
    }

    # Unknown protocol version falls back to default
    resp2 = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "init-2",
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )
    assert resp2 is not None
    assert resp2["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION

    # Notification produces no response
    notif = server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert notif is None

    # Arbitrary notification also produces no response
    unknown_notif = server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}}
    )
    assert unknown_notif is None

    # Ping returns empty object
    ping = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert ping == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_mcp_tools_list_all_12_schemas() -> None:
    server = McpServer(engine=StubEngine())
    resp = server.handle_message({"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
    assert resp is not None
    tools = resp["result"]["tools"]
    assert len(tools) == 12
    names = tuple(tool["name"] for tool in tools)
    assert names == EXPECTED_TOOLS

    tool_by_name = {tool["name"]: tool for tool in tools}
    for name in EXPECTED_TOOLS:
        entry = tool_by_name[name]
        assert isinstance(entry["description"], str) and entry["description"].strip()
        schema = entry["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)

    assert set(tool_by_name["super_agent_plan_create"]["inputSchema"]["required"]) == {
        "workspace",
        "tasks",
    }
    assert set(tool_by_name["super_agent_retry_task"]["inputSchema"]["required"]) == {
        "run_id",
        "task_id",
    }
    assert "required" not in tool_by_name["super_agent_doctor"]["inputSchema"]
    assert "required" not in tool_by_name["super_agent_list_runs"]["inputSchema"]
    assert len(TOOL_DEFINITIONS) == 12


def test_mcp_tools_call_all_12_tools() -> None:
    engine = StubEngine()
    server = McpServer(engine=engine)
    sample_tasks = [
        {
            "id": "mod-a",
            "title": "Implement A",
            "goal": "Create module A",
            "write_paths": ["src/a.py"],
        }
    ]

    cases: list[tuple[str, dict[str, Any], str, dict[str, Any]]] = [
        ("super_agent_doctor", {"workspace": "/ws"}, "doctor", {"workspace": "/ws"}),
        (
            "super_agent_plan_create",
            {
                "workspace": "/ws",
                "tasks": sample_tasks,
                "max_concurrency": 2,
                "host": "codex",
                "isolation_mode": "worktree",
                "auto_add_verifier": False,
                "run_id": "run-xyz",
            },
            "create_plan",
            {
                "workspace": "/ws",
                "tasks": sample_tasks,
                "max_concurrency": 2,
                "host": "codex",
                "isolation_mode": "worktree",
                "auto_add_verifier": False,
                "run_id": "run-xyz",
            },
        ),
        ("super_agent_plan_approve", {"run_id": "run-xyz"}, "approve_plan", {"run_id": "run-xyz"}),
        (
            "super_agent_dispatch_ready",
            {"run_id": "run-xyz"},
            "dispatch_ready",
            {"run_id": "run-xyz"},
        ),
        ("super_agent_poll_run", {"run_id": "run-xyz"}, "poll_run", {"run_id": "run-xyz"}),
        (
            "super_agent_wait_step",
            {"run_id": "run-xyz", "timeout_sec": 12.5, "poll_interval_sec": 0.25},
            "wait_step",
            {"run_id": "run-xyz", "timeout_sec": 12.5, "poll_interval_sec": 0.25},
        ),
        (
            "super_agent_retry_task",
            {"run_id": "run-xyz", "task_id": "mod-a", "reset_dependents": False},
            "retry_task",
            {"run_id": "run-xyz", "task_id": "mod-a", "reset_dependents": False},
        ),
        ("super_agent_cancel_run", {"run_id": "run-xyz"}, "cancel_run", {"run_id": "run-xyz"}),
        (
            "super_agent_integrate_run",
            {"run_id": "run-xyz"},
            "integrate_run",
            {"run_id": "run-xyz"},
        ),
        (
            "super_agent_summarize_run",
            {"run_id": "run-xyz"},
            "summarize_run",
            {"run_id": "run-xyz"},
        ),
        (
            "super_agent_apply_integration",
            {"run_id": "run-xyz", "check_only": True},
            "apply_integration",
            {"run_id": "run-xyz", "check_only": True},
        ),
        ("super_agent_list_runs", {}, "list_runs", {}),
    ]

    for idx, (tool_name, arguments, expected_method, expected_kwargs) in enumerate(cases, start=1):
        resp = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": idx,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )
        assert resp is not None
        assert "result" in resp, f"Unexpected error for {tool_name}: {resp}"
        result = resp["result"]
        assert result["isError"] is False
        assert isinstance(result["structuredContent"], dict)
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        parsed_text = json.loads(result["content"][0]["text"])
        assert parsed_text == result["structuredContent"]

        recorded_method, recorded_kwargs = engine.calls[-1]
        assert recorded_method == expected_method
        assert recorded_kwargs == expected_kwargs

    # Verify list_runs wraps list into {"runs": [...]} object
    last_resp = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "super_agent_list_runs"},
        }
    )
    assert last_resp is not None
    assert last_resp["result"]["structuredContent"] == {"runs": engine.runs}


def test_mcp_error_handling_codes_and_is_error() -> None:
    engine = StubEngine(fail_method="approve_plan", fail_message="run not found")
    server = McpServer(engine=engine)

    # 1. Domain exception -> MCP tool result with isError=True (not JSON-RPC crash)
    domain_err = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "super_agent_plan_approve", "arguments": {"run_id": "missing"}},
        }
    )
    assert domain_err is not None
    assert domain_err["result"] == {
        "content": [{"type": "text", "text": "run not found"}],
        "isError": True,
    }

    # 2. Tool argument validation error -> isError=True
    missing_arg = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "super_agent_plan_approve", "arguments": {}},
        }
    )
    assert missing_arg is not None
    assert missing_arg["result"]["isError"] is True
    assert "run_id" in missing_arg["result"]["content"][0]["text"]

    # Reject bool where int is required
    bool_int = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "super_agent_plan_create",
                "arguments": {
                    "workspace": "/ws",
                    "tasks": [{"id": "t1", "title": "T1", "goal": "G"}],
                    "max_concurrency": True,
                },
            },
        }
    )
    assert bool_int is not None
    assert bool_int["result"]["isError"] is True

    # Reject unexpected extra tool argument
    extra_arg = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "super_agent_doctor", "arguments": {"bogus": 1}},
        }
    )
    assert extra_arg is not None
    assert extra_arg["result"]["isError"] is True

    # 3. Unknown method -> -32601
    unknown_method = server.handle_message(
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}
    )
    assert unknown_method is not None
    assert unknown_method["error"]["code"] == METHOD_NOT_FOUND

    # 4. Unknown tool -> -32602
    unknown_tool = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
    )
    assert unknown_tool is not None
    assert unknown_tool["error"]["code"] == INVALID_PARAMS

    # 5. Non-dict params / non-dict arguments -> -32602
    bad_params = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "super_agent_doctor", "arguments": ["not-a-dict"]},
        }
    )
    assert bad_params is not None
    assert bad_params["error"]["code"] == INVALID_PARAMS

    # 6. Invalid JSON -> -32700
    parse_err = server.handle_raw_bytes(b"{broken json")
    assert parse_err is not None
    assert parse_err["error"]["code"] == PARSE_ERROR
    assert parse_err["id"] is None

    # 7. Invalid Request -> -32600
    bad_req = server.handle_message({"jsonrpc": "1.0", "id": 8, "method": "ping"})
    assert bad_req is not None
    assert bad_req["error"]["code"] == INVALID_REQUEST


def test_mcp_stdio_newline_framing_with_recovery_and_unicode() -> None:
    server = McpServer(engine=StubEngine())
    lines = [
        b"\xef\xbb\xbf" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode("utf-8"),
        b"",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8"),
        b"not-valid-json",
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "super_agent_doctor",
                    "arguments": {"workspace": "/项目/工作区"},
                },
            },
            ensure_ascii=False,
        ).encode("utf-8"),
    ]
    stdin = io.BytesIO(b"\r\n".join(lines) + b"\n")
    stdout = io.BytesIO()

    exit_code = server.serve_stdio(stdin=stdin, stdout=stdout)
    assert exit_code == 0

    raw_out = stdout.getvalue().decode("utf-8")
    out_lines = [line for line in raw_out.splitlines() if line]
    assert len(out_lines) == 3  # init, parse error, doctor (notification omitted!)

    resp_init = json.loads(out_lines[0])
    resp_parse = json.loads(out_lines[1])
    resp_doc = json.loads(out_lines[2])

    assert resp_init["id"] == 1
    assert resp_init["result"]["serverInfo"]["name"] == "super-agent"
    assert resp_parse["error"]["code"] == PARSE_ERROR
    assert resp_doc["id"] == 2
    assert resp_doc["result"]["isError"] is False
    assert resp_doc["result"]["structuredContent"]["workspace"] == "/项目/工作区"


def test_mcp_stdio_content_length_framing() -> None:
    server = McpServer(engine=StubEngine())
    stream_in = b"".join(
        [
            _encode_lsp(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                include_content_type=True,
            ),
            _encode_lsp({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            _encode_lsp({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
            _encode_lsp(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "super_agent_doctor",
                        "arguments": {"workspace": "/中文路径/repo"},
                    },
                }
            ),
            b"Content-Length: not-a-number\r\n\r\n",
        ]
    )
    stdin = io.BytesIO(stream_in)
    stdout = io.BytesIO()

    exit_code = server.serve_stdio(stdin=stdin, stdout=stdout)
    assert exit_code == 0

    raw_out = stdout.getvalue()
    assert raw_out.startswith(b"Content-Length: ")
    responses = _parse_lsp_stream(raw_out)
    assert len(responses) == 4  # init, ping, doctor, parse error (notification omitted!)
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["protocolVersion"] == "2025-03-26"
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert responses[2]["id"] == 3
    assert responses[2]["result"]["structuredContent"]["workspace"] == "/中文路径/repo"
    assert responses[3]["error"]["code"] == PARSE_ERROR


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_engine_subcommands_json_and_human(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = StubEngine()
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "a", "title": "Task A", "goal": "Do A", "write_paths": ["a.py"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    # doctor with --json before subcommand
    rc = cli.main(["--json", "doctor", "--workspace", str(tmp_path)], engine=engine)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["detected_host"] == "codex"

    # doctor human output
    rc = cli.main(["doctor"], engine=engine)
    assert rc == 0
    human_doc = capsys.readouterr().out
    assert "Detected host:" in human_doc
    assert "codex: available" in human_doc

    # plan-create via --tasks-file
    rc = cli.main(
        [
            "plan-create",
            "--workspace",
            str(tmp_path),
            "--tasks-file",
            str(tasks_file),
            "--max-concurrency",
            "2",
            "--host",
            "codex",
            "--isolation",
            "worktree",
            "--no-auto-verifier",
            "--run-id",
            "run-cli-1",
            "--json",
        ],
        engine=engine,
    )
    assert rc == 0
    created = json.loads(capsys.readouterr().out)
    assert created["run_id"] == "run-cli-1"
    assert engine.calls[-1][0] == "create_plan"
    assert engine.calls[-1][1]["auto_add_verifier"] is False
    assert engine.calls[-1][1]["isolation_mode"] == "worktree"

    # plan-create via --tasks-json
    rc = cli.main(
        [
            "plan-create",
            "--workspace",
            str(tmp_path),
            "--tasks-json",
            json.dumps([{"id": "b", "title": "B", "goal": "Do B"}]),
            "--json",
        ],
        engine=engine,
    )
    assert rc == 0
    capsys.readouterr()
    assert engine.calls[-1][1]["auto_add_verifier"] is True

    # Remaining lifecycle subcommands
    for argv, expected_method in [
        (["plan-approve", "--run-id", "run-cli-1", "--json"], "approve_plan"),
        (["dispatch", "--run-id", "run-cli-1", "--json"], "dispatch_ready"),
        (["poll", "--run-id", "run-cli-1", "--json"], "poll_run"),
        (
            ["wait", "--run-id", "run-cli-1", "--timeout", "5", "--interval", "0.5", "--json"],
            "wait_step",
        ),
        (
            ["retry", "--run-id", "run-cli-1", "--task-id", "a", "--no-reset-dependents", "--json"],
            "retry_task",
        ),
        (["cancel", "--run-id", "run-cli-1", "--json"], "cancel_run"),
        (["integrate", "--run-id", "run-cli-1", "--json"], "integrate_run"),
        (["summarize", "--run-id", "run-cli-1", "--json"], "summarize_run"),
        (["apply", "--run-id", "run-cli-1", "--check-only", "--json"], "apply_integration"),
        (["list-runs", "--json"], "list_runs"),
    ]:
        rc = cli.main(argv, engine=engine)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed is not None
        assert engine.calls[-1][0] == expected_method

    # list-runs human output
    rc = cli.main(["list-runs"], engine=engine)
    assert rc == 0
    assert "run-001" in capsys.readouterr().out


def test_cli_error_exit_codes_and_json_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_engine = StubEngine(fail_method="poll_run", fail_message="run missing")

    # Plain stderr error
    rc = cli.main(["poll", "--run-id", "nope"], engine=failing_engine)
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run missing" in captured.err

    # Structured JSON stderr error
    rc = cli.main(["poll", "--run-id", "nope", "--json"], engine=failing_engine)
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err_obj = json.loads(captured.err)
    assert err_obj["error"] == "run missing"
    assert err_obj["error_type"] == "RuntimeError"

    # Invalid tasks JSON
    rc = cli.main(
        ["plan-create", "--workspace", str(tmp_path), "--tasks-json", "{bad", "--json"],
        engine=StubEngine(),
    )
    assert rc == 1
    err_obj = json.loads(capsys.readouterr().err)
    assert "Invalid tasks JSON" in err_obj["error"]

    # Invalid wait flags
    rc = cli.main(
        ["wait", "--run-id", "r1", "--interval", "0", "--json"],
        engine=StubEngine(),
    )
    assert rc == 1
    assert "interval" in json.loads(capsys.readouterr().err)["error"]


def test_cli_install_and_uninstall_project_and_user_scopes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = tmp_path / "project"
    user_home = tmp_path / "fake-home"
    project_dir.mkdir()
    user_home.mkdir()

    # Project scope install
    rc = cli.main(
        [
            "install",
            "--host",
            "codex",
            "--scope",
            "project",
            "--target",
            str(project_dir),
            "--python",
            sys.executable,
            "--json",
        ]
    )
    assert rc == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["host"] == "codex"
    assert installed["scope"] == "project"
    assert Path(installed["plugin_dir"]).is_dir()
    assert Path(installed["skill_dir"]).is_dir()

    # Project scope uninstall
    rc = cli.main(
        [
            "uninstall",
            "--host",
            "codex",
            "--scope",
            "project",
            "--target",
            str(project_dir),
            "--json",
        ]
    )
    assert rc == 0
    uninstalled = json.loads(capsys.readouterr().out)
    assert uninstalled["already_absent"] is False
    assert not Path(installed["plugin_dir"]).exists()

    # User scope install with --target redirected as home_dir
    rc = cli.main(
        [
            "install",
            "--host",
            "antigravity",
            "--scope",
            "user",
            "--target",
            str(user_home),
            "--json",
        ]
    )
    assert rc == 0
    user_installed = json.loads(capsys.readouterr().out)
    expected_plugin_dir = user_home / ".gemini" / "config" / "plugins" / "super-agent"
    assert Path(user_installed["plugin_dir"]) == expected_plugin_dir
    assert expected_plugin_dir.is_dir()

    # User scope uninstall (human mode)
    rc = cli.main(
        [
            "uninstall",
            "--host",
            "antigravity",
            "--scope",
            "user",
            "--target",
            str(user_home),
        ]
    )
    assert rc == 0
    assert "Uninstalled super-agent" in capsys.readouterr().out
    assert not expected_plugin_dir.exists()

    # Custom host with --host-config writes SUPER_AGENT_HOST_CONFIG into .mcp.json
    custom_cfg = tmp_path / "custom-host.json"
    custom_cfg.write_text('{"start": ["echo"]}\n', encoding="utf-8")
    rc = cli.main(
        [
            "install",
            "--host",
            "custom",
            "--scope",
            "project",
            "--target",
            str(project_dir),
            "--host-config",
            str(custom_cfg),
            "--json",
        ]
    )
    assert rc == 0
    custom_installed = json.loads(capsys.readouterr().out)
    mcp_data = json.loads(Path(custom_installed["mcp_config_path"]).read_text(encoding="utf-8"))
    assert (
        mcp_data["mcpServers"]["super-agent"]["env"]["SUPER_AGENT_HOST_CONFIG"]
        == str(custom_cfg.resolve())
    )


def test_cli_run_root_and_doctor_host_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_args: list[dict[str, Any]] = []
    stub = StubEngine()

    def _fake_build_engine(*, run_root: str | None = None, host_override: str | None = None) -> Any:
        captured_args.append({"run_root": run_root, "host_override": host_override})
        return stub

    monkeypatch.setattr(cli, "_build_engine", _fake_build_engine)

    # --run-root before subcommand and --host on doctor
    rc = cli.main(["--run-root", str(tmp_path / "r1"), "doctor", "--host", "claude", "--json"])
    assert rc == 0
    capsys.readouterr()
    assert captured_args[-1] == {
        "run_root": str(tmp_path / "r1"),
        "host_override": "claude",
    }

    # --run-root after subcommand
    rc = cli.main(["list-runs", "--run-root", str(tmp_path / "r2"), "--json"])
    assert rc == 0
    capsys.readouterr()
    assert captured_args[-1] == {
        "run_root": str(tmp_path / "r2"),
        "host_override": None,
    }


def test_cli_mcp_serve_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8") + b"\n"
    fake_in = io.BytesIO(req)
    fake_out = io.BytesIO()

    class _StreamWrap:
        def __init__(self, buf: io.BytesIO) -> None:
            self.buffer = buf

    monkeypatch.setattr(sys, "stdin", _StreamWrap(fake_in))
    monkeypatch.setattr(sys, "stdout", _StreamWrap(fake_out))

    rc = cli.main(["mcp-serve"], engine=StubEngine())
    assert rc == 0
    resp = json.loads(fake_out.getvalue().decode("utf-8").strip())
    assert resp == {"jsonrpc": "2.0", "id": 1, "result": {}}


# ---------------------------------------------------------------------------
# Real subprocess smoke tests
# ---------------------------------------------------------------------------


def test_subprocess_mcp_server_newline_and_content_length(tmp_path: Path) -> None:
    engine_ready = (REPO_ROOT / "super_agent" / "engine.py").is_file()
    cmd = [sys.executable, "-m", "super_agent.mcp_server", "--run-root", str(tmp_path / "runs")]

    # 1. Standard newline-delimited MCP stdio session
    requests: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    ]
    if engine_ready:
        requests.extend(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "super_agent_doctor", "arguments": {"workspace": str(tmp_path)}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "super_agent_list_runs", "arguments": {}},
                },
            ]
        )

    stdin_bytes = b"".join(json.dumps(req).encode("utf-8") + b"\n" for req in requests)
    proc = subprocess.run(
        cmd,
        input=stdin_bytes,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr.decode('utf-8', errors='replace')}"
    assert proc.stderr == b""

    lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
    expected_count = 5 if engine_ready else 3
    assert len(lines) == expected_count

    init_resp = json.loads(lines[0])
    assert init_resp["id"] == 1
    assert init_resp["result"]["serverInfo"] == {"name": "super-agent", "version": __version__}

    ping_resp = json.loads(lines[1])
    assert ping_resp == {"jsonrpc": "2.0", "id": 2, "result": {}}

    list_resp = json.loads(lines[2])
    assert list_resp["id"] == 3
    assert tuple(t["name"] for t in list_resp["result"]["tools"]) == EXPECTED_TOOLS

    if engine_ready:
        doc_resp = json.loads(lines[3])
        assert doc_resp["id"] == 4
        assert doc_resp["result"]["isError"] is False
        assert "detected_host" in doc_resp["result"]["structuredContent"]

        runs_resp = json.loads(lines[4])
        assert runs_resp["id"] == 5
        assert runs_resp["result"]["isError"] is False
        assert runs_resp["result"]["structuredContent"] == {"runs": []}

    # 2. Content-Length LSP-style framing over real subprocess pipe
    lsp_in = b"".join(
        [
            _encode_lsp({"jsonrpc": "2.0", "id": 10, "method": "initialize"}),
            _encode_lsp({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            _encode_lsp({"jsonrpc": "2.0", "id": 11, "method": "tools/list"}),
        ]
    )
    proc_lsp = subprocess.run(
        cmd,
        input=lsp_in,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=20,
        check=False,
    )
    assert proc_lsp.returncode == 0, f"stderr: {proc_lsp.stderr.decode('utf-8', errors='replace')}"
    assert proc_lsp.stderr == b""
    lsp_responses = _parse_lsp_stream(proc_lsp.stdout)
    assert len(lsp_responses) == 2
    assert lsp_responses[0]["id"] == 10
    assert lsp_responses[1]["id"] == 11
    assert len(lsp_responses[1]["result"]["tools"]) == 12


def test_subprocess_module_cli_version_and_install(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "super_agent", "--version"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0
    assert __version__ in proc.stdout

    proj = tmp_path / "cli-proj"
    proj.mkdir()
    proc_install = subprocess.run(
        [
            sys.executable,
            "-m",
            "super_agent",
            "install",
            "--host",
            "claude",
            "--scope",
            "project",
            "--target",
            str(proj),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=20,
        check=False,
    )
    assert proc_install.returncode == 0, proc_install.stderr
    payload = json.loads(proc_install.stdout)
    assert payload["host"] == "claude"
    assert Path(payload["plugin_dir"]).is_dir()
