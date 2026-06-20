# Learning Governance V1 — Lessons Learned

## Major Design Corrections

### 1. Single Authority vs. Distributed Authority
**Initial design:** Authority decisions distributed across Strategic Learning, Evidence Builder, and future Governance.
**Correction:** Centralized all promotion/retirement/enforcement into `LearningGovernance` as the **sole authority**. Strategic Learning became a passive applier via `apply_governance_event`. This eliminated governance bypass paths and made audit chain complete.

### 2. M3 Split into 3a (Recommend) / 3b (Act)
**Initial design:** Single Milestone 3 for both recommendation and governance.
**Correction:** Split into 3a (observe/recommend) and 3b (act/govern). This created a clear gate: 3a could be built and accepted with zero governance authority, while 3b required explicit unfreeze authorization after learning architecture existed. The split forced rigorous scope isolation testing.

### 3. Governance Freeze/Unfreeze Mechanism
**Initial design:** No explicit freeze state; governance could be built anytime.
**Correction:** Implemented `LEARNING_GOVERNANCE_UNFREEZE_AUTHORIZATION` artifact. Milestone 3b build was blocked until the freeze was conditionally lifted (after M3a proved the learning architecture existed). This ensured governance code never shipped without full audit trail readiness.

### 4. Evidence Ownership Matrix
**Initial design:** Any module could write observations.
**Correction:** Defined strict ownership: EvidenceBuilder = observations, StrategicLearning = cases, Governance = events, Drift = snapshots, Dataset = batches. Enforced via `owner` column and module-level sole-writer discipline. This prevents evidence contamination and makes audit possible.

### 5. Circuit Breaker as Independent Component
**Initial design:** Circuit breaker logic embedded in Governance.
**Correction:** Extracted `LearningMirrorCircuitBreaker` as standalone module with its own table, state machine (ACTIVE/DEGRADED/REFROZEN), and 24h forced refreeze. This decouples safety from governance logic and allows independent testing.

### 6. OPVAL Contract Versioning
**Initial design:** No contract version on observations.
**Correction:** Added `OPVAL_EVIDENCE_CONTRACT_VERSION` and `contract_version` column. Every derivation validates the contract. This enables future schema evolution without silent data corruption.

## Governance Discoveries

### 1. Promotion Requires Multiple Gates
A strategy promotion is only approved when **ALL** of these hold:
- `StrategyEffectivenessManager.promotion_eligible == True`
- Circuit breaker `is_degraded() == False`
- No critical alignment recommendations pending
- No active demotion recommendations in queue

This multi-gate design prevents premature promotion and was a hard-won simplification from early designs that had complex weighted scoring.

### 2. Retirement Has Two Paths
- **Effectiveness-driven:** `retirement_eligible == True` from metrics
- **Queue-driven:** Active demotion recommendation from alignment monitor

Both paths converge in `LearningGovernance.retire_strategy`. The dual-path design emerged from realizing that effectiveness alone is too slow to catch misalignment.

### 3. Degraded Mode Blocks Only Promotion
Retirement remains available during degraded mode. This was a deliberate safety choice: if the system detects danger, it must still be able to retire bad strategies. The circuit breaker is a **one-way safety valve** for promotions.

### 4. Forced Refreeze Is Not Optional
The 24-hour forced refreeze after degraded mode expiration is a non-negotiable guard. It prevents "temporary" degraded modes from becoming permanent. The breaker transitions from DEGRADED to REFROZEN automatically; no operator action can bypass it.

## Migration Lessons

### 1. Idempotent Migrations Are Non-Negotiable
Every `CREATE TABLE` uses `IF NOT EXISTS`. Every `CREATE INDEX` uses `IF NOT EXISTS`. The test suite runs migrations multiple times. This caught a bug where `learning_cases` was dropped and recreated in M2 — the M3 migration would have failed on re-run.

### 2. Split Migrations By Milestone
M1, M2, M3a, M3b each have their own schema additions. No cross-milestone dependencies in DDL. This allows branching and reordering if needed.

### 3. Real `state.db` Must Remain Untouched
All learning code writes to OPVAL tables only. The OPVAL store is a separate SQLite connection. This isolation was enforced by code review and static analysis — no `state.db` string in any learning module.

## Testing Lessons

### 1. Regression Testing Is the Integration Test
Each milestone's test suite includes all prior milestone tests. This caught the M3b regression on M3a scope isolation (governance table existence tests) and the pre-existing delegate flaky test.

### 2. Adversarial Tests Find Real Bugs
Integration/adversarial tests for each milestone explicitly try to violate constraints:
- Try to promote without effectiveness
- Try to write observations without ownership
- Try to bypass circuit breaker
- Try to mutate governance chain

These tests found multiple logic bugs before audits.

### 3. Pre-existing Flaky Tests Must Be Documented
The delegate heartbeat test fails ~1/20 runs on Windows. It was documented as "pre-existing flaky" in M1 and carried forward. Do not let flaky tests block milestones — attribute, document, and exclude from gate criteria.

### 4. Test Data Seeding Must Match Evidence Builder Logic
Early M3b tests failed because `LearningEvidenceBuilder` derives `strategy_id` from `primary_domain`, ignoring constructor parameter. Tests had to use domain names matching strategy IDs. Always verify the builder's mapping logic.

## Audit Lessons

### 1. Static Analysis Catches What Reviews Miss
AST-based import scanning found zero Milestone 4 imports in Milestone 3 code. Text search found "Milestone 4" in a docstring — acceptable. Automated checks are essential for governance scope enforcement.

### 2. Hash-Chain Verification Must Be Tested
`verify_chain()` is tested for both valid chain and tampered chain. The tamper test modifies a stored event and asserts verification fails. This is the single most important audit test.

### 3. Category A Findings Must Be Resolved Before Next Phase
M3b readiness review found 1 Category A finding (governance unfreeze reevaluation). It was resolved by `LEARNING_GOVERNANCE_STATUS_REASSESSMENT` which confirmed architecture exists. The gate review pipeline works — but only if enforced.

### 4. Evidence Ownership Is Auditable at Rest
The `owner` column and `source_module` column make it trivial to query "who wrote this row." The audit plan explicitly checks ownership boundaries. This turned a design principle into a verifiable property.

## Cross-Cutting Lessons

### 1. Gate Reviews Prevent Scope Creep
The 10-phase pipeline (investigate → design → red team → conditional pass → redesign → red team → plan → gate review → authorize → implement) is heavy but necessary. Every milestone passed through it. No code was written before authorization.

### 2. Closure Artifacts Are Not Optional
Each milestone produces: implementation report, test results, audit artifact, post-implementation review. These become the authoritative record. Without them, you cannot do the next gate review.

### 3. Windows-Specific Issues Appear Late
The `pytest-logfire` plugin conflict and delegate flaky test only appeared on Windows CI. Local Linux development missed them. Test on target platform early.

### 4. Memory/Context Management Scales
The project used Hermes persistent memory for user preferences and project conventions. This allowed seamless context resumption across many sessions without re-reading hundreds of artifacts.
