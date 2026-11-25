"""Antigravity single-host session adapter using Connect HTTP ConversationService."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from super_agent.adapters.base import (
    AdapterCapabilities,
    AdapterError,
    AdapterHealth,
    CancelResult,
    HostSessionAdapter,
    WindowPollResult,
    WindowStartRequest,
)
from super_agent.models import SessionHandle, utc_now_iso

_SERVICE_PATH = "product.v1.ConversationService"
_CANCEL_TAG = "super-agent:cancel-requested"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _effective_env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _env_value(env: Mapping[str, str], key: str) -> str:
    val = env.get(key)
    if val is None:
        return ""
    return str(val).strip()


def _redact(text: str, secret: str | None) -> str:
    if secret:
        return text.replace(secret, "[REDACTED]")
    return text


def _build_rpc_url(address: str, method: str) -> str:
    normalized = address.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        normalized = f"http://{normalized}"
    return f"{normalized}/{_SERVICE_PATH}/{method}"


class AntigravityAdapter(HostSessionAdapter):
    """Creates independent Antigravity UI conversation windows via ConversationService."""

    host_name = "antigravity"

    def __init__(self, *, timeout_sec: float = 15.0) -> None:
        self.timeout_sec = timeout_sec

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            ui_native_window=True,
            resumable=True,
            supports_model=True,
            supports_cancel=False,
            transport="connect-http",
        )

    def _credentials(self, env: Mapping[str, str] | None) -> tuple[str, str, str]:
        eff = _effective_env(env)
        address = _env_value(eff, "ANTIGRAVITY_LS_ADDRESS")
        csrf_token = _env_value(eff, "ANTIGRAVITY_CSRF_TOKEN")
        project_id = _env_value(eff, "ANTIGRAVITY_PROJECT_ID")
        return address, csrf_token, project_id

    def health_check(self, env: Mapping[str, str] | None = None) -> AdapterHealth:
        address, csrf_token, _ = self._credentials(env)
        missing: list[str] = []
        if not address:
            missing.append("ANTIGRAVITY_LS_ADDRESS")
        if not csrf_token:
            missing.append("ANTIGRAVITY_CSRF_TOKEN")
        if missing:
            return AdapterHealth(
                host=self.host_name,
                available=False,
                detail=(
                    f"Missing shell-scoped Antigravity environment variable(s): {', '.join(missing)}. "
                    "Antigravity ConversationService credentials are injected only into the "
                    "Antigravity shell session; run inside Antigravity or use a CLI host fallback."
                ),
                executable=None,
                shell_fallback_recommended=True,
            )
        return AdapterHealth(
            host=self.host_name,
            available=True,
            detail=f"Antigravity ConversationService configured at {address}",
            executable=None,
            shell_fallback_recommended=False,
        )

    def _rpc(
        self,
        method: str,
        payload: dict[str, Any],
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        address, csrf_token, _ = self._credentials(env)
        if not address or not csrf_token:
            raise AdapterError(
                "Antigravity ConversationService requires shell-scoped "
                "ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN environment variables."
            )

        url = _build_rpc_url(address, method)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": csrf_token,
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        parsed = urllib.parse.urlsplit(url)
        try:
            if parsed.hostname in _LOOPBACK_HOSTS:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                cm = opener.open(req, timeout=self.timeout_sec)
            else:
                cm = urllib.request.urlopen(req, timeout=self.timeout_sec)
            with cm as response:
                raw_bytes = response.read()
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001
                err_body = ""
            detail = _redact(err_body or str(exc.reason), csrf_token)
            raise AdapterError(
                f"Antigravity {method} failed with HTTP {exc.code}: {detail}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = _redact(str(exc), csrf_token)
            raise AdapterError(f"Antigravity {method} request failed: {detail}") from None

        raw_text = raw_bytes.decode("utf-8", errors="replace").strip()
        if not raw_text:
            return {}
        try:
            decoded = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            detail = _redact(str(exc), csrf_token)
            raise AdapterError(f"Antigravity {method} returned invalid JSON: {detail}") from None
        if not isinstance(decoded, dict):
            raise AdapterError(f"Antigravity {method} returned non-object JSON payload")
        return decoded

    def _append_log(self, log_path: Path, event: dict[str, Any], secret: str | None = None) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            line = _redact(json.dumps(event, ensure_ascii=False), secret)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def start_window(self, request: WindowStartRequest) -> SessionHandle:
        _, csrf_token, project_id = self._credentials(request.env)
        title = f"[Super-Agent] {request.task.title}"
        tags = ["super-agent", request.run_id, request.task.id]
        payload: dict[str, Any] = {
            "title": title,
            "tags": tags,
            "prompt": [{"text": request.prompt_text}],
        }
        if project_id:
            payload["projectId"] = project_id
        if request.task.model:
            payload["model"] = request.task.model

        response = self._rpc("CreateConversation", payload, env=request.env)
        conversation = response.get("conversation")
        conv_id: str | None = None
        if isinstance(conversation, dict) and conversation.get("id"):
            conv_id = str(conversation["id"])
        elif response.get("conversationId"):
            conv_id = str(response["conversationId"])
        elif response.get("id"):
            conv_id = str(response["id"])

        if not conv_id:
            raise AdapterError("Antigravity CreateConversation response missing conversation.id")

        self._append_log(
            request.log_path,
            {
                "ts": utc_now_iso(),
                "event": "CreateConversation",
                "conversation_id": conv_id,
                "title": title,
                "tags": tags,
                "run_id": request.run_id,
                "task_id": request.task.id,
            },
            secret=csrf_token,
        )

        return SessionHandle(
            host=self.host_name,
            session_id=conv_id,
            title=title,
            status="running",
            pid=None,
            resume_hint=f"conversation://{conv_id}",
            log_path=str(request.log_path),
            metadata={
                "run_id": request.run_id,
                "task_id": request.task.id,
            },
        )

    def poll_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> WindowPollResult:
        if not handle.session_id:
            raise AdapterError("Antigravity SessionHandle is missing session_id")

        response = self._rpc(
            "GetConversation",
            {"conversationId": handle.session_id},
            env=env,
        )
        state = response.get("state")
        if not isinstance(state, dict):
            conv = response.get("conversation")
            state = conv.get("state") if isinstance(conv, dict) else {}
        if not isinstance(state, dict):
            state = {}

        tags: list[str] | None = None
        conv_obj = response.get("conversation")
        if isinstance(conv_obj, dict) and isinstance(conv_obj.get("tags"), list):
            tags = [str(t) for t in conv_obj["tags"]]
        elif isinstance(response.get("tags"), list):
            tags = [str(t) for t in response["tags"]]

        run_state = state.get("runState")
        resume_hint = handle.resume_hint or f"conversation://{handle.session_id}"
        meta: dict[str, Any] = {"run_state": run_state}
        if tags is not None:
            meta["tags"] = tags

        if run_state in ("RUN_STATE_RUNNING", "RUN_STATE_CANCELLING"):
            return WindowPollResult(
                status="running",
                session_id=handle.session_id,
                resume_hint=resume_hint,
                cleanup_confirmed=False,
                metadata=meta,
            )

        if run_state == "RUN_STATE_IDLE":
            raw_summary = state.get("summary")
            summary = str(raw_summary) if raw_summary is not None else None
            return WindowPollResult(
                status="completed",
                session_id=handle.session_id,
                resume_hint=resume_hint,
                summary=summary,
                cleanup_confirmed=True,
                metadata=meta,
            )

        if isinstance(run_state, str) and ("FAIL" in run_state or "ERROR" in run_state):
            err = state.get("error")
            return WindowPollResult(
                status="failed",
                session_id=handle.session_id,
                resume_hint=resume_hint,
                error=str(err) if err else f"Antigravity conversation entered {run_state}",
                cleanup_confirmed=True,
                metadata=meta,
            )

        if run_state == "RUN_STATE_CANCELLED":
            return WindowPollResult(
                status="cancelled",
                session_id=handle.session_id,
                resume_hint=resume_hint,
                cleanup_confirmed=True,
                metadata=meta,
            )

        return WindowPollResult(
            status="unknown",
            session_id=handle.session_id,
            resume_hint=resume_hint,
            cleanup_confirmed=False,
            metadata=meta,
        )

    def cancel_window(
        self, handle: SessionHandle, env: Mapping[str, str] | None = None,
    ) -> CancelResult:
        poll = self.poll_window(handle, env=env)
        if poll.status == "completed":
            return CancelResult(confirmed=True, detail="Conversation already idle")

        existing_tags = poll.metadata.get("tags")
        if isinstance(existing_tags, list):
            tags = [str(t) for t in existing_tags]
        else:
            tags = ["super-agent"]
            for key in ("run_id", "task_id"):
                val = handle.metadata.get(key)
                if val:
                    tags.append(str(val))
        if _CANCEL_TAG not in tags:
            tags.append(_CANCEL_TAG)

        try:
            self._rpc(
                "UpdateConversation",
                {
                    "conversationId": handle.session_id,
                    "tags": tags,
                },
                env=env,
            )
        except Exception:  # noqa: BLE001
            pass

        return CancelResult(
            confirmed=False,
            detail="ConversationService has no force-stop RPC; lock retained until RUN_STATE_IDLE",
        )
