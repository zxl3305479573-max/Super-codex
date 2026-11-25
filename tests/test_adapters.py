"""Comprehensive deterministic unit tests for Super-Agent single-host session adapters."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
import uuid

import pytest

from super_agent.adapters import (
    BUILTIN_HOSTS,
    AdapterError,
    AntigravityAdapter,
    ClaudeAdapter,
    CodexAdapter,
    CustomAdapter,
    OpenCodeAdapter,
    WindowStartRequest,
    detect_host,
    doctor,
    get_adapter,
)
from super_agent.adapters.base import is_pid_alive, resolve_command_argv
from super_agent.adapters.claude import _extract_claude_result
from super_agent.adapters.codex import _extract_thread_id
from super_agent.adapters.custom import find_custom_config_path, load_custom_config
from super_agent.models import TaskRole, TaskSpec


def _base_windows_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal isolated env that still allows spawning Python/.cmd on Windows."""
    env: dict[str, str] = {}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
    if extra:
        env.update(extra)
    return env


def _write_fake_cli(bin_dir: Path, name: str, script_body: str) -> Path:
    """Create a cross-platform fake CLI executable backed by the current Python interpreter."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    py_script = bin_dir / f"_{name}_impl.py"
    py_script.write_text(script_body, encoding="utf-8")

    if os.name == "nt":
        wrapper = bin_dir / f"{name}.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_script}" %*\r\n',
            encoding="utf-8",
        )
        return wrapper

    wrapper = bin_dir / name
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{py_script}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _make_request(
    tmp_path: Path,
    *,
    task: TaskSpec | None = None,
    read_only: bool = False,
    env: dict[str, str] | None = None,
    prompt_text: str = "Execute the assigned task carefully.",
) -> WindowStartRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    attempt_dir = tmp_path / "run" / "attempts" / "att-001"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = attempt_dir / "prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    return WindowStartRequest(
        run_id="run-123",
        task=task
        or TaskSpec(
            id="task-1",
            title="Build widget",
            goal="Implement the widget cleanly",
            role=TaskRole.WORKER,
            model="test-model-v1",
        ),
        attempt_id="att-001",
        workspace=workspace,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        result_path=attempt_dir / "result.json",
        last_message_path=attempt_dir / "last_message.txt",
        log_path=attempt_dir / "session.log",
        exit_code_path=attempt_dir / "exit_code.json",
        read_only=read_only,
        env=env,
    )


def _poll_until_done(adapter: Any, handle: Any, *, timeout: float = 10.0, env: dict[str, str] | None = None):
    deadline = time.monotonic() + timeout
    last_poll = None
    while time.monotonic() < deadline:
        last_poll = adapter.poll_window(handle, env=env)
        if last_poll.status in ("completed", "failed", "cancelled"):
            return last_poll
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for completion; last poll={last_poll}")


# ---------------------------------------------------------------------------
# 1. AntigravityAdapter tests (local http.server mock)
# ---------------------------------------------------------------------------


class _MockAntigravityServer:
    def __init__(self, expected_csrf: str) -> None:
        self.expected_csrf = expected_csrf
        self.requests: list[dict[str, Any]] = []
        self.run_state: str = "RUN_STATE_RUNNING"
        self.summary: str | None = None
        self.tags: list[str] = []
        self.conv_id: str = "conv-ag-98765"
        self.fail_next_with_secret_echo: bool = False

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                body = json.loads(raw)
                captured = {
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "connect_version": self.headers.get("Connect-Protocol-Version"),
                    "csrf": self.headers.get("x-antigravity-csrf-token"),
                    "body": body,
                }
                outer.requests.append(captured)

                if outer.fail_next_with_secret_echo:
                    outer.fail_next_with_secret_echo = False
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(
                        f"internal boom echoing token={outer.expected_csrf}".encode("utf-8")
                    )
                    return

                if captured["csrf"] != outer.expected_csrf:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"error": "forbidden"}')
                    return

                if self.path == "/product.v1.ConversationService/CreateConversation":
                    outer.tags = list(body.get("tags") or [])
                    resp = {
                        "conversation": {
                            "id": outer.conv_id,
                            "title": body.get("title"),
                            "tags": outer.tags,
                        }
                    }
                elif self.path == "/product.v1.ConversationService/GetConversation":
                    state: dict[str, Any] = {"runState": outer.run_state}
                    if outer.summary is not None:
                        state["summary"] = outer.summary
                    resp = {
                        "conversation": {
                            "id": body.get("conversationId"),
                            "tags": outer.tags,
                        },
                        "state": state,
                    }
                elif self.path == "/product.v1.ConversationService/UpdateConversation":
                    outer.tags = list(body.get("tags") or [])
                    resp = {
                        "conversation": {
                            "id": body.get("conversationId"),
                            "tags": outer.tags,
                        }
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return

                encoded = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"127.0.0.1:{self.server.server_port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _MockAntigravityServer:
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)


def test_antigravity_health_check_and_lifecycle(tmp_path: Path) -> None:
    secret_csrf = "super-secret-csrf-token-do-not-leak-9999"
    adapter = AntigravityAdapter()

    # Missing shell-scoped credentials -> shell_fallback_recommended=True.
    missing_health = adapter.health_check(env={})
    assert missing_health.available is False
    assert missing_health.shell_fallback_recommended is True
    assert "shell-scoped" in missing_health.detail.lower()
    assert "ANTIGRAVITY_LS_ADDRESS" in missing_health.detail
    assert "ANTIGRAVITY_CSRF_TOKEN" in missing_health.detail

    with _MockAntigravityServer(secret_csrf) as mock_srv:
        ag_env = {
            "ANTIGRAVITY_LS_ADDRESS": mock_srv.address,
            "ANTIGRAVITY_CSRF_TOKEN": secret_csrf,
            "ANTIGRAVITY_PROJECT_ID": "proj-alpha",
        }

        health = adapter.health_check(env=ag_env)
        assert health.available is True
        assert health.shell_fallback_recommended is False
        assert secret_csrf not in health.detail

        req = _make_request(tmp_path, env=ag_env, prompt_text="Implement feature X")
        handle = adapter.start_window(req)

        assert handle.host == "antigravity"
        assert handle.session_id == mock_srv.conv_id
        assert handle.title == "[Super-Agent] Build widget"
        assert handle.status == "running"
        assert handle.resume_hint == f"conversation://{mock_srv.conv_id}"
        assert handle.log_path == str(req.log_path)
        assert handle.metadata == {"run_id": "run-123", "task_id": "task-1"}

        # Verify CreateConversation HTTP request & exact Connect headers.
        assert len(mock_srv.requests) == 1
        create_call = mock_srv.requests[0]
        assert create_call["path"] == "/product.v1.ConversationService/CreateConversation"
        assert create_call["content_type"] == "application/json"
        assert create_call["connect_version"] == "1"
        assert create_call["csrf"] == secret_csrf
        assert create_call["body"] == {
            "title": "[Super-Agent] Build widget",
            "tags": ["super-agent", "run-123", "task-1"],
            "prompt": [{"text": "Implement feature X"}],
            "projectId": "proj-alpha",
            "model": "test-model-v1",
        }

        # CSRF token MUST NOT leak into handle or log file.
        assert secret_csrf not in json.dumps(handle.to_dict())
        assert secret_csrf not in req.log_path.read_text(encoding="utf-8")

        # Poll RUN_STATE_RUNNING -> running, cleanup_confirmed=False.
        poll_running = adapter.poll_window(handle, env=ag_env)
        assert poll_running.status == "running"
        assert poll_running.cleanup_confirmed is False
        assert poll_running.session_id == mock_srv.conv_id

        # Poll RUN_STATE_CANCELLING -> running, cleanup_confirmed=False.
        mock_srv.run_state = "RUN_STATE_CANCELLING"
        poll_cancelling = adapter.poll_window(handle, env=ag_env)
        assert poll_cancelling.status == "running"
        assert poll_cancelling.cleanup_confirmed is False

        # Cancel while running -> UpdateConversation adds cancel tag, confirmed=False.
        mock_srv.run_state = "RUN_STATE_RUNNING"
        cancel_active = adapter.cancel_window(handle, env=ag_env)
        assert cancel_active.confirmed is False
        assert "no force-stop RPC" in cancel_active.detail
        assert "super-agent:cancel-requested" in mock_srv.tags
        update_calls = [
            r for r in mock_srv.requests
            if r["path"] == "/product.v1.ConversationService/UpdateConversation"
        ]
        assert len(update_calls) == 1
        assert update_calls[0]["body"]["conversationId"] == mock_srv.conv_id
        assert "super-agent:cancel-requested" in update_calls[0]["body"]["tags"]

        # Poll RUN_STATE_IDLE -> completed, cleanup_confirmed=True, summary populated.
        mock_srv.run_state = "RUN_STATE_IDLE"
        mock_srv.summary = "Widget implemented and verified."
        poll_idle = adapter.poll_window(handle, env=ag_env)
        assert poll_idle.status == "completed"
        assert poll_idle.cleanup_confirmed is True
        assert poll_idle.summary == "Widget implemented and verified."

        # Cancel when already idle -> confirmed=True without another UpdateConversation.
        cancel_idle = adapter.cancel_window(handle, env=ag_env)
        assert cancel_idle.confirmed is True
        assert cancel_idle.detail == "Conversation already idle"

        # Error messages must redact CSRF token even if server echoes it back.
        mock_srv.fail_next_with_secret_echo = True
        with pytest.raises(AdapterError) as exc_info:
            adapter.poll_window(handle, env=ag_env)
        assert secret_csrf not in str(exc_info.value)
        assert "[REDACTED]" in str(exc_info.value)

        # Optional fields omitted when ANTIGRAVITY_PROJECT_ID and task.model are unset.
        minimal_env = {
            "ANTIGRAVITY_LS_ADDRESS": mock_srv.address,
            "ANTIGRAVITY_CSRF_TOKEN": secret_csrf,
        }
        minimal_task = TaskSpec(id="task-min", title="Minimal", goal="Minimal goal", model=None)
        minimal_req = _make_request(tmp_path / "min", task=minimal_task, env=minimal_env)
        adapter.start_window(minimal_req)
        min_call = mock_srv.requests[-1]
        assert "projectId" not in min_call["body"]
        assert "model" not in min_call["body"]

        # Best-effort cancel still returns confirmed=False even if UpdateConversation fails.
        mock_srv.run_state = "RUN_STATE_RUNNING"

        # Fail the UpdateConversation call (second call after GetConversation inside cancel_window).
        orig_update_path = "/product.v1.ConversationService/UpdateConversation"
        seen_get = False
        orig_do_post = mock_srv.server.RequestHandlerClass.do_POST

        def _failing_update_post(handler_self: Any) -> None:
            nonlocal seen_get
            if handler_self.path == orig_update_path and seen_get:
                handler_self.send_response(500)
                handler_self.end_headers()
                handler_self.wfile.write(b"update unavailable")
                return
            if handler_self.path == "/product.v1.ConversationService/GetConversation":
                seen_get = True
            orig_do_post(handler_self)

        mock_srv.server.RequestHandlerClass.do_POST = _failing_update_post  # type: ignore[assignment]
        try:
            res = adapter.cancel_window(handle, env=minimal_env)
            assert res.confirmed is False
            assert "no force-stop RPC" in res.detail
        finally:
            mock_srv.server.RequestHandlerClass.do_POST = orig_do_post  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 2. CodexAdapter tests (fake managed CLI)
# ---------------------------------------------------------------------------


def test_codex_adapter_lifecycle_and_cancel(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fake_bin"
    capture_file = tmp_path / "codex_capture.json"
    fake_script = f"""
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.42.0")
    sys.exit(0)

