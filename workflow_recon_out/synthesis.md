All three contested/load-bearing claims are now confirmed against the tree:

1. **Compression def-order shadowing** (`conversation_compression.py:40,46,286`): the `compress_context = compress_context_rt02` rebind at line 46 is overwritten by `def compress_context(` at line 286. core-wiring is correct; the RT-02 shim is dead at runtime. compression-and-cache's "the shim is on the real path" claim is **wrong** — it missed the line-286 redefinition.

2. **Attribute-name mismatch**: `apply_decision` writes `_authority_context_size_override` (`runtime_authority.py:32,174`); the loop reads `_authority_context_size` (`conversation_loop.py:655`). Confirmed never reconciled outside tests.

3. **Execution-checkpoint orphan**: `classify_task` / `ExecutionCheckpoint` have zero non-test, non-audit importers. Confirmed.

I have enough to synthesize with high confidence.

---

# Hermes "Learning + Checkpoint" Architecture — State-of-the-Architecture Report

*Synthesis of 6 investigations, cross-checked against the working tree. All paths under `C:\Users\ZpLp\AppData\Local\hermes\hermes-agent`. This is uncommitted, mid-flight work (~42 commits behind origin/main, local-only).*

## 1. Overview — the vision

The effort is a coordinated attempt to make Hermes **learn from its own operation and persist work across interruptions**, layered on top of a turn-oriented agent that today forgets everything except the transcript. It has four threads that share one substrate (the OPVAL SQLite store) but are at very different maturity:

- **Observe** (OPVAL): record per-session/turn quality, tool-execution, drift, and outcome telemetry into SQLite (`agent/opval/store.py`). This is the foundation everything else consumes.
- **Learn + Govern** (Learning Governance V1): turn that telemetry into per-strategy "what runtime settings work" decisions — derive observations → score effectiveness → gate on confidence → record promote/retire decisions in an append-only, hash-chained governance ledger → apply a tightly-whitelisted set of five "authority knobs" to a live agent, with a circuit breaker for safe-mode. Deliberately conservative: thresholds are immutable constants in `agent/learning_constants.py`, and the learning loop is forbidden from editing its own thresholds (`FORBIDDEN_AUTHORITY_KNOBS`, `learning_constants.py:122-128`).
- **Persist execution** (Execution Checkpoint): when a long task is interrupted (e.g. Telegram `/stop`), serialize the turn's internal loop state (counters, todo, pending tool calls) so the next message **resumes** instead of restarting from the transcript.
- **Harden the plumbing** (RT-01 / RT-02): make session-rotation during compression transactional (locks, idempotency, lineage, rollback) and migrate gateway session state into `state.db`.

The throughline: **make Hermes durable and self-improving** — remember how well it did, adjust its own bounded configuration, and never lose a task to an interrupt. The reality (below) is that the observation floor and unit tests landed cleanly, but almost none of the consumer/runtime wiring actually executes.

## 2. Subsystem status table

| Subsystem | Purpose (1 line) | Wired into runtime? | Test health | Completeness |
|---|---|---|---|---|
| **OPVAL (Observe)** | Record per-session/turn quality/drift/outcome telemetry to SQLite | **Yes**, but flag-gated on `HERMES_OPVAL=1` (`conversation_loop.py:537-583`, capture/finalize `:4565-4651`) | Green (covered indirectly) | ~90% (functional floor) |
| **Learning (derive→govern)** | Derive observations from OPVAL, score, hash-chain governance ledger, dataset batches | **No** — zero non-test/non-doc importers of any of the 7 learning modules or `LearningGovernance`/`StrategicLearning` | Green in isolation (176/176, e.g. `test_learning_governance.py` 11, `test_learning_evidence_builder.py` 10) | ~70% logic, **0% integrated** |
| **Strategy/Authority** | Generate/score/drift strategies; apply 5 bounded knobs to live agent | **No** (managers test-only) + **broken M1 hook** (`conversation_loop.py:592-595` imports nonexistent `ensure_runtime_*`, confirmed) | Green in isolation (`test_runtime_authority.py` 13, `test_runtime_feedback.py` 14) | ~65% logic, integration broken |
| **Execution-Checkpoint** | Persist turn loop-state so interrupts resume, not restart | **No** — `execution_checkpoint.py` imported by nothing (confirmed: only test/audit refs) | Unknown — cited tests (`TestDefectFixes`) **do not exist in tree** | ~15% (dataclass only; all integration absent) |
| **Compression (RT-02)** | Transactional session rotation (lock/idempotency/lineage/rollback) around existing summarizer | **No** — shim rebind at `conversation_compression.py:46` is shadowed by `def compress_context` at `:286` (confirmed); legacy path wins | No tests (untracked new files) | ~50% (coordinator written, never reached) |
| **Tool-Cache (premise)** | *Premise to verify:* "no read-cache for read-only tools" | N/A — partially refuted: `read_file` has mtime dedup (`file_tools.py:489-505,870-926`); `search_files` has none | N/A | Premise is the finding, not a feature |

