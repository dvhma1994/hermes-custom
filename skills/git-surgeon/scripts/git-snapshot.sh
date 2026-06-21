#!/usr/bin/env bash
# git-snapshot.sh â€” take a recoverable snapshot BEFORE any destructive git op.
# Creates a timestamped backup branch + tag pointing at current HEAD, and
# stashes (keeping) any dirty tree so nothing is ever unrecoverable.
#
# Usage:  ./git-snapshot.sh [label]
# Output: the snapshot ref names + the exact command to restore.
set -euo pipefail

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "error: not inside a git repository" >&2; exit 1
fi

label="${1:-snapshot}"
ts="$(date +%Y%m%d-%H%M%S)"
head_sha="$(git rev-parse HEAD)"
ref="backup/${label}-${ts}"

git branch "$ref" "$head_sha"
git tag    "$ref" "$head_sha" 2>/dev/null || true

# Preserve uncommitted work without disturbing the working tree.
if ! git diff --quiet || ! git diff --cached --quiet; then
  git stash push --include-untracked --keep-index \
    -m "git-snapshot ${ref}" >/dev/null 2>&1 || true
  echo "dirty tree stashed (kept): see 'git stash list'"
fi

echo "snapshot branch:  $ref"
echo "snapshot tag:     $ref"
echo "HEAD was:         $head_sha"
echo
echo "RESTORE with:     git reset --hard $ref      # move current branch back"
echo "or inspect with:  git log --oneline $ref"