if os.environ.get("FAKE_CODEX_SLEEP") == "1":
    print(json.dumps({{"type": "thread.started", "thread_id": "thr-sleep-1"}}), flush=True)
    time.sleep(30)
    sys.exit(0)

stdin_text = sys.stdin.read()
Path({str(capture_file)!r}).write_text(
    json.dumps({{"args": args, "stdin": stdin_text}}),
    encoding="utf-8",
)
# Write last message to -o path
if "-o" in args:
    out_path = Path(args[args.index("-o") + 1])
    out_path.write_text("Codex final summary", encoding="utf-8")

print(json.dumps({{"type": "thread.started", "thread_id": "thr-abc-123"}}), flush=True)
print(json.dumps({{"type": "turn.completed"}}), flush=True)
if os.environ.get("FAKE_CODEX_FAIL") == "1":
    sys.exit(7)
sys.exit(0)
"""
    _write_fake_cli(bin_dir, "codex", fake_script)

    adapter = CodexAdapter()
    assert adapter.health_check(env=_base_windows_env({"PATH": ""})).available is False

    env = _base_windows_env({"PATH": str(bin_dir)})
    health = adapter.health_check(env=env)
    assert health.available is True
    assert health.version == "codex-cli 0.42.0"
    assert health.executable is not None
    assert "codex" in Path(health.executable).name.lower()

    # Run successful standalone `codex exec` thread.
    req = _make_request(tmp_path, read_only=True, env=env, prompt_text="Prompt via stdin")
    handle = adapter.start_window(req)
    assert handle.host == "codex"
    assert handle.pid is not None and handle.pid > 0

    poll = _poll_until_done(adapter, handle, env=env)
    assert poll.status == "completed"
    assert poll.exit_code == 0
    assert poll.cleanup_confirmed is True
    assert poll.session_id == "thr-abc-123"
    assert poll.resume_hint == "codex resume thr-abc-123"
    assert req.last_message_path.read_text(encoding="utf-8") == "Codex final summary"

    captured = json.loads(capture_file.read_text(encoding="utf-8"))
    assert captured["stdin"] == "Prompt via stdin"
    assert "spawn_agent" not in captured["args"]
    assert captured["args"] == [
        "exec",
        "-C",
        str(req.workspace),
        "--sandbox",
        "read-only",
        "--json",
        "-o",
        str(req.last_message_path),
        "--skip-git-repo-check",
        "-m",
        "test-model-v1",
        "-",
    ]

    # Non-zero exit -> failed with cleanup_confirmed=True.
    fail_env = _base_windows_env({"PATH": str(bin_dir), "FAKE_CODEX_FAIL": "1"})
    fail_req = _make_request(tmp_path / "fail_case", read_only=False, env=fail_env)
    fail_handle = adapter.start_window(fail_req)
    fail_poll = _poll_until_done(adapter, fail_handle, env=fail_env)
    assert fail_poll.status == "failed"
    assert fail_poll.exit_code == 7
    assert fail_poll.cleanup_confirmed is True

    # Long-running process -> running then cancelled cleanly.
    sleep_env = _base_windows_env({"PATH": str(bin_dir), "FAKE_CODEX_SLEEP": "1"})
    sleep_req = _make_request(tmp_path / "cancel_case", env=sleep_env)
    sleep_handle = adapter.start_window(sleep_req)
    time.sleep(0.3)
    running_poll = adapter.poll_window(sleep_handle, env=sleep_env)
    assert running_poll.status == "running"
    assert running_poll.cleanup_confirmed is False

    cancel_res = adapter.cancel_window(sleep_handle, env=sleep_env)
    assert cancel_res.confirmed is True
    assert not is_pid_alive(sleep_handle.pid)

    after_cancel_poll = adapter.poll_window(sleep_handle, env=sleep_env)
    assert after_cancel_poll.status == "failed"
    assert after_cancel_poll.cleanup_confirmed is True
    assert after_cancel_poll.error == "Managed runner exited without exit record"


def test_codex_thread_id_extraction_variants(tmp_path: Path) -> None:
    log1 = tmp_path / "log1.jsonl"
    log1.write_text('not-json\n{"thread": {"id": "thr-nested-1"}}\n', encoding="utf-8")
    assert _extract_thread_id(log1) == "thr-nested-1"

    log2 = tmp_path / "log2.jsonl"
    log2.write_text('{"session_id": "sess-fallback-2"}\n', encoding="utf-8")
    assert _extract_thread_id(log2) == "sess-fallback-2"


# ---------------------------------------------------------------------------
# 3. ClaudeAdapter tests (fake managed CLI)
# ---------------------------------------------------------------------------


def test_claude_adapter_lifecycle_and_cancel(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fake_bin"
    capture_file = tmp_path / "claude_capture.json"
    fake_script = f"""
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.0 (Claude Code)")
    sys.exit(0)

