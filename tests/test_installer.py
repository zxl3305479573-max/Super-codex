"""Tests for multi-host plugin manifests, $super-agent skill, and installer/uninstaller."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from super_agent import __version__
from super_agent.installer import (
    InstallerError,
    SUPPORTED_HOSTS,
    SUPPORTED_SCOPES,
    install_plugin,
    resolve_install_layout,
    uninstall_plugin,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _create_link_or_junction(link_path: Path, target_dir: Path) -> None:
    """Create a symlink, falling back to an unprivileged Windows junction if needed."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target_dir, link_path, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=True,
            capture_output=True,
        )


def _remove_link_or_junction(link_path: Path) -> None:
    if not link_path.exists() and not link_path.is_symlink():
        return
    try:
        link_path.unlink()
    except OSError:
        os.rmdir(link_path)


# ---------------------------------------------------------------------------
# Manifest & Skill schema tests
# ---------------------------------------------------------------------------


def test_antigravity_root_plugin_manifest_schema() -> None:
    manifest_path = REPO_ROOT / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    allowed_keys = {"name", "displayName", "version", "description", "suggestedPrompts"}
    assert set(data.keys()) == allowed_keys
    assert data["name"] == "super-agent"
    assert isinstance(data["displayName"], str) and data["displayName"].strip()
    assert data["version"] == __version__ == "0.2.0"
    assert isinstance(data["description"], str) and data["description"].strip()
    assert isinstance(data["suggestedPrompts"], list)
    assert len(data["suggestedPrompts"]) == 3
    assert all(isinstance(prompt, str) and prompt.strip() for prompt in data["suggestedPrompts"])


