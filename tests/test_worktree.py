import os
from pathlib import Path
import subprocess
import sys

import pytest

from skill.orchestrator.runtime import worktree as runtime


def git(workspace, *args, data=None):
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], shell=False, input=data, capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    source = tmp_path / "source checkout"
    source.mkdir()
    git(source, "init", "--quiet")
    git(source, "config", "user.name", "Worktree Test")
    git(source, "config", "user.email", "worktree@example.invalid")
    git(source, "config", "core.autocrlf", "false")
    git(source, "config", "commit.gpgsign", "false")
    (source / "main.txt").write_bytes(b"original\n")
    (source / "outside.txt").write_bytes(b"outside\n")
    (source / "delete.bin").write_bytes(bytes(range(256)))
    (source / ".gitignore").write_bytes(b"*.ignored\n")
    git(source, "add", "--all")
    git(source, "commit", "--quiet", "-m", "Fixture baseline")
    return source, tmp_path / "managed run", runtime.resolve_base(source)


def attempt(repo, name, *patches):
    source, run_dir, base = repo
    return runtime.prepare(source, run_dir, name, base, list(patches))


def task_patch(repo, name, content, *prerequisites):
    worktree, input_tree = attempt(repo, name, *prerequisites)
    (worktree / "main.txt").write_bytes(content)
    patch = repo[1] / f"{name}.patch"
    runtime.capture(worktree, input_tree, ["main.txt"], patch)
    return patch


def cli(command, worktree, output, *patches):
    args = [sys.executable, runtime.__file__, command, "--workspace", str(worktree),
            "--output", str(output)]
    for patch in patches:
        args.extend(("--patch", str(patch)))
    return subprocess.run(args, shell=False, capture_output=True, check=False)


def test_prepare_uses_committed_base_and_preserves_source(repo, monkeypatch):
    source, run_dir, base = repo
    (source / "main.txt").write_bytes(b"staged source edit\n")
    git(source, "add", "main.txt")
    (source / "main.txt").write_bytes(b"unstaged source edit\n")
    (source / "local.txt").write_bytes(b"untracked source file\n")
    source_status = git(source, "status", "--porcelain=v1", "-z")
    refs = git(source, "for-each-ref")
    monkeypatch.setenv("GIT_INDEX_FILE", str(source / ".git" / "index"))
    worktree, input_tree = attempt(repo, "attempt-1")
    monkeypatch.delenv("GIT_INDEX_FILE")

    assert worktree == run_dir / "attempt-1"
    assert input_tree == git(source, "rev-parse", f"{base}^{{tree}}").decode().strip()
    assert git(worktree, "rev-parse", "--symbolic-full-name", "HEAD") == b"HEAD\n"
    assert (worktree / "main.txt").read_bytes() == b"original\n"
    assert not (worktree / "local.txt").exists()
    assert git(source, "status", "--porcelain=v1", "-z") == source_status
    assert git(source, "show", ":main.txt") == b"staged source edit\n"
    assert git(source, "for-each-ref") == refs


def test_capture_replays_staged_and_unstaged_contents_binary_add_and_delete(repo):
    worktree, input_tree = attempt(repo, "producer")
    (worktree / "main.txt").write_bytes(b"staged\n")
    git(worktree, "add", "main.txt")
    (worktree / "main.txt").write_bytes(b"final\n")
    (worktree / "delete.bin").unlink()
    payload = bytes(range(255, -1, -1))
    (worktree / "assets").mkdir()
    (worktree / "assets" / "new file.bin").write_bytes(payload)
    (worktree / "forced.ignored").write_bytes(b"force-added\n")
    git(worktree, "add", "-f", "forced.ignored")
    (worktree / "local.ignored").write_bytes(b"ignored\n")
    index_before = git(worktree, "ls-files", "--stage", "-z")
    patch = repo[1] / "producer.patch"

    runtime.capture(worktree, input_tree,
                    ["main.txt", "delete.bin", "assets\\", "forced.ignored"], patch)

    assert b"GIT binary patch" in patch.read_bytes()
    assert git(worktree, "ls-files", "--stage", "-z") == index_before
    replay, tree = attempt(repo, "replay", patch)
    assert tree != input_tree
    assert (replay / "main.txt").read_bytes() == b"final\n"
    assert (replay / "assets" / "new file.bin").read_bytes() == payload
    assert (replay / "forced.ignored").read_bytes() == b"force-added\n"
    assert not (replay / "delete.bin").exists()
    assert not (replay / "local.ignored").exists()