if os.environ.get("FAKE_CLAUDE_SLEEP") == "1":
    time.sleep(30)
    sys.exit(0)

stdin_text = sys.stdin.read()
Path({str(capture_file)!r}).write_text(
    json.dumps({{"args": args, "stdin": stdin_text}}),
    encoding="utf-8",
)
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "unknown"
print(json.dumps({{"type": "init", "session_id": sid}}), flush=True)
print(json.dumps({{"type": "result", "session_id": sid, "result": "Claude task completed cleanly"}}), flush=True)
sys.exit(0)
"""
    _write_fake_cli(bin_dir, "claude", fake_script)

    adapter = ClaudeAdapter()
    env = _base_windows_env({"PATH": str(bin_dir)})

    health = adapter.health_check(env=env)
    assert health.available is True
    assert health.version == "2.1.0 (Claude Code)"

    req = _make_request(tmp_path, read_only=False, env=env, prompt_text="Claude stdin prompt")
    handle = adapter.start_window(req)
    assert handle.host == "claude"
    assert handle.session_id is not None
    # Verify pre-generated UUID.
    uuid.UUID(handle.session_id)
    assert handle.resume_hint == f"claude --resume {handle.session_id}"

    poll = _poll_until_done(adapter, handle, env=env)
    assert poll.status == "completed"
    assert poll.exit_code == 0
    assert poll.cleanup_confirmed is True
    assert poll.session_id == handle.session_id
    assert poll.resume_hint == f"claude --resume {handle.session_id}"

    # Verify last_message_path extracted from session.log JSON result field.
    assert req.last_message_path.exists()
    assert req.last_message_path.read_text(encoding="utf-8") == "Claude task completed cleanly"

    captured = json.loads(capture_file.read_text(encoding="utf-8"))
    assert captured["stdin"] == "Claude stdin prompt"
    assert captured["args"] == [
        "-p",
        "--output-format",
        "json",
        "--session-id",
        handle.session_id,
        "-n",
        "[Super-Agent] Build widget",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "test-model-v1",
    ]

    # Test read_only -> --permission-mode plan.
    ro_req = _make_request(tmp_path / "ro", read_only=True, env=env)
    ro_handle = adapter.start_window(ro_req)
    _poll_until_done(adapter, ro_handle, env=env)
    ro_captured = json.loads(capture_file.read_text(encoding="utf-8"))
    assert "--permission-mode" in ro_captured["args"]
    assert ro_captured["args"][ro_captured["args"].index("--permission-mode") + 1] == "plan"

    # Test cancel_window terminates managed process tree.
    sleep_env = _base_windows_env({"PATH": str(bin_dir), "FAKE_CLAUDE_SLEEP": "1"})
    sleep_req = _make_request(tmp_path / "cancel_case", env=sleep_env)
    sleep_handle = adapter.start_window(sleep_req)
    time.sleep(0.25)
    cancel_res = adapter.cancel_window(sleep_handle, env=sleep_env)
    assert cancel_res.confirmed is True
    assert not is_pid_alive(sleep_handle.pid)


def test_claude_result_extraction_pretty_json(tmp_path: Path) -> None:
    log_path = tmp_path / "pretty.log"
    log_path.write_text(
        "warning: starting\n"
        + json.dumps({"type": "result", "result": "Pretty JSON output"}, indent=2),
        encoding="utf-8",
    )
    assert _extract_claude_result(log_path) == "Pretty JSON output"


# ---------------------------------------------------------------------------
# 4. OpenCodeAdapter tests (fake managed CLI)
# ---------------------------------------------------------------------------


def test_opencode_adapter_lifecycle_and_cancel(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fake_bin"
    capture_file = tmp_path / "opencode_capture.json"
    fake_script = f"""
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("opencode 1.3.5")
    sys.exit(0)

