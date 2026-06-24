# Hermes-custom — capabilities index

Single reference for everything this fork adds on top of upstream Hermes (rounds 1–5).
**Every addition is flag-gated + fail-open**: a complete no-op (byte-identical default path,
prompt cache untouched) until you opt in. Each flag below is confirmed against the source file
that reads it. Detailed write-ups: [IMPROVEMENTS-2026-06-22.md](IMPROVEMENTS-2026-06-22.md) and
[IMPROVEMENTS-2026-06-24-12h.md](IMPROVEMENTS-2026-06-24-12h.md).

## Quick enable (`~/.hermes/.env`)
```
# Capabilities
HERMES_SEMANTIC_RECALL=1
HERMES_EMBED_PROVIDER=ollama_local
HERMES_EMBED_BASE=http://127.0.0.1:11500
HERMES_MODEL_ROUTER=1
HERMES_EXPERT_CONSULT=1        # + sakana provider in config.yaml; HERMES_EXPERT_MODEL=fugu (default) | fugu-ultra
# Plugins (also need the names in config.yaml plugins.enabled — see bottom)
HERMES_REFLECTION=1
HERMES_REDACT_OUTPUT=1
HERMES_AUTO_INDEX=1           # needs HERMES_SEMANTIC_RECALL + a reachable embedder
# Reliability gates (enable any subset)
HERMES_EXEC_LEDGER=1
HERMES_DOD_GATE=1
HERMES_REGRESSION_GATE=1
HERMES_TODO_GATE=1
HERMES_OSC_GUARD=1
HERMES_SALIENT_TRUNCATION=1
HERMES_ENV_DIAG=1
HERMES_SUBAGENT_AUDIT=1
HERMES_PLAN_NUDGE=1
HERMES_V4A_ROLLBACK=1
```

## Plugins (native `plugin.yaml` + `register(ctx)`) — round 5
| Plugin | Flag | Hook | What it does |
|---|---|---|---|
| `plugins/reflection` | `HERMES_REFLECTION` | `pre_llm_call` | Cross-turn Reflexion: if the previous turn had tool failures, injects a root-cause+alternative nudge into the next turn's user message (cache-safe). |
| `plugins/redact_output` | `HERMES_REDACT_OUTPUT` | `transform_llm_output` | Redacts secrets (GitHub/OpenAI/Slack/GCP/AWS keys, JWT, PEM, `fish_`) from outgoing replies → typed placeholders. |
| `plugins/auto_index` | `HERMES_AUTO_INDEX` | `on_session_start` | Daemon-thread incremental backfill keeps the semantic index fresh (needs `HERMES_SEMANTIC_RECALL` + embedder). |

## Capabilities (modules / tools) — rounds 1–4
| Capability | Flag(s) | Where |
|---|---|---|
| Hybrid semantic recall (FTS5+vector RRF, sidecar DB) | `HERMES_SEMANTIC_RECALL`, `HERMES_EMBED_PROVIDER`, `HERMES_EMBED_BASE` | `agent/semantic_recall.py`; opt-in in `tools/session_search_tool.py` |
| Semantic `skill_search` tool | gated by `HERMES_SEMANTIC_RECALL` | `tools/skill_search_tool.py` |
| Dynamic model router / fallback auto-population | `HERMES_MODEL_ROUTER`, `HERMES_ROUTER_MODELS` | `agent/model_router.py` (wired in `agent/agent_init.py`) |
| Expert escalation `consult_expert` (→ Sakana Fugu) | `HERMES_EXPERT_CONSULT`, `HERMES_EXPERT_MODEL` | `tools/consult_expert_tool.py` |
| Skill-induction proposer (read-only) | (offline util) | `agent/skill_induction.py` |
| Portable eval harness (offline) | (offline util) | `agent/eval_harness.py` |

## Reliability gates — round 0 (13)
| Gate | Flag |
|---|---|
| Execution + verification ledger (survive compression) | `HERMES_EXEC_LEDGER` |
| Definition-of-Done gate | `HERMES_DOD_GATE` |
| Regression-test gate | `HERMES_REGRESSION_GATE` |
| Plan-completion (todo) gate | `HERMES_TODO_GATE` |
| Tool-failure oscillation guard | `HERMES_OSC_GUARD` |
| Salience-aware terminal truncation | `HERMES_SALIENT_TRUNCATION` |
| Env-setup failure diagnosis | `HERMES_ENV_DIAG` |
| Subagent file-write audit | `HERMES_SUBAGENT_AUDIT` |
| Plan-first nudge | `HERMES_PLAN_NUDGE` |
| V4A multi-file patch rollback | `HERMES_V4A_ROLLBACK` |
| Git worktree-destruction gating | (default-on, no flag) |
| V4A move-path guard + secret redaction | (hardens gated code) |

## Enabling the plugins
Plugins also need their key in `config.yaml` under `plugins.enabled` (already set on this device):
```yaml
plugins:
  enabled: [reflection, redact-output, auto-index]
```
Fresh CLI sessions pick up flags/plugins immediately; the running gateway needs a (user-triggered) restart.

## Safety model
Flag-gated + fail-open everywhere; public extension surfaces only (plugin hooks, tool registry, provider
config) — no core/hot-path edits; semantic recall uses an isolated sidecar DB; plugin hooks are each wrapped
so a fault is a no-op. Cumulative test status: 166 green across capability + plugin suites (2026-06-24).