def test_mcp_configs_schema() -> None:
    antigravity_mcp = json.loads((REPO_ROOT / "mcp_config.json").read_text(encoding="utf-8"))
    assert antigravity_mcp == {
        "mcpServers": {
            "super-agent": {
                "command": "python",
                "args": ["-m", "super_agent.mcp_server"],
                "env": {
                    "SUPER_AGENT_HOST": "antigravity",
                },
            }
        }
    }

    generic_mcp = json.loads((REPO_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert generic_mcp == {
        "mcpServers": {
            "super-agent": {
                "command": "python",
                "args": ["-m", "super_agent.mcp_server"],
            }
        }
    }


def test_codex_plugin_manifest_schema_and_paths() -> None:
    manifest = json.loads(
        (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "super-agent"
    assert manifest["version"] == "0.2.0"
    assert isinstance(manifest["description"], str) and manifest["description"].strip()
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"

    assert (REPO_ROOT / manifest["skills"]).is_dir()
    assert (REPO_ROOT / manifest["skills"] / "super-agent" / "SKILL.md").is_file()
    assert (REPO_ROOT / manifest["mcpServers"]).is_file()

    interface = manifest["interface"]
    expected_interface_keys = {
        "displayName",
        "shortDescription",
        "longDescription",
        "category",
        "capabilities",
        "defaultPrompt",
    }
    assert expected_interface_keys.issubset(interface.keys())
    assert isinstance(interface["capabilities"], list) and len(interface["capabilities"]) >= 1
    assert all(isinstance(cap, str) and cap.strip() for cap in interface["capabilities"])
    assert isinstance(interface["defaultPrompt"], str) and "$super-agent" in interface["defaultPrompt"]


def test_claude_plugin_manifest_schema_and_paths() -> None:
    manifest = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "super-agent"
    assert isinstance(manifest["displayName"], str) and manifest["displayName"].strip()
    assert manifest["version"] == "0.2.0"
    assert isinstance(manifest["description"], str) and manifest["description"].strip()
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"

    assert (REPO_ROOT / manifest["skills"]).is_dir()
    assert (REPO_ROOT / manifest["mcpServers"]).is_file()


def test_skill_files_and_openai_yaml() -> None:
    openai_yaml = (
        REPO_ROOT / "skills" / "super-agent" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    lines = [line.strip() for line in openai_yaml.splitlines() if line.strip()]
    assert lines == ["policy:", "allow_implicit_invocation: false"]

    skill_md = (REPO_ROOT / "skills" / "super-agent" / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\n")
    assert "name: super-agent" in skill_md
    assert "description:" in skill_md

    for required_token in (
        "SUPER_AGENT_HOST",
        "super_agent_doctor",
        "super_agent_plan_create",
        "super_agent_plan_approve",
        "super_agent_dispatch_ready",
        "super_agent_wait_step",
        "super_agent_integrate_run",
        "super_agent_summarize_run",
        "super_agent_apply_integration",
        "super_agent_retry_task",
        "super_agent_cancel_run",
        "shell_fallback_recommended",
        "ANTIGRAVITY_LS_ADDRESS",
        "ANTIGRAVITY_CSRF_TOKEN",
        "python -m super_agent dispatch --run-id",
        "python -m super_agent wait --run-id",
        "final.patch",
    ):
        assert required_token in skill_md, f"Missing {required_token!r} in SKILL.md"


# ---------------------------------------------------------------------------
# Layout resolution tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "scope", "expected_plugin_rel", "expected_skill_rel"),
    [
        ("antigravity", "user", ".gemini/config/plugins/super-agent", None),
        ("antigravity", "project", ".agents/plugins/super-agent", None),
        ("codex", "user", ".codex/plugins/super-agent", ".agents/skills/super-agent"),
        ("codex", "project", ".agents/plugins/super-agent", ".agents/skills/super-agent"),
        ("claude", "user", ".claude/plugins/super-agent", None),
        ("claude", "project", ".claude/plugins/super-agent", None),
        ("opencode", "user", ".config/opencode/plugins/super-agent", ".agents/skills/super-agent"),
        ("opencode", "project", ".opencode/plugins/super-agent", ".agents/skills/super-agent"),
        ("custom", "user", ".super-agent/plugin", ".agents/skills/super-agent"),
        ("custom", "project", ".super-agent/plugin", ".agents/skills/super-agent"),
    ],
)
def test_resolve_install_layout_exact_paths(
    tmp_path: Path,
    host: str,
    scope: str,
    expected_plugin_rel: str,
    expected_skill_rel: str | None,
) -> None:
    project_dir = tmp_path / "proj"
    home_dir = tmp_path / "home"
    base = home_dir if scope == "user" else project_dir

    layout = resolve_install_layout(
        host, scope=scope, target_root=project_dir, home_dir=home_dir
    )
    assert layout["plugin_dir"] == base / Path(expected_plugin_rel)
    if expected_skill_rel is None:
        assert "skill_dir" not in layout
    else:
        assert layout["skill_dir"] == base / Path(expected_skill_rel)


def test_resolve_install_layout_validation() -> None:
    with pytest.raises(InstallerError, match="Unsupported host"):
        resolve_install_layout("unknown-host")
    with pytest.raises(InstallerError, match="Unsupported scope"):
        resolve_install_layout("codex", scope="global")


# ---------------------------------------------------------------------------
# Install / Repeat / Upgrade / Uninstall lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", SUPPORTED_HOSTS)
@pytest.mark.parametrize("scope", SUPPORTED_SCOPES)
def test_install_upgrade_and_uninstall_lifecycle_all_hosts(
    tmp_path: Path, host: str, scope: str
) -> None:
    project_dir = tmp_path / "project"
    home_dir = tmp_path / "home"
    project_dir.mkdir()
    home_dir.mkdir()

    # Seed simulated run records and unrelated sibling files to verify preservation.
    user_run_record = home_dir / ".super-agent" / "runs" / "run-001" / "state.json"
    user_run_record.parent.mkdir(parents=True, exist_ok=True)
    user_run_record.write_text('{"run_id": "run-001"}\n', encoding="utf-8")

    project_run_record = project_dir / ".super-agent" / "runs" / "run-002" / "state.json"
    project_run_record.parent.mkdir(parents=True, exist_ok=True)
    project_run_record.write_text('{"run_id": "run-002"}\n', encoding="utf-8")

    layout = resolve_install_layout(
        host, scope=scope, target_root=project_dir, home_dir=home_dir
    )
    sibling_plugin = layout["plugin_dir"].parent / "other-plugin" / "marker.txt"
    sibling_plugin.parent.mkdir(parents=True, exist_ok=True)
    sibling_plugin.write_text("keep-me", encoding="utf-8")

    sibling_skill: Path | None = None
    if "skill_dir" in layout:
        sibling_skill = layout["skill_dir"].parent / "other-skill" / "SKILL.md"
        sibling_skill.parent.mkdir(parents=True, exist_ok=True)
        sibling_skill.write_text("keep-skill", encoding="utf-8")

    custom_python = str(Path(sys.executable))
    custom_cfg = tmp_path / "custom-adapter.json" if host == "custom" else None

    # 1. Fresh install
    res1 = install_plugin(
        host,
        scope=scope,
        target_root=project_dir,
        source_root=REPO_ROOT,
        python_executable=custom_python,
        home_dir=home_dir,
        custom_host_config=custom_cfg,
    )
    # Result must be cleanly JSON-serializable.
    json.dumps(res1)

    assert res1["host"] == host
    assert res1["scope"] == scope
    assert res1["upgraded"] is False
    assert res1["version"] == __version__
    assert isinstance(res1["next_steps"], list) and len(res1["next_steps"]) >= 2

    plugin_dir = Path(res1["plugin_dir"])
    assert plugin_dir == layout["plugin_dir"]
    assert (plugin_dir / "super_agent" / "__init__.py").is_file()
    assert (plugin_dir / "super_agent" / "installer.py").is_file()
    assert (plugin_dir / "skills" / "super-agent" / "SKILL.md").is_file()
    assert (plugin_dir / "skills" / "super-agent" / "agents" / "openai.yaml").is_file()
    assert (plugin_dir / "plugin.json").is_file()
    assert (plugin_dir / ".codex-plugin" / "plugin.json").is_file()
    assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file()

    # Ensure no __pycache__ or .pyc artifacts were copied.
    assert not list(plugin_dir.rglob("__pycache__"))
    assert not list(plugin_dir.rglob("*.pyc"))

    # Verify rewritten MCP configs bind PYTHONPATH and SUPER_AGENT_HOST strictly.
    for mcp_name in ("mcp_config.json", ".mcp.json"):
        mcp_data = json.loads((plugin_dir / mcp_name).read_text(encoding="utf-8"))
        server = mcp_data["mcpServers"]["super-agent"]
        assert server["command"] == custom_python
        assert server["args"] == ["-m", "super_agent.mcp_server"]
        assert server["env"]["PYTHONPATH"] == str(plugin_dir.resolve())
        assert server["env"]["SUPER_AGENT_HOST"] == host
        if custom_cfg is not None:
            assert server["env"]["SUPER_AGENT_HOST_CONFIG"] == str(custom_cfg.resolve())
        else:
            assert "SUPER_AGENT_HOST_CONFIG" not in server["env"]

    expected_mcp_file = "mcp_config.json" if host == "antigravity" else ".mcp.json"
    assert Path(res1["mcp_config_path"]) == plugin_dir / expected_mcp_file

    # Verify install_meta.json
    meta = json.loads((plugin_dir / "install_meta.json").read_text(encoding="utf-8"))
    assert meta["host"] == host
    assert meta["scope"] == scope
    assert meta["version"] == __version__
    assert meta["python_executable"] == custom_python
    assert isinstance(meta["installed_at"], str) and meta["installed_at"].endswith("Z")

    # Verify separate skill_dir if applicable
    if "skill_dir" in layout:
        assert res1["skill_dir"] == str(layout["skill_dir"])
        assert (layout["skill_dir"] / "SKILL.md").is_file()
        assert (layout["skill_dir"] / "agents" / "openai.yaml").is_file()
    else:
        assert res1["skill_dir"] is None

    # 2. Repeat install / simulated upgrade from older version with stale files
    stale_file = plugin_dir / "stale_old_artifact.txt"
    stale_file.write_text("from-0.1.0", encoding="utf-8")
    (plugin_dir / "install_meta.json").write_text(
        json.dumps({"host": host, "scope": scope, "version": "0.1.0"}),
        encoding="utf-8",
    )

    res2 = install_plugin(
        host,
        scope=scope,
        target_root=project_dir,
        source_root=REPO_ROOT,
        python_executable=custom_python,
        home_dir=home_dir,
        custom_host_config=custom_cfg,
    )
    assert res2["upgraded"] is True
    assert not stale_file.exists()
    upgraded_meta = json.loads((plugin_dir / "install_meta.json").read_text(encoding="utf-8"))
    assert upgraded_meta["version"] == __version__

    # Verify siblings and run records survived install + upgrade
    assert sibling_plugin.read_text(encoding="utf-8") == "keep-me"
    if sibling_skill is not None:
        assert sibling_skill.read_text(encoding="utf-8") == "keep-skill"
    assert user_run_record.read_text(encoding="utf-8") == '{"run_id": "run-001"}\n'
    assert project_run_record.read_text(encoding="utf-8") == '{"run_id": "run-002"}\n'

    # 3. Uninstall
    un1 = uninstall_plugin(
        host, scope=scope, target_root=project_dir, home_dir=home_dir
    )
    json.dumps(un1)
    assert un1["already_absent"] is False
    assert str(plugin_dir) in un1["removed_paths"]
    assert not plugin_dir.exists()
    assert plugin_dir.parent.exists()
    if "skill_dir" in layout:
        assert str(layout["skill_dir"]) in un1["removed_paths"]
        assert not layout["skill_dir"].exists()
        assert layout["skill_dir"].parent.exists()

    # Siblings and run records MUST still be untouched after uninstall
    assert sibling_plugin.read_text(encoding="utf-8") == "keep-me"
    if sibling_skill is not None:
        assert sibling_skill.read_text(encoding="utf-8") == "keep-skill"
    assert user_run_record.read_text(encoding="utf-8") == '{"run_id": "run-001"}\n'
    assert project_run_record.read_text(encoding="utf-8") == '{"run_id": "run-002"}\n'

    # 4. Repeat uninstall is idempotent
    un2 = uninstall_plugin(
        host, scope=scope, target_root=project_dir, home_dir=home_dir
    )
    assert un2["already_absent"] is True
    assert un2["removed_paths"] == []


# ---------------------------------------------------------------------------
# Safety & edge-case tests
# ---------------------------------------------------------------------------


def test_rejects_symlink_or_junction_target_on_install_and_uninstall(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    outside_dir = tmp_path / "outside-victim"
    outside_dir.mkdir(parents=True)
    victim_file = outside_dir / "precious.txt"
    victim_file.write_text("do-not-delete", encoding="utf-8")

    layout = resolve_install_layout("codex", scope="project", target_root=project_dir)
    link_path = layout["plugin_dir"]
    _create_link_or_junction(link_path, outside_dir)
    try:
        with pytest.raises(InstallerError, match="symlink or reparse point"):
            install_plugin(
                "codex",
                scope="project",
                target_root=project_dir,
                source_root=REPO_ROOT,
            )

        with pytest.raises(InstallerError, match="symlink or reparse point"):
            uninstall_plugin("codex", scope="project", target_root=project_dir)

        assert victim_file.read_text(encoding="utf-8") == "do-not-delete"
    finally:
        _remove_link_or_junction(link_path)


def test_rejects_symlink_or_junction_on_skill_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    outside_dir = tmp_path / "outside-skill"
    outside_dir.mkdir(parents=True)
    victim_file = outside_dir / "keep.txt"
    victim_file.write_text("safe", encoding="utf-8")

    layout = resolve_install_layout("codex", scope="project", target_root=project_dir)
    _create_link_or_junction(layout["skill_dir"], outside_dir)
    try:
        with pytest.raises(InstallerError, match="symlink or reparse point"):
            install_plugin(
                "codex",
                scope="project",
                target_root=project_dir,
                source_root=REPO_ROOT,
            )
        # plugin_dir must not have been created because safety check runs first
        assert not layout["plugin_dir"].exists()
        assert victim_file.read_text(encoding="utf-8") == "safe"
    finally:
        _remove_link_or_junction(layout["skill_dir"])


def test_rejects_non_directory_destination_and_invalid_inputs(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    layout = resolve_install_layout("claude", scope="project", target_root=project_dir)
    layout["plugin_dir"].parent.mkdir(parents=True, exist_ok=True)
    layout["plugin_dir"].write_text("not-a-directory", encoding="utf-8")

    with pytest.raises(InstallerError, match="not a directory"):
        install_plugin(
            "claude",
            scope="project",
            target_root=project_dir,
            source_root=REPO_ROOT,
        )

    with pytest.raises(InstallerError, match="not a directory"):
        uninstall_plugin("claude", scope="project", target_root=project_dir)

    with pytest.raises(InstallerError, match="python_executable"):
        install_plugin(
            "antigravity",
            scope="project",
            target_root=tmp_path / "empty-py",
            source_root=REPO_ROOT,
            python_executable="   ",
        )

    with pytest.raises(InstallerError, match="Missing required plugin"):
        install_plugin(
            "antigravity",
            scope="project",
            target_root=tmp_path / "missing-src",
            source_root=tmp_path / "empty-source",
        )


def test_install_does_not_mutate_source_manifests(tmp_path: Path) -> None:
    before_antigravity = (REPO_ROOT / "mcp_config.json").read_text(encoding="utf-8")
    before_generic = (REPO_ROOT / ".mcp.json").read_text(encoding="utf-8")

    install_plugin(
        "antigravity",
        scope="project",
        target_root=tmp_path / "proj",
        source_root=REPO_ROOT,
        python_executable="C:/CustomPython/python.exe",
    )

    assert (REPO_ROOT / "mcp_config.json").read_text(encoding="utf-8") == before_antigravity
    assert (REPO_ROOT / ".mcp.json").read_text(encoding="utf-8") == before_generic