if os.environ.get("FAKE_OPENCODE_SLEEP") == "1":
    time.sleep(30)
    sys.exit(0)

Path({str(capture_file)!r}).write_text(
    json.dumps({{"args": args}}),
    encoding="utf-8",
)
print(json.dumps({{"type": "step_start", "sessionID": "ses_oc_777"}}), flush=True)
print(json.dumps({{"type": "text", "sessionID": "ses_oc_777", "part": {{"type": "text", "text": "OpenCode final answer"}}}}), flush=True)
sys.exit(0)
"""
    _write_fake_cli(bin_dir, "opencode", fake_script)

    adapter = OpenCodeAdapter()
    env = _base_windows_env(
        {
            "PATH": str(bin_dir),
            "OPENCODE_SERVER_URL": "http://127.0.0.1:4096",
        }
    )

    health = adapter.health_check(env=env)
    assert health.available is True
    assert health.version == "opencode 1.3.5"

    req = _make_request(tmp_path, env=env)
    handle = adapter.start_window(req)
    assert handle.host == "opencode"

    poll = _poll_until_done(adapter, handle, env=env)
    assert poll.status == "completed"
    assert poll.exit_code == 0
    assert poll.cleanup_confirmed is True
    assert poll.session_id == "ses_oc_777"
    assert poll.resume_hint == "opencode --session ses_oc_777"

    assert req.last_message_path.exists()
    assert req.last_message_path.read_text(encoding="utf-8") == "OpenCode final answer"

    captured = json.loads(capture_file.read_text(encoding="utf-8"))
    assert captured["args"] == [
        "run",
        "--format",
        "json",
        "--title",
        "[Super-Agent] Build widget",
        "-f",
        str(req.prompt_path),
        "-m",
        "test-model-v1",
        "--attach",
        "http://127.0.0.1:4096",
        "Follow all instructions in the attached prompt file and write the required result JSON.",
    ]

    # Cancel running OpenCode process.
    sleep_env = _base_windows_env({"PATH": str(bin_dir), "FAKE_OPENCODE_SLEEP": "1"})
    sleep_req = _make_request(tmp_path / "cancel_case", env=sleep_env)
    sleep_handle = adapter.start_window(sleep_req)
    time.sleep(0.25)
    cancel_res = adapter.cancel_window(sleep_handle, env=sleep_env)
    assert cancel_res.confirmed is True
    assert not is_pid_alive(sleep_handle.pid)


# ---------------------------------------------------------------------------
# 5. CustomAdapter tests (config precedence, placeholders, session regex, cancel)
# ---------------------------------------------------------------------------


def test_custom_config_discovery_precedence(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    ws_dir = tmp_path / "ws"
    env_cfg = tmp_path / "env_host.json"
    explicit_cfg = tmp_path / "explicit_host.json"

    for path, name in (
        (home_dir / ".super-agent" / "host.json", "home-agent"),
        (ws_dir / ".super-agent" / "host.json", "ws-agent"),
        (env_cfg, "env-agent"),
        (explicit_cfg, "explicit-agent"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"name": name, "command": ["echo", "{prompt_file}"]}),
            encoding="utf-8",
        )

    base_env = {"HOME": str(home_dir), "USERPROFILE": str(home_dir)}

    # 1. Explicit config_path wins over everything.
    cfg, chosen = load_custom_config(
        config_path=explicit_cfg,
        env={**base_env, "SUPER_AGENT_HOST_CONFIG": str(env_cfg)},
        workspace=ws_dir,
    )
    assert chosen == explicit_cfg
    assert cfg["name"] == "explicit-agent"

    # 2. SUPER_AGENT_HOST_CONFIG wins over workspace and home.
    cfg, chosen = load_custom_config(
        env={**base_env, "SUPER_AGENT_HOST_CONFIG": str(env_cfg)},
        workspace=ws_dir,
    )
    assert chosen == env_cfg
    assert cfg["name"] == "env-agent"

    # 3. <workspace>/.super-agent/host.json wins over ~/.super-agent/host.json.
    cfg, chosen = load_custom_config(env=base_env, workspace=ws_dir)
    assert chosen == ws_dir / ".super-agent" / "host.json"
    assert cfg["name"] == "ws-agent"

    # 4. ~/.super-agent/host.json used when workspace has no config.
    cfg, chosen = load_custom_config(env=base_env, workspace=tmp_path / "empty_ws")
    assert chosen == home_dir / ".super-agent" / "host.json"
    assert cfg["name"] == "home-agent"


def test_custom_adapter_lifecycle_placeholders_and_cancel(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fake_bin"
    capture_file = tmp_path / "custom_capture.json"
    fake_script = f"""
