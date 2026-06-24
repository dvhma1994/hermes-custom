# Hermes agent — improvements (2026-06-24, 12h session)

Native Hermes **plugins** (the user asked for "ready-made plugin bundles that improve decisions /
design"). Each is a real plugin (`plugin.yaml` + `register(ctx)`) using the **public hook surface** —
no core/hot-path edits. **Every change is flag-gated + fail-open**: loaded but a complete no-op until
its env flag is set, so the default path (and prompt cache) is unaffected. Unit-tested + live-verified.

---

## New plugins

### `plugins/reflection/` — cross-turn Reflexion nudge · `HERMES_REFLECTION=1`
At the start of a turn, if the **previous** turn contained tool/verification failures, injects a short
"root cause + different approach" nudge so the agent doesn't repeat the mistake. Delivered via the
`pre_llm_call` hook's `{"context": ...}` return → appended to the **user message**, never the system
prompt, so the prompt-cache prefix is preserved. Bounded (≤4 failures summarized) + dedup (one nudge per
session/turn). Complements — does not duplicate — the within-turn oscillation guard (`HERMES_OSC_GUARD`).

> Design note: `pre_llm_call` fires **once per turn** (in `build_turn_context`, before the tool loop), so
> a plugin cannot inject *mid*-turn after a single tool fails; and `post_tool_call`/`transform_tool_result`
> are not wired for Python plugins (shell-hooks only). Hence the cross-turn design — it's what the hook
> cadence actually supports, verified against the source.

### `plugins/redact_output/` — outgoing secret redaction · `HERMES_REDACT_OUTPUT=1`
Scans every final response (via the `transform_llm_output` hook) for high-signal credential shapes —
GitHub/OpenAI/Slack/GCP/AWS keys, JWTs, PEM private keys, Sakana `fish_` keys — and replaces matches with
typed placeholders (`‹redacted:github-token›`) before the reply is sent. Defensive security for a shared
chat/gateway transcript. Strict patterns, so obvious placeholders (`gho_example`, `sk-xxxx`) pass through.
Returns `None` when nothing matched (other transform hooks still run).

### `plugins/auto_index/` — keep the semantic index fresh · `HERMES_AUTO_INDEX=1`
The semantic-recall sidecar only contains what's been backfilled; new messages aren't searchable until
re-indexed. On each new session start (`on_session_start` — once per session, NOT per turn) this runs an
incremental, idempotent backfill of recent messages in a **daemon thread** (never blocks startup), via a
**read-only** DB connection. Single-flight guard avoids overlapping runs. Makes the already-shipped
`HERMES_SEMANTIC_RECALL` hybrid recall / `skill_search` / `session_search` stay useful over time. Clean
no-op unless `HERMES_SEMANTIC_RECALL=1` and the embedder is reachable.

> Rejected (honest scoping): a *per-turn* auto-recall injector — `pre_llm_call` would load the full vector
> matrix every turn (perf cliff) — and cross-session context injection (risk of leaking old secrets to a
> cloud model). auto_index is the clean slice that improves the existing infra without those costs.

---

## Tests
- `tests/plugins/test_reflection.py` (12) · `tests/plugins/test_redact_output.py` (10) ·
  `tests/plugins/test_auto_index.py` (6) · `tests/plugins/test_custom_plugins_integrity.py` (6) —
  34 total, all green.
- Integrity test asserts each plugin's `register()` wires exactly the hooks its manifest declares and only
  hooks in `VALID_HOOKS` (guards manifest/code drift).

## Enabling (local, on-device)
Both are already in `config.yaml` `plugins.enabled` (outside the repo) so they load, but stay **inert**
until you set the flag. Add to `~/.hermes/.env`:
```
HERMES_REFLECTION=1
HERMES_REDACT_OUTPUT=1
HERMES_AUTO_INDEX=1        # also needs HERMES_SEMANTIC_RECALL=1 + a reachable embedder
```
Fresh CLI sessions pick the flags up immediately; the running gateway needs a (user-triggered) restart.

## Safety posture
Flag-gated + fail-open + bounded; public hook surface only (no core edits); cache-preserving; every hook
callback wrapped so a fault returns a no-op and never breaks the agent loop.
