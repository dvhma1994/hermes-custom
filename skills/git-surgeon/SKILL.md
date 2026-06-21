---
name: git-surgeon
description: Safe advanced git without interactive editors: bisect regressions, recover via reflog, resolve conflicts, squash/reorder non-interactively, undo safely, preview pushes.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [git, version-control, bisect, reflog, rebase, merge-conflicts, undo, non-interactive]
    category: software-development
---

# Git Surgeon Skill

Advanced, **non-destructive-by-default** git for an automated agent that has **no
interactive editor**. Every recipe here runs to completion without ever opening
`vim`/`nano` â€” no `rebase -i` prompt, no commit-message editor, no merge-message
editor. Every destructive operation is preceded by a recoverable snapshot.

All commands below are verified on git 2.53.

## When to use

- A test/behavior regressed and you must find the exact commit that broke it â†’ **bisect**.
- Commits seem "lost" after a bad `reset --hard`, branch delete, or rebase â†’ **reflog**.
- A merge/rebase/cherry-pick stopped with conflict markers â†’ **resolve methodically**.
- History needs cleanup before pushing (combine WIP commits, drop a commit) â†’ **non-interactive squash/reorder**.
- You ran a command you regret and need to undo it cleanly â†’ **revert vs reset**.
- Before pushing, you want to see exactly what will leave your machine â†’ **push preview**.

## When NOT to use

- **Shared/published history.** Never rewrite (`rebase`, `reset --hard`, force-push)
  commits others have already pulled. Rewrite only local, un-pushed commits. To
  undo something already pushed, use `git revert` (a new commit), not a rewrite.
- **A trivial uncommitted edit.** Just `git checkout -- file` or `git restore file`.
- **You don't yet know what's wrong.** Diagnose first (see the `systematic-debugging`
  skill); surgery on the wrong commit wastes time.

---

## Rule 0: snapshot before every destructive op

Destructive = anything that moves a branch ref or rewrites commits: `reset --hard`,
`rebase`, `merge --no-ff` you might abort, `commit --amend`, `branch -D`,
`push --force`. Before any of them, make HEAD recoverable:

```bash
# One-liner snapshot: a branch + tag at the current commit.
git branch  backup/$(date +%Y%m%d-%H%M%S)
git tag     pre-surgery-$(date +%Y%m%d-%H%M%S)

# If the working tree is dirty, also stash a copy but KEEP your files:
git stash push --include-untracked --keep-index -m "pre-surgery"
```

Or use the bundled helper (does branch + tag + dirty-tree stash and prints the
restore command):

```bash
bash scripts/git-snapshot.sh my-label
# RESTORE with: git reset --hard backup/my-label-<timestamp>
```

To undo *any* botched surgery afterward: `git reset --hard <snapshot-ref>`.
Even without a snapshot, the **reflog** (below) is your safety net for ~90 days.

---

## 1. Bisect: pinpoint the commit that introduced a regression

Binary-search between a known-good and known-bad commit. The killer feature is
`bisect run` â€” fully automated, no prompts.

```bash
# Provide a test script that exits 0 = good, non-zero = bad (125 = skip/untestable).
# Example: the regression is "grep finds v=BAD in val.txt".
cat > /tmp/bisect-test.sh <<'EOF'
#!/bin/sh
# Build/setup if needed, then assert the GOOD condition:
grep -q "v=BAD" val.txt && exit 1   # bad
exit 0                              # good
EOF
chmod +x /tmp/bisect-test.sh

git bisect start <known-bad> <known-good>   # e.g. HEAD HEAD~20  or  HEAD v1.2.0
git bisect run /tmp/bisect-test.sh          # prints "<sha> is the first bad commit"
git show <first-bad-sha>                     # inspect the offending diff
git bisect reset                             # ALWAYS reset â€” restores your branch
```

Manual bisect (no script): `git bisect start`, then `git bisect bad` /
`git bisect good` at each checkout until git reports the first bad commit, then
`git bisect reset`.

**Traps:**
- Forgetting `git bisect reset` leaves you on a detached HEAD mid-search.
- The test must be deterministic and self-contained (rebuild artifacts inside it
  if the bug depends on compilation). Use `exit 125` for commits that can't be built/tested.
- Order is `bad` then `good`: `git bisect start <bad> <good>`.

---

## 2. Reflog: recover "lost" commits

`git reflog` records every position HEAD has held â€” surviving `reset --hard`,
deleted branches, and aborted rebases. Almost nothing is truly lost for ~90 days.