Notes on the table: "Completeness %" is logic-written vs. fully-integrated-and-proven; given AGENTS.md's "no dead code wired in without E2E proof" bar, **integrated completeness for Learning/Strategy/Checkpoint/Compression is effectively 0%.**

## 3. Milestones — inferred structure

From test names (`test_milestone1/2/3a/3b_integration.py`, `test_category_a_remediation.py`) and audit JSONs, the Learning effort is structured M1→M4 (RT-01/RT-02 are a parallel "runtime hardening" track):

- **M1 — Runtime Authority + Feedback (live reactive control).** Intended: per-turn `RuntimeFeedbackCollector.adapt()` tightens policy on tool failures, applied via `apply_decision`. **Status: landed-but-broken.** Classes + tests are green (`test_milestone1_integration.py` 11 passing), but the live hook is dead — `conversation_loop.py:592-595` imports `ensure_runtime_authority`/`ensure_runtime_feedback`, **neither exists** (confirmed: `def ensure_runtime_*` found nowhere), so the `ImportError` is swallowed at `:596` and `agent._runtime_*` is never set. Net: **in-progress, non-functional.**
- **M2 — Evidence + Effectiveness + Generation.** Derive observations, score win_rate/avg_score, generate candidate directives. **Status: unit-complete, unwired.** `test_milestone2_integration.py` 10 passing; no production importer.
- **M3a — Alignment + Demotion queue.** `AuthorityAlignmentMonitor` + `LearningDemotionRecommendations`. **Status: unit-complete, unwired** (`test_milestone3a_integration.py` 8 passing).
- **M3b — Governance + Dataset + Circuit Breaker.** Hash-chain ledger, dataset batching, safe-mode. **Status: unit-complete, unwired** (`test_milestone3b_integration.py` 10 passing). Contains acknowledged stubs (below).
- **M4 — (deferred).** Strategy generation/drift maturation referenced in module headers; not started.
- **"Category A remediation"** (`test_category_a_remediation.py` 12 passing) appears to be a hardening pass on knob-safety / ownership validation — green.
- **RT-01 (gateway state.db migration):** Phase 1 CLI (`hermes_cli/commands.py:238-244` repair/restore) + Phase 3 dual-path (`gateway/session.py:721-854`). **Status: real but dormant** — gated on `self._migration_complete()`; falls through to legacy `sessions.json` until the flag flips.
- **RT-02 (transactional compression):** coordinator + shim written. **Status: written, dead** (def-order shadow).
- **Execution-Checkpoint:** audits claim "PRODUCTION_VALIDATED" but the cited integration code, the v18 `pending_resume_checkpoints` table, and the `TestDefectFixes` tests are **all absent from the tree**. **Status: design + orphan dataclass only.** The audits dramatically overstate completion (their own caveats admit "synthetic runtime evidence," `execution_checkpoint_final_validation.json:34`).

**Done (unit-level only): M2, M3a, M3b, Category-A.** **In-progress/broken: M1 (broken hook), RT-02 (shadowed), Checkpoint (mostly nonexistent).** **Dormant by design: RT-01.**

## 4. Top risks (most important first)

1. **Entire Learning/Strategy/Authority stack is dead code wired (or staged) without E2E proof** — the headline AGENTS.md violation (`AGENTS.md:122-125`). The 10 learning modules are imported only by each other + tests + audit JSON. No `run_agent.py`/`cli.py`/`gateway/`/`cron/` path constructs `LearningGovernance`, `StrategicLearning`, or any manager. 176 green tests prove behavior-in-isolation, **not** integration — exactly the "green unit mocks" the repo distrusts (`AGENTS.md:85-87`).

2. **M1 runtime hook throws every turn and is silently swallowed** (confirmed): `conversation_loop.py:592-593` imports nonexistent `ensure_runtime_authority`/`ensure_runtime_feedback`; `ImportError` caught at `:596` (debug-log only). This single defect makes the entire reactive-control path inert — feedback recording (`tool_executor.py:106-126`), delegate feedback (`delegate_tool.py:2065-2091`), adapt hook (`conversation_loop.py:4065-4074`), and tool-scope filtering all dead-end on never-set attributes.