@pytest.mark.parametrize("change", ["staged", "unstaged", "untracked", "staged then reverted"])
def test_capture_rejects_escapes_and_preserves_existing_artifact(repo, change):
    worktree, input_tree = attempt(repo, "escaped")
    escaped = worktree / "outside.txt"
    if change == "untracked":
        escaped = worktree / "allowed-extra" / "unexpected.txt"
        escaped.parent.mkdir()
    escaped.write_bytes(b"changed\n")
    if change in ("staged", "staged then reverted"):
        git(worktree, "add", "outside.txt")
    if change == "staged then reverted":
        escaped.write_bytes(b"outside\n")
    patch = repo[1] / "capture.patch"
    patch.write_bytes(b"previous artifact")

    with pytest.raises(runtime.WorktreeError, match=escaped.name):
        runtime.capture(worktree, input_tree, ["allowed", "main.txt"], patch)

    assert patch.read_bytes() == b"previous artifact"
    assert escaped.exists()


def test_capture_uses_git_case_rules_for_scope(repo):
    source = repo[0]
    git(source, "config", "core.ignoreCase", "true")
    worktree, input_tree = attempt(repo, "case")
    (worktree / "Nested").mkdir()
    (worktree / "Nested" / "Thing.txt").write_bytes(b"case\n")
    patch = repo[1] / "case.patch"
    runtime.capture(worktree, input_tree, ["nested\\thing.txt"], patch)
    assert b"Nested/Thing.txt" in patch.read_bytes()

    git(source, "config", "core.ignoreCase", "false")
    with pytest.raises(runtime.WorktreeError, match="Nested/Thing.txt"):
        runtime.capture(worktree, input_tree, ["nested\\thing.txt"], patch)


@pytest.mark.skipif(os.name == "nt", reason="Windows treats backslash as a separator")
def test_capture_treats_literal_backslash_in_git_path_as_data(repo):
    worktree, input_tree = attempt(repo, "literal-backslash")
    (worktree / "allowed\\escaped.txt").write_bytes(b"outside\n")
    with pytest.raises(runtime.WorktreeError, match="outside write scope"):
        runtime.capture(worktree, input_tree, ["allowed"], repo[1] / "escaped.patch")


def test_cli_integration_and_verification_replay_in_order(repo):
    first = task_patch(repo, "first", b"first\n")
    second = task_patch(repo, "second", b"second\n", first)
    assert b"-first\n" in second.read_bytes()
    worktree, _ = attempt(repo, "integration")
    output = repo[1] / "final.patch"

    result = cli("integrate", worktree, output, first, second)
    assert result.returncode == 0, result.stderr.decode()
    assert (worktree / "main.txt").read_bytes() == b"second\n"
    assert b"-original\n+second\n" in output.read_bytes()
    result = cli("verify", worktree, output, first, second)
    assert result.returncode == 0, result.stderr.decode()

    replay, _ = attempt(repo, "final-replay")
    git(replay, "apply", "--index", "-", data=output.read_bytes())
    assert (replay / "main.txt").read_bytes() == b"second\n"

    output.write_bytes(first.read_bytes())
    result = cli("verify", worktree, output, first, second)
    assert result.returncode == 1
    assert b"Patch differs" in result.stderr


def test_integration_recovers_matching_state_and_keeps_other_state(repo):
    patch = task_patch(repo, "one", b"one\n")
    worktree, _ = attempt(repo, "integration")
    output = repo[1] / "integration.patch"
    git(worktree, "apply", "--index", "-", data=patch.read_bytes())

    runtime.integrate(worktree, [patch], output)
    mtime = output.stat().st_mtime_ns
    runtime.integrate(worktree, [patch], output)
    assert output.stat().st_mtime_ns == mtime
    assert output.read_bytes() == patch.read_bytes()

    output.write_bytes(b"different bytes")
    with pytest.raises(runtime.WorktreeError, match="Existing patch differs"):
        runtime.integrate(worktree, [patch], output)
    assert output.read_bytes() == b"different bytes"
    (worktree / "main.txt").write_bytes(b"user change\n")
    with pytest.raises(runtime.WorktreeError, match="unstaged"):
        runtime.integrate(worktree, [patch], output)
    assert (worktree / "main.txt").read_bytes() == b"user change\n"


