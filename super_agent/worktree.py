"""Prepare, capture, and replay patches in detached Git worktrees."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
import tempfile


class WorktreeError(RuntimeError):
    """A worktree or patch operation failed."""


_GIT_ENV = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CONFIG",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX", "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE", "GIT_WORK_TREE",
}
_DIFF = (
    "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
    "--no-relative", "--ignore-submodules=none", "--submodule=short",
)


def _git(
    workspace: Path, *args: str, index: Path | None = None,
    data: bytes | None = None, allowed: tuple[int, ...] = (0,),
) -> bytes:
    env = {name: value for name, value in os.environ.items() if name not in _GIT_ENV}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false",
             "-C", str(workspace), *args],
            shell=False, env=env, input=data, capture_output=True, check=False,
        )
    except OSError as exc:
        raise WorktreeError(f"Cannot run Git in {workspace}: {exc}") from exc
    if result.returncode not in allowed:
        detail = (result.stderr + result.stdout).decode("utf-8", errors="replace").strip()
        raise WorktreeError(f"git {' '.join(args)} failed in {workspace}\n{detail}")
    return result.stdout


def _revision(workspace: Path, name: str, kind: str) -> str:
    return _git(workspace, "rev-parse", "--verify", "--end-of-options",
                f"{name}^{{{kind}}}").decode("ascii").strip()


def resolve_base(workspace: Path) -> str:
    """Return the committed HEAD of the source workspace."""
    return _revision(workspace, "HEAD", "commit")


def is_git_repo(workspace: Path) -> bool:
    """Return True if workspace is the root of a Git worktree with a resolvable HEAD commit."""
    if not workspace.exists() or not workspace.is_dir():
        return False
    try:
        resolved = workspace.resolve()
        if _git_path(resolved, "--show-toplevel") != resolved:
            return False
        resolve_base(resolved)
    except WorktreeError:
        return False
    return True


def _git_path(workspace: Path, option: str) -> Path:
    raw = _git(workspace, "rev-parse", "--path-format=absolute", option)
    return Path(os.fsdecode(raw.removesuffix(b"\n"))).resolve()


def _checked_out_paths(workspace: Path) -> list[Path]:
    listing = _git(workspace, "worktree", "list", "--porcelain", "-z")
    return [
        Path(os.fsdecode(field.removeprefix(b"worktree "))).resolve()
        for field in listing.split(b"\0") if field.startswith(b"worktree ")
    ]


def _detached_root(worktree: Path) -> Path:
    worktree = worktree.resolve()
    if (
        _git_path(worktree, "--show-toplevel") != worktree
        or _git_path(worktree, "--absolute-git-dir") == _git_path(worktree, "--git-common-dir")
        or _git(worktree, "rev-parse", "--symbolic-full-name", "HEAD") != b"HEAD\n"
    ):
        raise WorktreeError(f"Expected the root of a detached linked worktree: {worktree}")
    for checkout in _checked_out_paths(worktree):
        if checkout != worktree and worktree.is_relative_to(checkout):
            raise WorktreeError(f"Worktree {worktree} is inside checkout {checkout}")
    return worktree


def _output_path(worktree: Path, output: Path) -> Path:
    output = output.absolute()
    resolved = output.resolve()
    protected = [*_checked_out_paths(worktree), _git_path(worktree, "--git-common-dir")]
    if any(resolved.is_relative_to(path) or output.is_relative_to(path) for path in protected):
        raise WorktreeError(f"Patch output must be outside Git checkouts and metadata: {output}")
    if output.is_symlink():
        raise WorktreeError(f"Patch output cannot be a symlink: {output}")
    return resolved


def _publish(output: Path, content: bytes) -> None:
    temporary = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(output)
    except OSError as exc:
        raise WorktreeError(f"Cannot publish patch {output}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read(patch: Path) -> bytes:
    try:
        return patch.read_bytes()
    except OSError as exc:
        raise WorktreeError(f"Cannot read patch {patch}: {exc}") from exc


def _apply(worktree: Path, patches: list[Path], index: Path | None = None) -> None:
    for patch in patches:
        try:
            _git(
                worktree, "-c", "apply.ignoreWhitespace=no", "apply",
                "--cached" if index is not None else "--index", "--3way",
                "--whitespace=nowarn", "--allow-empty", "-",
                index=index, data=_read(patch),
            )
        except WorktreeError as exc:
            raise WorktreeError(f"Cannot apply {patch} in {worktree}\n{exc}") from exc


def _tree(worktree: Path, index: Path | None = None) -> str:
    return _git(worktree, "write-tree", index=index).decode("ascii").strip()


def prepare(
    workspace: Path, run_dir: Path, attempt_id: str, base: str, patches: list[Path],
) -> tuple[Path, str]:
    """Create a fresh attempt and return the path and prerequisite tree."""
    if (
        attempt_id in ("", ".", "..") or any(char in attempt_id for char in "/\\:\0")
        or (os.name == "nt" and attempt_id.endswith((".", " ")))
    ):
        raise WorktreeError(f"Attempt ID must be a single directory name: {attempt_id!r}")
    workspace = workspace.resolve()
    run_dir = run_dir.resolve()
    worktree = run_dir / attempt_id
    protected = [*_checked_out_paths(workspace), _git_path(workspace, "--git-common-dir")]
    if any(
        (worktree.is_relative_to(path) or path.is_relative_to(worktree))
        and not (worktree == path and path != workspace)
        for path in protected
    ):
        raise WorktreeError(f"Attempt worktree overlaps a Git checkout or metadata: {worktree}")
    if worktree.exists() or worktree.is_symlink():
        raise WorktreeError(f"Attempt path already exists: {worktree}")
    commit = _revision(workspace, base, "commit")
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(f"Cannot create run directory {run_dir}: {exc}") from exc
    _git(workspace, "worktree", "add", "--detach", "--", str(worktree), commit)
    _apply(worktree, patches)
    return worktree, _tree(worktree)


@contextmanager
def _scratch_index(worktree: Path, base: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=".orchestrator-index-", dir=worktree.parent) as temp:
        index = Path(temp) / "index"
        _git(worktree, "read-tree", base, index=index)
        yield index


def _paths(raw: bytes) -> set[bytes]:
    return {path for path in raw.split(b"\0") if path}


def _diff_paths(worktree: Path, *revisions: str) -> set[bytes]:
    return _paths(_git(worktree, "diff", *_DIFF, "--name-only", "-z", *revisions, "--"))


def _patch(worktree: Path, base: str, final: str) -> bytes:
    return _git(
        worktree, "diff", *_DIFF, "--binary", "--src-prefix=a/", "--dst-prefix=b/",
        "-O", os.devnull, base, final, "--",
    )


def _scope(worktree: Path, write_paths: list[str], changed: set[bytes]) -> None:
    setting = _git(worktree, "config", "--type=bool", "--get", "core.ignoreCase", allowed=(0, 1))
    ignore_case = setting == b"true\n" or (not setting and os.name == "nt")
    scopes = []
    for raw in write_paths:
        path = PurePosixPath(raw.replace("\\", "/"))
        if not raw or path.is_absolute() or PureWindowsPath(raw).drive or ".." in path.parts:
            raise WorktreeError(f"Write scope must be repository-relative: {raw!r}")
        scopes.append(tuple(part.casefold() if ignore_case else part for part in path.parts))
    escaped = []
    for raw in sorted(changed):
        path = os.fsdecode(raw)
        parts = tuple((path.casefold() if ignore_case else path).split("/"))
        if not any(parts[:len(scope)] == scope for scope in scopes):
            escaped.append(repr(path))
    if escaped:
        raise WorktreeError(f"Changes outside write scope: {', '.join(escaped)}")


def capture(worktree: Path, input_tree: str, write_paths: list[str], patch: Path) -> None:
    """Export the worker's final file contents relative to its prerequisite tree."""
    worktree = _detached_root(worktree)
    output = _output_path(worktree, patch)
    baseline = _revision(worktree, input_tree, "tree")
    if _git(worktree, "ls-files", "--unmerged", "-z"):
        raise WorktreeError(f"Cannot capture an unmerged index in {worktree}")
    staged = _diff_paths(worktree, "--cached", baseline)
    unstaged = _diff_paths(worktree)
    untracked = _paths(_git(worktree, "ls-files", "--others", "--exclude-standard", "-z"))
    _scope(worktree, write_paths, staged | unstaged | untracked)
    with _scratch_index(worktree, baseline) as index:
        _git(worktree, "-c", "core.ignoreCase=false", "add", "-A", "--", ".", index=index)
        tracked = _paths(_git(worktree, "ls-files", "--cached", "-z"))
        missing = tracked - _paths(_git(worktree, "ls-files", "--cached", "-z", index=index))
        if missing:
            ignored = _paths(_git(
                worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                index=index,
            ))
            if forced := missing & ignored:
                _git(
                    worktree, "--literal-pathspecs", "add", "--force", "--pathspec-from-file=-",
                    "--pathspec-file-nul", index=index, data=b"\0".join(sorted(forced)) + b"\0",
                )
        final = _tree(worktree, index)
    _scope(worktree, write_paths, _diff_paths(worktree, baseline, final))
    raw_changes = _git(worktree, "diff", *_DIFF, "--raw", "-z", baseline, final, "--")
    unstaged_raw = _git(worktree, "diff", *_DIFF, "--raw", "-z", "--")
    if any(b"160000" in entry.split(b" ")[:2]
           for raw in (raw_changes, unstaged_raw) for entry in raw.split(b"\0")[::2]):
        raise WorktreeError(f"Cannot capture changed submodules or embedded repositories in {worktree}")
    _publish(output, _patch(worktree, baseline, final))