```bash
git reflog                              # find the line for the state you want
# e.g.  e55c845 HEAD@{1}: commit: important work

# Recover onto a NEW branch (safest â€” doesn't touch your current branch):
git branch recovered e55c845           # or: git branch recovered HEAD@{1}
git log --oneline recovered            # verify it's there

# Recover a deleted branch: its tip is in the reflog too:
git reflog | grep "<branch-name>"      # find the last sha
git branch <branch-name> <sha>
```

Lost commits from a squashed/dropped rebase: `git reflog` shows the pre-rebase
HEAD; `git reset --hard HEAD@{N}` (after snapshotting) returns you there.
Truly dangling objects: `git fsck --lost-found` lists unreachable commits.

**Trap:** reflog is **local and per-clone** â€” it does not exist on a fresh clone.
Recover from the machine where the work happened.

---

## 3. Resolve merge/rebase conflicts methodically

First, get readable conflict markers (shows the common ancestor â€” far easier):

```bash
git config merge.conflictstyle zdiff3   # one-time, recommended
```

When a `merge`/`rebase`/`cherry-pick` stops with conflicts:

```bash
git status                              # see what's in progress
git diff --name-only --diff-filter=U    # list ONLY the conflicted files
```

Each conflict looks like (with `zdiff3`):

```
<<<<<<< HEAD            (your current side, "ours")
MAIN
||||||| <base>         (common ancestor â€” what both started from)
b
=======
FEATURE                (incoming side, "theirs")
>>>>>>> feature
```

Resolve each file, then stage it:

```bash
# Option A â€” edit the file by hand, delete all <<<< ==== >>>> markers, then:
git add <file>

# Option B â€” take one side wholesale:
git checkout --ours   <file> && git add <file>   # keep current branch's version
git checkout --theirs <file> && git add <file>   # keep incoming version
#   NOTE: during a REBASE, "ours"/"theirs" are swapped (ours = the branch you're
#   rebasing ONTO, theirs = your commits). Verify with `git status` before choosing.

git diff --check                        # catch any leftover conflict markers
```

Finish the operation **without an editor**:

```bash
git commit --no-edit                    # for a merge (reuses the merge message)
git rebase --continue                   # for a rebase (set GIT_EDITOR=true if it
                                        #   tries to open a message: below)
git cherry-pick --continue
```

Bail out and restore the pre-conflict state at any time:

```bash
git merge --abort        # or: git rebase --abort   /   git cherry-pick --abort
```

**Traps:**
- Don't `git add` a file that still contains `<<<<<<<` markers â€” `git diff --check` prevents this.
- ours/theirs invert under rebase. When unsure, open the file rather than guessing.
- A `rebase --continue` may want to open the commit message editor; prefix with
  `GIT_EDITOR=true` to accept it unchanged (see Â§4).

---

## 4. Squash, drop, reorder â€” WITHOUT the interactive editor

`rebase -i` normally opens an editor. Drive it programmatically with two env vars:

- `GIT_SEQUENCE_EDITOR` â€” edits the *todo list* (pick/squash/reword/drop/order).
- `GIT_EDITOR` â€” handles any commit-*message* prompt. `GIT_EDITOR=true` accepts unchanged.

**Snapshot first** (Â§0), then:

### 4a. Cleanest squash: `--fixup` + `--autosquash` (no `sed` needed)

```bash
# You committed a follow-up that belongs in an earlier commit <target-sha>:
git commit --fixup=<target-sha>         # makes a "fixup! ..." commit

# Fold all fixups into their targets automatically, no editor:
GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash <base>
#   <base> = a commit BEFORE the target, e.g. HEAD~5, or --root for the very first.
```

### 4b. Squash the last N commits into one

```bash
# Mark every commit after the first as 'squash' via the sequence editor:
GIT_SEQUENCE_EDITOR="sed -i -e '2,\$s/^pick/squash/'" GIT_EDITOR=true \
  git rebase -i HEAD~4
#   -> the 4 commits become 1; GIT_EDITOR=true keeps the concatenated message.
#   On macOS/BSD sed use:  sed -i ''  (with an explicit empty backup arg).
```

### 4c. Drop a specific commit

```bash
BAD=$(git rev-parse <bad-sha>)
GIT_SEQUENCE_EDITOR="sed -i -e \"/^pick ${BAD:0:7}/d\"" GIT_EDITOR=true \
  git rebase -i <bad-sha>~1
#   Simpler alternative that needs no rebase: git revert --no-edit <bad-sha>
#   (revert is non-destructive â€” prefer it for already-pushed commits).
```

### 4d. Reorder commits

Write the todo file yourself by reordering its `pick` lines:

