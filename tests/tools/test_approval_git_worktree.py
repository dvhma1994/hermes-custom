"""Approval gating for destructive git working-tree commands.

git checkout/restore of the worktree and `git stash drop/clear` irreversibly
discard uncommitted work — same data-loss class as `git reset --hard`, which is
already gated. Branch switches and `restore --staged` must stay un-gated.
"""

import pytest

from tools.approval import detect_dangerous_command


@pytest.mark.parametrize("cmd", [
    "git checkout -- src/foo.py",
    "git checkout -- .",
    "git checkout .",
    "git checkout HEAD~1 -- src/foo.py",
    "git checkout HEAD .",            # review Issue 2
    "git checkout -f main",           # review Issue 2 (force discards)
    "git checkout --force",           # review Issue 2
    "git switch --discard-changes",   # review Issue 3
    "git restore foo.py",
    "git restore .",
    "git restore --worktree foo.py",
    "git restore --staged --worktree foo.py",  # review Issue 1 (discards worktree)
    "git stash drop",
    "git stash clear",
    "git stash drop stash@{2}",
])
def test_destructive_worktree_git_is_gated(cmd):
    is_dangerous, _key, desc = detect_dangerous_command(cmd)
    assert is_dangerous is True, f"{cmd!r} should require approval"
    assert desc


@pytest.mark.parametrize("cmd", [
    "git checkout -b feature",
    "git checkout main",
    "git checkout feature-branch",
    "git switch main",
    "git switch -c newbranch",
    "git restore --staged foo.py",
    "git stash",
    "git stash pop",
    "git stash list",
    "git status",
    "git diff -- src/foo.py",
])
def test_safe_git_forms_not_gated(cmd):
    is_dangerous, _key, _desc = detect_dangerous_command(cmd)
    assert is_dangerous is False, f"{cmd!r} should NOT require approval"