def _staged_state(worktree: Path) -> str:
    if _diff_paths(worktree) or _git(
        worktree, "ls-files", "--others", "--exclude-standard", "-z",
    ):
        raise WorktreeError(f"Worktree has unstaged or untracked changes: {worktree}")
    return _tree(worktree)


def _replay_tree(worktree: Path, base: str, patches: list[Path]) -> str:
    with _scratch_index(worktree, base) as index:
        _apply(worktree, patches, index)
        return _tree(worktree, index)


def integrate(worktree: Path, patches: list[Path], destination: Path) -> None:
    """Apply prerequisite patches in order and publish a single patch against HEAD."""
    worktree = _detached_root(worktree)
    output = _output_path(worktree, destination)
    base = _revision(worktree, "HEAD", "tree")
    current = _staged_state(worktree)
    if current != base or output.exists():
        expected = _replay_tree(worktree, base, patches)
        if current != expected:
            raise WorktreeError(f"Worktree index does not match the integration result: {worktree}")
        content = _patch(worktree, base, expected)
        if output.exists():
            if _read(output) != content:
                raise WorktreeError(f"Existing patch differs from integration result: {output}")
            return
    else:
        _apply(worktree, patches)
        content = _patch(worktree, base, _staged_state(worktree))
    _publish(output, content)