@pytest.mark.parametrize("operation", ["prepare", "integrate"])
def test_conflict_preserves_worktree_and_index(repo, operation):
    first = task_patch(repo, "first", b"first\n")
    conflicting = task_patch(repo, "conflicting", b"conflicting\n")
    output = repo[1] / "conflict.patch"
    worktree = repo[1] / "conflict"

    with pytest.raises(runtime.WorktreeError, match=r"Cannot apply .*conflicting\.patch"):
        if operation == "prepare":
            attempt(repo, "conflict", first, conflicting)
        else:
            attempt(repo, "conflict")
            runtime.integrate(worktree, [first, conflicting], output)

    conflict_contents = (worktree / "main.txt").read_bytes()
    conflict_index = git(worktree, "ls-files", "--unmerged", "-z")
    assert b"<<<<<<<" in conflict_contents
    assert conflict_index
    with pytest.raises(runtime.WorktreeError):
        runtime.integrate(worktree, [first, conflicting], output)
    assert (worktree / "main.txt").read_bytes() == conflict_contents
    assert git(worktree, "ls-files", "--unmerged", "-z") == conflict_index
    assert not output.exists()
    assert git(repo[0], "status", "--porcelain") == b""


def test_empty_output_and_source_protection(repo):
    source, run_dir, base = repo
    with pytest.raises(runtime.WorktreeError, match="overlaps"):
        runtime.prepare(source, source / "managed", "attempt", base, [])
    assert not (source / "managed").exists()
    with pytest.raises(runtime.WorktreeError, match="single directory name"):
        runtime.prepare(source, run_dir, "..\\escape", base, [])
    with pytest.raises(runtime.WorktreeError, match="detached linked worktree"):
        runtime.integrate(source, [], run_dir / "source.patch")

    worktree, input_tree = attempt(repo, "empty")
    with pytest.raises(runtime.WorktreeError, match="already exists"):
        attempt(repo, "empty")
    with pytest.raises(runtime.WorktreeError, match="outside Git checkouts"):
        runtime.capture(worktree, input_tree, [], source / "main.txt")
    assert (source / "main.txt").read_bytes() == b"original\n"

    empty = run_dir / "empty.patch"
    runtime.capture(worktree, input_tree, [], empty)
    assert empty.read_bytes() == b""
    integration, _ = attempt(repo, "integration")
    output = run_dir / "final.patch"
    runtime.integrate(integration, [empty], output)
    runtime.verify_integration(integration, [empty], output)
    assert output.read_bytes() == b""
    assert git(source, "status", "--porcelain") == b""


def test_cli_requires_absolute_paths(repo):
    result = cli("integrate", repo[0], Path("relative.patch"))
    assert result.returncode == 2
    assert b"require absolute paths" in result.stderr


def test_is_git_repo_and_apply_patch_to_workspace(repo, tmp_path):
    source, run_dir, _ = repo
    assert runtime.is_git_repo(source) is True
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()
    assert runtime.is_git_repo(non_git) is False
    empty_git = tmp_path / "unborn_repo"
    empty_git.mkdir()
    git(empty_git, "init", "--quiet")
    assert runtime.is_git_repo(empty_git) is False

    patch = task_patch(repo, "apply-demo", b"applied\n")
    runtime.apply_patch_to_workspace(source, patch, check_only=True)
    assert (source / "main.txt").read_bytes() == b"original\n"

    runtime.apply_patch_to_workspace(source, patch, check_only=False)
    assert (source / "main.txt").read_bytes() == b"applied\n"

    # Re-applying the same patch fails cleanly without corrupting workspace
    with pytest.raises(runtime.WorktreeError):
        runtime.apply_patch_to_workspace(source, patch, check_only=False)
    assert (source / "main.txt").read_bytes() == b"applied\n"

