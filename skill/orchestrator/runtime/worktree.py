"""Backward-compatible shim exporting super_agent.worktree."""

from __future__ import annotations

from pathlib import Path
import sys

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from super_agent.worktree import (  # noqa: E402, F401
    WorktreeError,
    _main,
    apply_patch_to_workspace,
    capture,
    integrate,
    is_git_repo,
    prepare,
    resolve_base,
    verify_integration,
)

if __name__ == "__main__":
    sys.exit(_main())