```bash
GIT_SEQUENCE_EDITOR='f(){ awk "NR==2{print prev} NR==1{prev=\$0;next} {print}" "$1" > "$1.t" && mv "$1.t" "$1"; }; f' \
  GIT_EDITOR=true git rebase -i HEAD~3   # swaps the first two picks (oldest two)
```
For non-trivial reorders it's clearer to dump the plan to a file and `cp` it in:
`GIT_SEQUENCE_EDITOR="cp /tmp/myplan"`.

**Traps:**
- Only on **un-pushed** commits. If pushed and shared, use `revert` instead.
- `GIT_EDITOR=true` blindly accepts messages â€” fine for squash, but use
  `reword` only when you supply the new message non-interactively (e.g.
  `git commit --amend -m "..."` after stopping with an `edit` line).
- A squash/reorder can conflict mid-way â†’ resolve per Â§3, then `git rebase --continue`.

---

## 5. Undo safely: revert vs reset (`--soft`/`--mixed`/`--hard`)

| Goal | Command | Touches | Use when |
|------|---------|---------|----------|
| Undo a **pushed/shared** commit | `git revert --no-edit <sha>` | adds a new commit | history is public â€” never rewrite it |
| Undo last commit, **keep changes staged** | `git reset --soft HEAD~1` | moves branch only | re-commit differently / combine commits |
| Unstage but keep edits in working tree | `git reset --mixed HEAD~1` (default) | branch + index | split a commit, restage selectively |
| Discard commit **and all its changes** | `git reset --hard HEAD~1` | branch + index + **tree** | local junk you truly want gone (snapshot first!) |
| Discard one file's uncommitted edits | `git restore <file>` (`git checkout -- <file>`) | working tree | revert a single file |
| Throw away ALL uncommitted edits | `git restore .` / `git reset --hard` | working tree | start over locally |

```bash
# Safe public undo (reverses the diff, preserves history):
git revert --no-edit <sha>            # range: git revert --no-edit <old>..<new>

# Reword the last (local) commit's message with no editor:
git commit --amend -m "fix: clearer message"
```

**The cardinal rule:** `reset --hard` is the only one that destroys working-tree
changes. Run Â§0 snapshot first; recover via Â§2 reflog if you skipped it
(`git reset --hard HEAD@{1}`).

---

## 6. Preview exactly what a push will send

Never push blind. Compare your branch against its remote-tracking ref:

```bash
git fetch origin                                   # refresh remote-tracking refs first

git log --oneline origin/main..HEAD                # the commits that will be pushed
git diff --stat origin/main..HEAD                  # files + churn those commits change
git diff        origin/main..HEAD                  # the full outgoing diff
git rev-list --count origin/main..HEAD             # how many commits ahead

# Ahead/behind in one shot (left = behind, right = ahead):
git rev-list --left-right --count origin/main...HEAD   # note the THREE dots
```

Or ask git directly without contacting the server:

```bash
git push --dry-run origin HEAD          # shows the ref update that WOULD happen
git status -sb                          # header line shows "ahead N, behind M"
```

**Traps:**
- Two dots `A..HEAD` = "commits in HEAD not in A" (what you'll push). Three dots
  `A...HEAD` with `--left-right --count` = symmetric ahead/behind. Don't mix them up.
- `origin/main` is only as fresh as your last `git fetch` â€” fetch first or the
  preview is stale.
- If you're behind, a plain push is rejected: rebase/merge first, and prefer
  `git push --force-with-lease` over `--force` (it refuses to clobber commits you
  haven't seen).

---

## Quick reference

| Task | Command |
|------|---------|
| Snapshot before surgery | `bash scripts/git-snapshot.sh label` |
| Find regressing commit | `git bisect start <bad> <good> && git bisect run ./test.sh` |
| Recover lost commit | `git reflog` â†’ `git branch recovered <sha>` |
| List conflicts | `git diff --name-only --diff-filter=U` |
| Take incoming side | `git checkout --theirs <f> && git add <f>` |
| Abort in-progress op | `git merge/rebase/cherry-pick --abort` |
| Squash fixups, no editor | `git commit --fixup=<sha>` â†’ `GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash <base>` |
| Undo pushed commit | `git revert --no-edit <sha>` |
| Undo local commit, keep staged | `git reset --soft HEAD~1` |
| Nuke local commit + changes | snapshot, then `git reset --hard HEAD~1` |
| Preview push | `git fetch && git log --oneline origin/main..HEAD && git diff --stat origin/main..HEAD` |
| Safe force-push | `git push --force-with-lease` |
