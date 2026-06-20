---
name: planning-with-files
description: Track task plans in a local file to survive interruptions.
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [planning, productivity, files, durable-state]
    category: productivity
---

# Planning With Files

Long, multi-step tasks lose context as a conversation grows: early steps scroll out of view, context compression discards detail, and an interruption can wipe working memory. Writing the plan and its progress to a local file gives the agent a durable scratchpad it can re-read at any point. This skill defines a small, repeatable workflow for creating and maintaining that file across the whole task lifecycle.

## When to Use

- A task has more than ~5 distinct steps or spans multiple tool sessions.
- An interruption (user steering, error recovery, context compaction) is likely.
- The work has ordering dependencies where skipping or repeating a step is costly.
- You need a clear handoff narrative if the user returns later.

Skip this for trivial single-shot answers or quick lookups — the file overhead isn't worth it.

## Prerequisites

- A writable working directory (the task's workspace or a scratch folder).
- Hermes file tools available: `read_file`, `write_file`, `patch`, `search_files`.
- A task concrete enough to enumerate steps. If you can't list steps yet, plan in the file as you discover them.

## Procedure

1. **Create the plan file early.** Before doing the substantive work, call `write_file` to create `PLAN.md` (or `TODO.md`) in the workspace root using the template below.
2. **List steps with status markers.** Each step gets one line: `- [ ]` (pending), `- [~]` (in progress), or `- [x]` (done). Keep step text to one line; push detail into Notes.
3. **Record decisions and context.** Add a short Notes section capturing key choices, file paths discovered, and constraints — anything a future-you (post-compression) would need.
4. **Update status as you go.** After completing a step, use `patch` (replace mode) to flip its marker. Patching one line is cheaper than a full rewrite and avoids clobbering concurrent edits.
5. **Re-read before resuming.** After any interruption or at the start of a new phase, call `read_file` on `PLAN.md` to re-load the full current state before acting.
6. **Search when the plan grows.** For plans past ~50 lines, use `search_files` (content mode) to jump straight to a step or note instead of scanning.
7. **Close out.** When the task is done, mark all steps `[x]`, add a one-line Outcome entry, and leave the file in place as a record.

### PLAN.md template

```markdown
# Plan: <task title>

## Goal
<one-sentence statement of done>

## Steps
- [ ] Step 1 — <verb + object>
- [ ] Step 2 — <verb + object>

## Notes
- <key decision, path, or constraint>

## Outcome
<filled in at the end>
```

## Quick Reference

| Action | Tool | Notes |
|---|---|---|
| Create plan | `write_file` | path `PLAN.md`, use template above |
| Mark step done | `patch` (replace) | flip `- [ ]` → `- [x]` for one line |
| Check current state | `read_file` | do this after every interruption |
| Find a step or note | `search_files` | content mode, pattern = step text |
| Rewrite whole plan | `write_file` | only when structure changes significantly |

## Pitfalls

- **Forgetting to update.** A stale plan is worse than none — it misleads. Patch status immediately after each step, not in a batch later.
- **Too much detail per step.** If a step needs a paragraph, split it into sub-steps or move detail to Notes. One-line steps stay scannable.
- **Rewriting instead of patching.** Calling `write_file` on every change risks dropping a step by accident. Prefer `patch` for single-line status flips.
- **Hiding the file in a deep path.** Keep `PLAN.md` at the workspace root so it's easy to find on resume.
- **Treating the plan as immutable.** Add, split, or remove steps as the task reveals itself; the file is a living document.

## Verification

- After creating `PLAN.md`, `read_file` it back and confirm all template sections are present.
- The plan is "working" when, after an interruption, a single `read_file` is enough to resume correctly — no guessing the last completed step.
- At task end, every step should read `- [x]` and the Outcome line should state the result.
- If a reader who wasn't present could follow what happened from the file alone, the skill was applied well.
