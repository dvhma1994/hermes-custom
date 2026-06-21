# Hermes — custom build (what's added & developed)

A personalized fork of [hermes-agent](https://github.com/dvhma1994/hermes-agent). Installs and runs
exactly like upstream (same `pyproject.toml` / `setup.py` / `Dockerfile` / desktop bootstrap
installer) — this file documents everything that's *different*.

---

## 1. Self-improvement loop — wired end-to-end & live

Upstream shipped a large, milestone-structured "Learning Governance" effort that was **dead code**:
~16 `agent/` modules with green unit tests but **0% integrated** into the running agent. This build
wires it into a working loop and turns it on.

```
OPVAL telemetry ──▶ observations ──▶ effectiveness ──▶ governance decision ──▶ applied authority
  (per session)      (evidence)       (win-rate)        (hash-chained, idempotent)   (per session, cache-safe)
```

- **`agent/learning_cycle.py`** — the orchestrator: `run_learning_cycle()` (offline digest),
  `apply_learned_authority()` (session-start, cache-safe), `maybe_run_session_end_digest()` (throttled).
- **`runtime_authority` / `runtime_feedback`** — per-turn reactive control (baseline-aware).
- **strategy effectiveness / generation / drift**, **hash-chained governance** (tamper-evident),
  **evidence builder**, **mirror circuit breaker** (refuses restore on a tampered chain).
- Entirely **flag-gated**: a complete no-op (no DB touch) unless `HERMES_LEARNING=1`. `HERMES_OPVAL=1`
  collects the telemetry that feeds it.

**Critical fix #1 — telemetry was silently dead:** `agent/opval/store.py` ran `CREATE INDEX` on new
columns *before* the additive column migrations, and `_MIGRATIONS` was missing 4 columns — so on any
pre-existing `state.db`, `OpvalStore` raised `no such column` and OPVAL recording silently died,
starving the loop. Fixed: migrations are now table-aware and run **before** the schema script, plus
the 4 missing column migrations were added. Telemetry records again; the loop has data.

**Critical fix #2 — promotion was mathematically impossible:** the collectors store
`drift_pct`/`misalignment_pct` on a **0–100 percent** scale, but `strategy_effectiveness_manager`
compared them as **0–1 fractions** — so `avg_alignment` collapsed to 0 on *any* misaligned turn (vs the
65 gate) and `avg_drift` was ~100× inflated (vs the 0.15 gate). No strategy could *ever* be promoted;
the signal was a flat, degenerate constant. Fixed the units (`alignment = 100 − misalignment_pct`,
`drift = drift_pct / 100`); a clean high-win-rate strategy now actually promotes. Two related signal
fixes landed too: readiness no longer aggregates over **all** sessions when the eligible set is empty
(`1=1` → `0=1`), and `is_drift_alert` is now read-only so polling no longer drags the averaged baseline
toward the current value and self-extinguishes a real drift. **The loop can now show measurable
self-improvement** — the gradient that was missing.

## 2. Hardening fixes — 53 verified bugs, DoD-gated

Found via parallel adversarial bug-hunts (read-only finders → independent verifiers that reproduce
each candidate against the live code), then fixed one at a time under a strict gate: **each fix has a
regression test that fails on the old code and passes on the new**, and the affected subsystem's
existing suite stays green. Regression suites live in `tests/agent/test_overnight2_fixes.py` and
`tests/agent/test_hunt3_fixes.py`. 12 of the fixes were delegated to the agent itself (engineer role)
and independently audited.

**Security (4):**
- Hardline command floor (`rm -rf /`, `dd` to a raw device, `shutdown`) was **bypassable by quoting**
  the path/command word (`rm -rf "/"`); the detector normalizer only stripped *empty* quote pairs.
- The dangerous-command guard consulted only the **first** matching pattern, so a session approval of
  one common op (e.g. "recursive delete") silently authorized a *different* destructive op bundled into
  the same command (`rm -rf x && git push --force`). Now keyed on the pattern **combination**.
- **Path traversal** in transcript cleanup: an unvalidated `session_id` (`../…`) could delete files
  outside the sessions dir. Now rejected + containment-checked.
- **Cross-profile credential leak**: dashboard env writes scoped to *another* profile still mutated the
  running process's shared `os.environ`, clobbering live credentials. Now gated to the active profile.

**Data integrity & correctness:** SQLite success decided from a statement's `rowcount` (not the
connection's cumulative `total_changes`); governance hash-chain ordered by insertion (no fork on equal
timestamps); conversation replay ordered by id, not wall-clock (survives a clock regression); memory
entries reject the on-disk delimiter (no silent entry-splitting) and keep live state on drift; Bedrock
images decoded (were double-base64'd); a tool result for a Codex-shape `call_id` tool-call no longer
dropped.

**Crash guards** across the turn loop, tool executor, compression, providers, and every gateway
adapter — null/non-string error fields, non-dict tool args, unknown email charsets, impossible-but-valid
cron expressions (`0 9 31 2 *` = Feb 31, which used to crash the **entire** scheduler tick), and more —
now degrade gracefully instead of aborting the turn / tick / poll batch.

**Formatting fidelity:** Slack/Telegram/WhatsApp/Signal message rendering (link-label escaping, URL
parens, UTF-16 mention offsets, header bold, UUID mentions) and the rate-limit "resets in 2h30m"
parser.

- **Backup retention** (`hermes_state_backup.py`): hot/repair/daily `state.db` snapshots are now
  capped by **count *and* age**, and the `repair/` tier — which was **never pruned** — is cleaned.
  (In the wild this had let the backup dir grow to ~190 GB of 360 MB snapshots.)

## 3. Operating doctrine — baked into the default SOUL

`hermes_cli/default_soul.py` seeds a tuned doctrine into every install:

- **Internal thinking protocol** — reframe → surface unknowns → find the crux → options → pre-mortem → commit.
- **Scope before you start** — decompose open-ended tasks into bounded, individually-verifiable
  sub-tasks; finish & save each before the next; report honest scoped status instead of saturating.
- **Definition of Done** — a non-negotiable self-verification gate: reproduce→prove, full check (not
  the happy path), adversarial self-review, scope & hygiene, requirement check. Report **DONE** with
  evidence or **BLOCKED** — never a hopeful "done".
- **Output craft** — lead with the result, calibrate length, evidence over adjectives, recommend
  don't enumerate, code that reads like its neighbors.

## 4. Validated: is the self-verified output trustworthy without review?

The doctrine above was stress-tested during development (agent solves unseen tasks under its DoD gate;
results blind-audited against hidden oracle tests it never sees):

| Task class | Result |
|---|---|
| Bounded coding tasks (trap-laden) | **111/111 hidden tests**, 0 reviewer intervention |
| Open-ended "find & fix the bugs" (ground-truthed) | **12/12 planted bugs found, 0 fabricated fixes, 27/27 hidden tests** |
| Algorithmic-coding benchmark vs a frontier model | dead heat (166/166 each) |

Conclusion: on bounded/self-contained work the agent's self-verified `DONE` held up under independent
audit; the open-ended scoping gap (it used to saturate) is closed by the "scope-before-start" rule.

## 5. Bundled skills (`./skills/`)

`deep-verify` · `scout` · `orchestrate` · `autonomous-auditor` · `claim-verify` · `codebase-onboarding`
· `local-web-research` · `local-data-explore` · `mermaid-diagrams` · `planning-with-files`
· `progressive-recall` · `self-eval` · `structured-output` · `dependency-analyzer`

## Install & config

```bash
git clone https://github.com/dvhma1994/hermes-custom.git hermes && cd hermes
pip install -e .                 # or: docker build -t hermes . && docker compose up
cp .env.example .env             # fill your own keys; add HERMES_OPVAL=1 and HERMES_LEARNING=1
hermes                           # first run seeds the custom SOUL + skills into your HERMES_HOME
```

## Safety / what's NOT included

No secrets and no personal data: `.env` (keys/tokens), `state.db` (sessions/messages), backups, auth
tokens, and caches are git-ignored and were verified absent from every tracked file. The CI
workflows under `.github/workflows/` are omitted (the push token lacked GitHub `workflow` scope);
re-add them with `gh auth refresh -s workflow` if you want CI.

## Based on

[hermes-agent](https://github.com/dvhma1994/hermes-agent) by Nous Research — MIT licensed.