3. **Two latent prompt-cache landmines** (the "sacred" invariant, `AGENTS.md:19-23,90-91`). Dormant *only* because their enabling flags are never set; they break caching the moment M1 is repaired:
   - **Mid-conversation system-prompt mutation**: `conversation_loop.py:857-866` appends `_authority_tool_guidance`/`_authority_system_prompt_suffix` to the system message. **No writer exists for these attrs anywhere** (confirmed not even in `apply_decision`'s `_KNOB_TO_AGENT_ATTR`) — pure dead reads today, cache-busting if wired.
   - **Mid-conversation toolset swap**: `chat_completion_helpers.py:555-574` filters the cached tool list when `_authority_tool_scope` is set.

4. **Attribute-name mismatch makes the context-size knob a guaranteed no-op even if M1 is fixed** (confirmed): `apply_decision` writes `_authority_context_size_override` (`runtime_authority.py:32,174`); the loop reads `_authority_context_size` (`conversation_loop.py:655`). These never reconcile outside tests.

5. **RT-02 compression shim is silently neutralized by definition order** (confirmed): `conversation_compression.py:46` rebinds `compress_context = compress_context_rt02`, then `:286` redefines `def compress_context(...)`, overwriting it. **Report disagreement resolved:** *compression-and-cache* claimed the shim is "on the real path / not dead code" — this is **wrong**; it missed line 286. *core-wiring* is correct: the legacy compressor runs; `CompressionCoordinator` never instantiates at runtime. (Even if reached, the coordinator depends on ~15 `state_db` methods the shim itself doubts exist, `shim.py:54-57`, and Step 6 passes un-compressed `messages` while the agent keeps `compressed` — `coordinator.py:286-294` vs `shim.py:139`.)

6. **Execution-Checkpoint audits assert validation that the code contradicts.** "PRODUCTION_VALIDATED"/"267 passed"/schema-v18 claims cannot be reproduced: the integration code, both tables, and the cited tests are absent. Only the orphan `execution_checkpoint.py` dataclass exists. Anyone trusting the audits will believe a shipped feature that isn't there.

7. **Correctness gaps in the (unwired) governance layer that will surface on first integration:**
   - **Strategy-id identity mismatch** (`learning_evidence_builder.py:127` derives `strategy:{domain}` vs `strategic_learning.py:114-115` requiring the constructor arg) → `derive_and_record` raises "Observation does not belong to this strategy" unless the caller passes the exact derived string.
   - **`verify_chain` can't catch a tampered first event's `previous_hash`** (`learning_governance.py:181,185-186`) — first event's stored `previous_hash` is never fed into recomputation.
   - **Stubs masquerading as complete:** `_check_contract_version` is a `pass` no-op (`learning_evidence_builder.py:96-105`); `LearningMirrorCircuitBreaker.restore` records HEALTHY without the chain replay its own docstring requires (`learning_mirror_circuit_breaker.py:90-91`).

8. **`search_files` re-runs every call** — no dedup/cache (`file_tools.py:1657`), unlike `read_file`'s mtime dedup. Real but minor token waste, not a correctness risk.

## 5. Recommended next actions to make it "superhuman"

Ordered by leverage. Each tagged **[FINISH]** (complete started work) or **[NEW]**, with effort and core/caching risk.

1. **[FINISH] Fix or remove the M1 broken hook.** Either implement `ensure_runtime_authority`/`ensure_runtime_feedback` in `agent/runtime_authority.py` / `agent/runtime_feedback.py`, or delete the import block at `conversation_loop.py:592-595`. A swallowed per-turn `ImportError` is the worst state — neither working nor visibly broken. **Effort: S.** **Risk: low** if removing; **medium** if wiring (re-introduces caching hooks #3 below). *Do not wire it live until #3 is resolved.*

2. **[FINISH] Reconcile the authority attribute names and prove the cache impact before enabling.** Fix the `_authority_context_size_override` vs `_authority_context_size` mismatch (`runtime_authority.py:174` vs `conversation_loop.py:655`), and decide the fate of the never-written `_authority_tool_guidance`/`_authority_system_prompt_suffix`/`_authority_tool_scope` reads. Any knob that mutates the system prompt or toolset mid-conversation **must** be gated to apply only at turn boundaries / new sessions to preserve prompt caching (`AGENTS.md:19-23`). **Effort: M.** **Risk to caching: HIGH if mishandled** — this is the single most dangerous spot in the whole effort.

3. **[FINISH] Resolve the RT-02 def-order bug or back it out.** Delete the legacy `def compress_context` at `conversation_compression.py:286` (so the line-46 rebind wins) **only after** verifying the ~15 `state_db` methods exist in `hermes_state.py` and adding E2E tests; otherwise remove the rebind and the untracked `compression_coordinator.py`/`compression_rt02_shim.py` to avoid dead code. Also fix Step-6 (`coordinator.py:286-294` receiving un-compressed `messages`, `shim.py:139`). **Effort: M.** **Risk: medium** (compression is the one sanctioned cache-invalidation point, so prompt-cache risk is bounded, but rotation correctness/rollback is high-stakes).

4. **[FINISH] Make the audits truthful for Execution-Checkpoint, then build the real integration.** First, mark the "PRODUCTION_VALIDATED" audit JSONs as design-only (they claim absent code). Then implement the designed surface incrementally: `hermes_state.py` table + `save/load/delete/prune`, the `conversation_loop.py`/`tool_executor.py` capture hooks, and `turn_context.py` resume — with **real** (not synthetic) interrupt-resume tests. **Effort: L.** **Risk: medium** (touches the live turn loop and gateway; the dataclass `execution_checkpoint.py` is sound to build on).

5. **[FINISH] Build ONE thin, real integration of the learning pipeline end-to-end before adding any M4 features.** Wire a single offline path: read OPVAL session → `derive_observations` → `evaluate` → `LearningGovernance.promote/retire` → `apply_governance_event` → `RuntimeAuthority`, behind a flag like the existing `HERMES_OPVAL` gate, exercised by an E2E test against a temp `HERMES_HOME`. This is what converts "self-consistent island" into "feature." Fix the strategy-id identity contract (`strategic_learning.py:114-115` vs `learning_evidence_builder.py:127`) as part of this — it will fail on the first real call. **Effort: L.** **Risk: low-medium** (offline derivation path has no per-turn caching impact; keep it out of the hot loop).

6. **[FINISH] Close the governance-layer correctness gaps while they're cheap (pre-integration).** Fix `verify_chain` first-event handling (`learning_governance.py:181`); implement or explicitly defer-with-raise `_check_contract_version` (`learning_evidence_builder.py:96`) and `LearningMirrorCircuitBreaker.restore`'s missing chain replay (`learning_mirror_circuit_breaker.py:90`); tighten degraded-mode enforcement so it can't pass non-policy knobs (`learning_governance.py:146`). **Effort: M.** **Risk: low** (all currently unwired; no core/caching exposure).

7. **[NEW] Add `search_files` dedup reusing the existing `_read_tracker`.** Mirror the `read_file` mtime-dedup pattern at `file_tools.py:1657`, sharing invalidation (`notify_other_tool_call`, `reset_file_dedup`). Genuinely new but small, well-scoped token savings; not on the critical path. **Effort: S.** **Risk: low** (must register the same compression-reset/`terminal`-invalidation hooks to avoid stale reads).

8. **[NEW] Add a read-only/mutating flag to the tool registry.** `registry.register()` has no such metadata (`registry.py:234-248`); today read-vs-mutate is inferred from ad-hoc sets (`_READ_SEARCH_TOOLS`, `model_tools.py:570`). A formal flag would make #7 and any future dispatch-level cache (natural seam: `model_tools.py:1131-1141`) safe and general. **Effort: M.** **Risk: low.**

### Where the reports disagreed or were uncertain (flagged explicitly)
- **Is the RT-02 compression shim live?** *compression-and-cache* said yes; *core-wiring* said no (def-order shadow). **I verified: core-wiring is correct** (`conversation_compression.py:46` rebind overwritten by `def` at `:286`). The shim is dead at runtime.
- **Execution-Checkpoint completeness:** the audit JSONs claim full validation; *checkpoint-resume* and my grep confirm the integration code and cited tests are **absent**. Trust the tree, not the audits.
- **Whether M2/M3 integration tests carry real E2E weight:** *test-health* explicitly could not determine whether `test_milestone*_integration.py` exercise real imports vs. mock the boundary (out of scope for a test-only run). Given zero production importers (confirmed by *core-wiring* and *strategy-authority*), these are integration tests **between learning modules**, not between learning and the live agent — so they do not satisfy the AGENTS.md E2E bar.
- **`state_db` methods for RT-02:** *compression-and-cache* could not confirm the ~15 coordinator-required methods exist in `hermes_state.py`; this is an open verification item gating action #3.