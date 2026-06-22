# Hermes agent — improvements (2026-06-22)

A session of capability + reliability work on the live on-device agent. **Every change is
flag-gated and fail-open** (a complete no-op when its flag is unset), reversible, and
verified with unit tests, zero-regression checks against the known pre-existing failures,
live proof on-device, and adversarial review by an independent model. Nothing mutates the
system prompt or tool list on the default (flag-off) path, so prompt caching is unaffected.

---

## New capabilities

### Hybrid semantic recall — `agent/semantic_recall.py`  ·  `HERMES_SEMANTIC_RECALL=1`
Session/recall search was FTS5 keyword-only, so a query worded differently from the stored
text missed. Adds a vector index fused with FTS5 via **Reciprocal Rank Fusion**. Dependency-light
(numpy + stdlib sqlite — no vector extension), pluggable embedder (local Ollama / OpenAI /
Ollama-cloud), and the index lives in an **isolated sidecar DB** (`state.db.semantic.db`, own
connection+lock) — it never touches the live `state.db` connection. Opt-in via
`session_search(..., semantic=true)`; falls back to FTS5 when off/empty. Backfill with
`semantic_recall.backfill_for_db(db)`.

### Semantic skill discovery — `tools/skill_search_tool.py`  ·  gated by `HERMES_SEMANTIC_RECALL`
`skill_search(query)` ranks skills by **meaning** (hybrid keyword + vector over each skill's
name/description, reusing the sidecar index, `kind="skill"`). Registered + gated at RUNTIME via
`check_fn` (so it never races `.env` loading and is excluded byte-identically when off); fail-open to
keyword ranking. **Status:** engine built + tested + verified ranking on the real 93-skill set, but NOT
yet offered to the model — exposing it requires adding `skill_search` to the static `TOOLSETS` config
(`toolsets.py`), a change with cross-profile invariants best made + verified deliberately (the agent
already finds skills adequately via `skills_list` in the meantime).

### Dynamic model router / fallback auto-population — `agent/model_router.py`  ·  `HERMES_MODEL_ROUTER=1`
The fallback mechanism existed but the chain shipped **empty**, so when the primary model
rate-limited/overloaded the turn just failed. The router discovers the models available to the
primary's Ollama provider (or an explicit `HERMES_ROUTER_MODELS` pool) and **auto-populates the
empty fallback chain** (capped, code-capable first, excludes the primary incl. same-base-different-tag).
Failure-only (never changes the model on a successful turn → caching safe); never overwrites an
explicitly-configured chain.

### Skill-induction proposer — `agent/skill_induction.py`  (read-only)
Mines recurring multi-tool **workflows** from past sessions and *proposes* candidate skills
(`propose_skills_from_db(db)`) — it never auto-creates anything, so the human/curator is the safety
gate. Read-only, fail-open, no hot-loop coupling.

### Portable eval harness — `agent/eval_harness.py`  (offline)
A generic fixture scorer (`score_project`/`run_evals`) + scoreboard JSONL to regression-check the
agent's coding ability over time. 100% offline (subprocess pytest), no hardcoded paths, fail-open.

---

## Reliability hardening (13 capabilities — earlier the same day)
All flag-gated + fail-open. Detailed history in the project memory.

| Capability | Flag |
|---|---|
| Execution ledger (file-state survives compression) | `HERMES_EXEC_LEDGER` |
| Verification ledger (checks survive compression, staleness-aware, FAIL snippet) | `HERMES_EXEC_LEDGER` |
| Definition-of-Done gate | `HERMES_DOD_GATE` |
| Regression-test gate | `HERMES_REGRESSION_GATE` |
| Plan-completion (todo) gate | `HERMES_TODO_GATE` |
| Tool-failure oscillation guard | `HERMES_OSC_GUARD` |
| Salience-aware terminal truncation | `HERMES_SALIENT_TRUNCATION` |
| Env-setup failure diagnosis | `HERMES_ENV_DIAG` |
| Subagent file-write audit | `HERMES_SUBAGENT_AUDIT` |
| Plan-first nudge | `HERMES_PLAN_NUDGE` |
| Git worktree-destruction gating | (default-on, no flag) |
| V4A multi-file patch rollback | `HERMES_V4A_ROLLBACK` |
| V4A move-path guard + secret redaction | (hardens gated code) |

---

## Enabling (local, on-device)
Add to `AppData\Local\hermes\.env` (omit a line to disable):
```
HERMES_MODEL_ROUTER=1
HERMES_SEMANTIC_RECALL=1
HERMES_EMBED_PROVIDER=ollama_local
HERMES_EMBED_BASE=http://127.0.0.1:11500
```
Then build the semantic index once: `python -c "from agent.semantic_recall import backfill_for_db; from hermes_state import SessionDB; print(backfill_for_db(SessionDB()))"`.
Fresh CLI sessions pick up flags immediately; the running gateway needs a restart.

## Evidence
Coding parity proven against Claude on 3 hidden-oracle trials (ratelimit 29/29, cron 38/38, real-repo
integration 36/36 = 103/103, 0 reviewer intervention). New capability suites add ~80 tests; all reliability
suites green (211). Every wired change verified live on-device.
