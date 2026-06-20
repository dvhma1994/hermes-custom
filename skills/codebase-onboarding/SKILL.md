---
name: codebase-onboarding
description: Onboard any repo fast with repomix and symbol navigation.
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [codebase, onboarding, navigation, repomix, serena]
    category: software-development
---

# Codebase Onboarding

Ad-hoc grepping a new repo is slow: you jump between files, miss cross-cutting
concerns, and never build a mental map. This workflow fixes that by **first
packing the entire repository into a single context file** with `repomix`, then
**navigating at the symbol level** with the `serena` MCP server and structural
search via `ast-grep`. Everything runs locally — no API keys, no external
service.

## When to Use

- You just cloned or were handed an unfamiliar repository.
- You need to answer "where is X defined and who calls it?" quickly.
- You want a single consolidated file to feed into analysis or review.
- You are preparing a PR review and need to understand the architecture fast.
- You want symbol-accurate navigation (find a class, its methods, their callers)
  without relying on text matching alone.

## Prerequisites

All three tools are **local and free** — no API key required.

- `repomix` CLI — concatenate a whole repo into one file:
  `npx repomix` (or install globally: `npm i -g repomix`, then run `repomix`).
- The `serena` MCP server — symbol-level code intelligence, exposing tools such
  as:
  - `mcp_serena_activate_project` — point serena at the repo root.
  - `mcp_serena_get_symbols_overview` — list classes/functions in a file.
  - `mcp_serena_find_symbol` — locate a definition by name path.
  - `mcp_serena_find_referencing_symbols` — find all callers/users.
  - `mcp_serena_search_for_pattern` — regex search scoped to the codebase.
- `ast-grep` CLI (`sg`) — structural, AST-aware code search:
  `npm i -g @ast-grep/cli` then run `sg run`.

## Procedure

1. **Pack the repo.** From the repository root:
   ```bash
   repomix --style markdown --output repomix-out.md .
   ```
   For XML (better for tool parsing): `repomix --style xml --output repomix-out.xml .`

2. **Skim the packed file.** Open `repomix-out.md` (or read it with `read_file`).
   Look at the directory tree, file list, and metrics at the top — this gives
   you the lay of the land in seconds. Note the 5–10 files that matter most.

3. **Activate the project in serena.** Call:
   ```
   mcp_serena_activate_project(project="/path/to/repo")
   ```
   This primes the LSP backend so subsequent symbol queries are accurate.

4. **Get a symbols overview.** For each key file from step 2:
   ```
   mcp_serena_get_symbols_overview(relative_path="src/index.ts", depth=1)
   ```
   This lists top-level classes, functions, exports — no need to read the file.

5. **Drill into symbols and references.** Find a definition:
   ```
   mcp_serena_find_symbol(name_path_pattern="AuthService/login", include_body=true)
   ```
   Then find everyone who calls it:
   ```
   mcp_serena_find_referencing_symbols(name_path="AuthService/login", relative_path="src/auth/auth-service.ts")
   ```
   Use `mcp_serena_search_for_pattern` for regex sweeps when you only have a
   string fragment.

6. **Structural search with ast-grep.** When you need a pattern, not a name:
   ```bash
   sg run -p 'class $NAME { $$$ }' --lang typescript
   sg run -p 'await $_.fetch($$$)' --lang javascript
   ```

## Quick Reference

| Goal | Command / Tool |
|------|----------------|
| Pack repo into one file | `repomix --style markdown --output repomix-out.md .` |
| View repo structure | skim top of `repomix-out.md` |
| Point serena at repo | `mcp_serena_activate_project` |
| List symbols in a file | `mcp_serena_get_symbols_overview` |
| Find a definition | `mcp_serena_find_symbol` |
| Find all callers | `mcp_serena_find_referencing_symbols` |
| Regex search codebase | `mcp_serena_search_for_pattern` |
| Structural AST search | `sg run -p '<pattern>' --lang <lang>` |

## Pitfalls

- **Huge repos blow up the packed file.** Use a `.repomixignore` (same syntax as
  `.gitignore`) or `--include` to restrict paths, e.g.
  `repomix --include "src/**/*.ts" .`
- **serena tools fail silently if the project isn't activated first.** Always
  call `mcp_serena_activate_project` before any other `mcp_serena_*` tool.
- **ast-grep needs the correct language flag** (`--lang python`, `--lang rust`,
  etc.). Without it, `sg` may refuse to parse the file or mis-match.
- **repomix binary/asset files are noisy.** Exclude `*.png`, `*.lock`,
  `dist/`, `node_modules/` via ignore patterns.
- **serena language servers take a moment to index.** If the first symbol query
  returns nothing, retry once — the LSP backend may still be warming up.

## Verification

Onboarding succeeded when you can confidently answer, for at least one core
concept in the repo:

- **Where is it defined?** — `mcp_serena_find_symbol` returns the file and body.
- **Who calls it?** — `mcp_serena_find_referencing_symbols` returns the list.
- **What's the overall shape?** — you can describe the directory layout from the
  repomix output without re-reading it.

If you can answer "where is X defined and who calls it?" in under a minute per
query, the workflow is working.