def verify_integration(worktree: Path, patches: list[Path], destination: Path) -> None:
    """Independently replay inputs and verify the staged state and published patch."""
    worktree = _detached_root(worktree)
    output = _output_path(worktree, destination)
    base = _revision(worktree, "HEAD", "tree")
    current = _staged_state(worktree)
    expected = _replay_tree(worktree, base, patches)
    if current != expected:
        raise WorktreeError(f"Worktree index does not match the integration result: {worktree}")
    content = _read(output)
    if content != _patch(worktree, base, expected):
        raise WorktreeError(f"Patch differs from integration result: {output}")
    with _scratch_index(worktree, base) as index:
        _git(worktree, "apply", "--cached", "--whitespace=nowarn", "--allow-empty", "-",
             index=index, data=content)
        if _tree(worktree, index) != expected:
            raise WorktreeError(f"Published patch does not replay to the expected tree: {output}")


def apply_patch_to_workspace(workspace: Path, patch: Path, *, check_only: bool = False) -> None:
    """Safely check and optionally apply a consolidated patch to the primary workspace."""
    workspace = workspace.resolve()
    content = _read(patch)
    if not content:
        return
    _git(
        workspace, "-c", "apply.ignoreWhitespace=no", "apply",
        "--check", "--whitespace=nowarn", "--allow-empty", "-",
        data=content,
    )
    if check_only:
        return
    _git(
        workspace, "-c", "apply.ignoreWhitespace=no", "apply",
        "--whitespace=nowarn", "--allow-empty", "-",
        data=content,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, action in (("integrate", integrate), ("verify", verify_integration)):
        command = commands.add_parser(name, help=action.__doc__)
        command.add_argument("--workspace", required=True, type=Path, help="Detached linked worktree")
        command.add_argument("--output", required=True, type=Path, help="Consolidated patch path")
        command.add_argument("--patch", action="append", default=[], type=Path, help="Input patch in DAG order")
        command.set_defaults(action=action)
    options = parser.parse_args()
    if not all(path.is_absolute() for path in [options.workspace, options.output, *options.patch]):
        parser.error("--workspace, --output, and --patch require absolute paths")
    try:
        options.action(options.workspace, options.patch, options.output)
    except (WorktreeError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
