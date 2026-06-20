---
name: scout
description: Explore code in a subagent; return citations, not dumps.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [context, retrieval, delegation, token-efficiency]
    category: devops
---

# Scout Skill

When a question needs sweeping many files ("where is X handled?", "how does Y
flow?"), do NOT read whole files into the main context. Delegate a read-only
exploration to a subagent that fans out, then returns only a compact **citation
block** — file paths with line ranges and one-line notes — never raw file
dumps. The main agent keeps a small, high-signal context and decides what to
open. This mirrors the 2026 "context-firewall" pattern (fast, parallel
retrieval; up to ~60% fewer main-model tokens with higher precision).

## When to Use
- Broad "find / locate / map" questions across an unfamiliar or large repo.
- Before a change, to gather the exact sites to edit without flooding context.
- NOT for reading one known file — just `read_file` it directly.

## Prerequisites
- `delegate_task` available (delegation toolset) and `search_files` / `read_file`.

## How to Run
Delegate a leaf subagent scoped to read-only search, with an explicit
citation-only contract:

```
delegate_task(
  goal="Find every place <thing> is handled. Use search_files + read_file only. "
       "Return ONLY a citation block: for each hit, `path:line-range — one-line note`. "
       "Do NOT paste file contents. End with a 2-line summary of the overall flow.",
  toolsets=["file", "search"]
)
```

## Procedure
1. Frame ONE precise question (the scout returns better with a sharp target).
2. Delegate it read-only with the citation-only contract above.
3. Receive citations; the main agent decides which few sites to `read_file` in
   full. Open only what you must change or quote.
4. For big sweeps, delegate a **batch** of parallel scouts (one per subsystem)
   and merge their citation blocks.

## Quick Reference
- Scout returns `path:line-range — note`, never file bodies.
- Keep the scout read-only (`file`, `search` toolsets) — no edits.
- Open full files only after citations narrow the target.

## Pitfalls
- Letting the scout paste whole files (defeats the purpose) — enforce the
  citation-only contract in the goal text.
- Over-broad goals ("understand the repo") — scope to one question per scout.

## Verification
You used scout correctly if the main context grew by a short citation list (not
file dumps) and you then opened only the few files the citations pointed to.