import json, os, sys, time
from pathlib import Path

if os.environ.get("FAKE_CUSTOM_SLEEP") == "1":
    time.sleep(30)
    sys.exit(0)

stdin_text = sys.stdin.read()
Path({str(capture_file)!r}).write_text(
    json.dumps({{
        "args": sys.argv[1:],
        "stdin": stdin_text,
        "custom_flag": os.environ.get("MY_CUSTOM_FLAG"),
    }}),
    encoding="utf-8",
)
print("booting my-agent...", flush=True)
print("SESSION_ID=cust-session-42", flush=True)
sys.exit(0)
"""
    _write_fake_cli(bin_dir, "my-agent", fake_script)

    cfg_path = tmp_path / "host.json"
    cfg_path.write_text(
        json.dumps(
            {
                "name": "my-agent",
                "command": [
                    "my-agent",
                    "--ws",
                    "{workspace}",
                    "--prompt",
                    "{prompt_file}",
                    "--result",
                    "{result_file}",
                    "--last",
                    "{last_message_file}",
                    "--id",
                    "{task_id}",
                    "--title",
                    "{title}",
                    "--role",
                    "{role}",
                    "--model",
                    "{model}",
                    "--json-literal",
                    '{"keep":"braces"}',
                ],
                "stdin_prompt": True,
                "env": {"MY_CUSTOM_FLAG": "enabled-123"},
                "session_id_regex": r"SESSION_ID=([A-Za-z0-9_-]+)",
                "resume_template": "my-agent --resume {session_id}",
            }
        ),
        encoding="utf-8",
    )

    env = _base_windows_env({"PATH": str(bin_dir)})
    adapter = CustomAdapter(config_path=cfg_path)

    caps = adapter.capabilities()
    assert caps.resumable is True
    assert caps.supports_model is True
    assert caps.supports_cancel is True

    health = adapter.health_check(env=env)
    assert health.available is True
    assert "my-agent" in health.detail

    req = _make_request(tmp_path, env=env, prompt_text="Custom stdin payload")
    handle = adapter.start_window(req)
    assert handle.host == "custom"

    poll = _poll_until_done(adapter, handle, env=env)
    assert poll.status == "completed"
    assert poll.exit_code == 0
    assert poll.cleanup_confirmed is True
    assert poll.session_id == "cust-session-42"
    assert poll.resume_hint == "my-agent --resume cust-session-42"

    captured = json.loads(capture_file.read_text(encoding="utf-8"))
    assert captured["stdin"] == "Custom stdin payload"
    assert captured["custom_flag"] == "enabled-123"
    assert captured["args"] == [
        "--ws",
        str(req.workspace),
        "--prompt",
        str(req.prompt_path),
        "--result",
        str(req.result_path),
        "--last",
        str(req.last_message_path),
        "--id",
        "task-1",
        "--title",
        "Build widget",
        "--role",
        "worker",
        "--model",
        "test-model-v1",
        "--json-literal",
        '{"keep":"braces"}',
    ]

    # Test cancel_window terminates running custom process.
    sleep_env = _base_windows_env({"PATH": str(bin_dir), "FAKE_CUSTOM_SLEEP": "1"})
    sleep_req = _make_request(tmp_path / "cancel_case", env=sleep_env)
    sleep_handle = adapter.start_window(sleep_req)
    time.sleep(0.25)
    cancel_res = adapter.cancel_window(sleep_handle, env=sleep_env)
    assert cancel_res.confirmed is True
    assert not is_pid_alive(sleep_handle.pid)


# ---------------------------------------------------------------------------
# 6. resolve_command_argv tests (Windows .cmd / .ps1 handling)
# ---------------------------------------------------------------------------


def test_resolve_command_argv_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(AdapterError):
        resolve_command_argv("definitely-nonexistent-binary-xyz", ["--version"], env={"PATH": str(tmp_path)})


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific .cmd/.ps1 resolution")
def test_resolve_command_argv_windows_cmd_and_ps1(tmp_path: Path) -> None:
    # 1. .cmd wrapped with cmd.exe /d /c
    cmd_tool = tmp_path / "sampletool.cmd"
    cmd_tool.write_text("@echo off\r\necho hi\r\n", encoding="utf-8")
    argv_cmd = resolve_command_argv("sampletool", ["--flag", "val"], env={"PATH": str(tmp_path)})
    assert Path(argv_cmd[0]).name.lower() == "cmd.exe"
    assert argv_cmd[1:3] == ["/d", "/c"]
    assert Path(argv_cmd[3]).resolve() == cmd_tool.resolve()
    assert argv_cmd[4:] == ["--flag", "val"]

    # 2. Standalone .ps1 wrapped with powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File
    ps1_only = tmp_path / "onlyps1.ps1"
    ps1_only.write_text("Write-Output 'ok'\n", encoding="utf-8")
    argv_ps1 = resolve_command_argv(str(ps1_only), ["--arg"], env={"PATH": os.environ.get("PATH", "")})
    assert "powershell" in Path(argv_ps1[0]).name.lower()
    assert "-NoProfile" in argv_ps1
    assert "-NonInteractive" in argv_ps1
    assert "-ExecutionPolicy" in argv_ps1
    assert "Bypass" in argv_ps1
    assert "-File" in argv_ps1
    assert Path(argv_ps1[argv_ps1.index("-File") + 1]).resolve() == ps1_only.resolve()
    assert argv_ps1[-1] == "--arg"

    # 3. .ps1 with sibling .cmd prefers sibling .cmd over powershell.exe
    paired_ps1 = tmp_path / "paired.ps1"
    paired_cmd = tmp_path / "paired.cmd"
    paired_ps1.write_text("Write-Output 'ps1'\n", encoding="utf-8")
    paired_cmd.write_text("@echo off\r\necho cmd\r\n", encoding="utf-8")
    argv_paired = resolve_command_argv(str(paired_ps1), ["--ok"], env={"PATH": str(tmp_path)})
    assert Path(argv_paired[0]).name.lower() == "cmd.exe"
    assert argv_paired[1:3] == ["/d", "/c"]
    assert Path(argv_paired[3]).resolve() == paired_cmd.resolve()
    assert argv_paired[4:] == ["--ok"]


# ---------------------------------------------------------------------------
# 7. detect_host precedence, get_adapter, and doctor() tests
# ---------------------------------------------------------------------------


def test_detect_host_precedence(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    # Explicit SUPER_AGENT_HOST wins over everything.
    assert (
        detect_host(
            env={
                "SUPER_AGENT_HOST": "opencode",
                "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:9999",
                "CODEX_THREAD_ID": "t-1",
                "CLAUDECODE": "1",
            },
            workspace=ws,
        )
        == "opencode"
    )

    # ANTIGRAVITY_LS_ADDRESS beats Codex/Claude/OpenCode env vars.
    assert (
        detect_host(
            env={
                "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:9999",
                "CODEX_THREAD_ID": "t-1",
                "CLAUDECODE": "1",
                "OPENCODE": "1",
            },
            workspace=ws,
        )
        == "antigravity"
    )

    # Codex env vars beat Claude and OpenCode env vars.
    for codex_var in ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_HOME"):
        assert (
            detect_host(
                env={codex_var: "1", "CLAUDECODE": "1", "OPENCODE": "1"},
                workspace=ws,
            )
            == "codex"
        )

    # Claude env vars beat OpenCode env vars.
    for claude_var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        assert (
            detect_host(
                env={claude_var: "1", "OPENCODE": "1"},
                workspace=ws,
            )
            == "claude"
        )

    # OpenCode env vars beat PATH lookup.
    for oc_var in ("OPENCODE", "OPENCODE_SERVER_URL"):
        assert detect_host(env={oc_var: "1"}, workspace=ws) == "opencode"

    # PATH CLI discovery precedence: codex > claude > opencode.
    bin_oc = tmp_path / "bin_oc"
    _write_fake_cli(bin_oc, "opencode", "import sys; sys.exit(0)\n")
    assert detect_host(env={"PATH": str(bin_oc)}, workspace=ws) == "opencode"

    bin_cl = tmp_path / "bin_cl"
    _write_fake_cli(bin_cl, "claude", "import sys; sys.exit(0)\n")
    _write_fake_cli(bin_cl, "opencode", "import sys; sys.exit(0)\n")
    assert detect_host(env={"PATH": str(bin_cl)}, workspace=ws) == "claude"

    bin_cx = tmp_path / "bin_cx"
    _write_fake_cli(bin_cx, "codex", "import sys; sys.exit(0)\n")
    _write_fake_cli(bin_cx, "claude", "import sys; sys.exit(0)\n")
    assert detect_host(env={"PATH": str(bin_cx)}, workspace=ws) == "codex"

    # Custom host.json discovered when no env/CLI matches.
    ws_cfg = ws / ".super-agent" / "host.json"
    ws_cfg.parent.mkdir(parents=True, exist_ok=True)
    ws_cfg.write_text(json.dumps({"name": "custom-cli", "command": ["echo", "hi"]}), encoding="utf-8")
    assert find_custom_config_path(env={}, workspace=ws) == ws_cfg
    assert detect_host(env={}, workspace=ws) == "custom"

    # Final fallback is "codex" when nothing matches.
    assert detect_host(env={}, workspace=tmp_path / "no_config_ws") == "codex"


def test_get_adapter_and_doctor(tmp_path: Path) -> None:
    assert BUILTIN_HOSTS == ("antigravity", "codex", "claude", "opencode", "custom")
    assert isinstance(get_adapter("antigravity"), AntigravityAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)
    assert isinstance(get_adapter("claude"), ClaudeAdapter)
    assert isinstance(get_adapter("opencode"), OpenCodeAdapter)
    assert isinstance(get_adapter("custom", workspace=tmp_path), CustomAdapter)

    with pytest.raises(AdapterError):
        get_adapter("unsupported-host")

    bin_dir = tmp_path / "doctor_bin"
    _write_fake_cli(bin_dir, "claude", "import sys; print('claude 2.0.1'); sys.exit(0)\n")
    secret_csrf = "doctor-csrf-secret-xyz"
    env = _base_windows_env(
        {
            "PATH": str(bin_dir),
            "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:43210",
            "ANTIGRAVITY_CSRF_TOKEN": secret_csrf,
        }
    )

    report = doctor(env=env, workspace=tmp_path)
    # Must be JSON-serializable and must not leak CSRF token.
    serialized = json.dumps(report)
    assert secret_csrf not in serialized

    assert report["detected_host"] == "antigravity"
    assert report["recommended_host"] == "antigravity"
    assert set(report["hosts"].keys()) == set(BUILTIN_HOSTS)
    assert report["hosts"]["antigravity"]["available"] is True
    assert report["hosts"]["claude"]["available"] is True
    assert report["hosts"]["claude"]["health"]["version"] == "claude 2.0.1"
    assert report["hosts"]["codex"]["available"] is False
    assert report["hosts"]["opencode"]["available"] is False
    assert report["hosts"]["custom"]["available"] is False

    # Verify custom host capabilities and availability via SUPER_AGENT_HOST_CONFIG in doctor().
    _write_fake_cli(bin_dir, "my-custom", "import sys; sys.exit(0)\n")
    custom_cfg = tmp_path / "custom_doctor.json"
    custom_cfg.write_text(
        json.dumps(
            {
                "name": "my-custom",
                "command": ["my-custom", "--model", "{model}", "--prompt", "{prompt_file}"],
                "session_id_regex": r"id=(\S+)",
                "resume_template": "my-custom --resume {session_id}",
            }
        ),
        encoding="utf-8",
    )
    custom_env = _base_windows_env(
        {
            "PATH": str(bin_dir),
            "SUPER_AGENT_HOST_CONFIG": str(custom_cfg),
        }
    )
    custom_report = doctor(env=custom_env, workspace=tmp_path)
    assert custom_report["hosts"]["custom"]["available"] is True
    assert custom_report["hosts"]["custom"]["capabilities"]["resumable"] is True
    assert custom_report["hosts"]["custom"]["capabilities"]["supports_model"] is True

    # Invalid custom host config reports available=False cleanly.
    bad_cfg = tmp_path / "bad_host.json"
    bad_cfg.write_text(json.dumps({"name": "broken", "command": []}), encoding="utf-8")
    bad_health = CustomAdapter(config_path=bad_cfg).health_check(env=custom_env)
    assert bad_health.available is False
    assert "command" in bad_health.detail
