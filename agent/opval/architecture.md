# Learning Governance V1 — Architecture

## Final Architecture

Learning Governance V1 is a read-only-to-authority learning loop built on top of the Hermes OPVAL evidence store. It observes runtime sessions, derives evidence, recommends authority adjustments, and can finally enact governance decisions when strict safety conditions are met.

## Component Inventory

| # | Component | Module | Responsibility | M2 Authority? | M3 Authority? |
|---|-----------|--------|----------------|---------------|---------------|
| 1 | Runtime Authority | `agent/runtime_authority.py` | Immutable authority knobs, decision container, policy enforcement | No | No |
| 2 | Runtime Feedback | `agent/runtime_feedback.py` | Capture session/turn outcomes into OPVAL | No | No |
| 3 | Learning Constants | `agent/learning_constants.py` | Immutable constants, contract version, ownership tags | No | No |
| 4 | OPVAL Store | `agent/opval/store.py` | Evidence persistence, additive M1-M3b schema | No | No |
| 5 | Evidence Builder | `agent/learning_evidence_builder.py` | Sole writer of `learning_strategy_observations` | No | No |
| 6 | Strategic Learning | `agent/strategic_learning.py` | Applies allowed authority directives; governance hook | No | Apply allowed directives only |
| 7 | Strategy Effectiveness | `agent/strategy_effectiveness_manager.py` | Compute win rate, score, alignment; eligibility flags | No | No |
| 8 | Strategy Generation | `agent/strategy_generation_manager.py` | Pattern-based strategy proposals | No | No |
| 9 | Authority Alignment Monitor | `agent/authority_alignment_monitor.py` | Recommend allowed authority adjustments | No | No |
| 10 | Demotion Recommendations | `agent/learning_demotion_recommendations.py` | Queue strategy demotion/retirement candidates | No | No |
| 11 | Learning Governance | `agent/learning_governance.py` | Append-only governance event chain; promotion/retirement decisions | No | **Yes** — sole authority |
| 12 | Strategy Drift Monitor | `agent/strategy_drift_monitor.py` | Periodic drift snapshots | No | No |
| 13 | Dataset Manager | `agent/learning_dataset_manager.py` | Curate dataset batches and lineage | No | No |
| 14 | Mirror Circuit Breaker | `agent/learning_mirror_circuit_breaker.py` | Safety shutdown and forced refreeze | No | No |

## Authority Boundaries

- **Only `LearningGovernance`** may record promotion, retirement, or enforcement events.
- **Only `LearningEvidenceBuilder`** may write `learning_strategy_observations`.
- **Only `StrategicLearning`** may mutate a `RuntimeAuthority` instance.
- **Only `LearningMirrorCircuitBreaker`** may set degraded/refrozen state.
- **Only `StrategyDriftMonitor`** may write `learning_drift_snapshots`.
- **Only `LearningDatasetManager`** may write `learning_dataset_batches`.
- **M3a components remain recommendation-only**; they never trigger governance actions.

## Governance Flow

```
OPVAL session/turns
       |
       v
LearningEvidenceBuilder  --> learning_strategy_observations (append-only)
       |
       v
StrategyEffectivenessManager  --> effectiveness metrics
       |
       +--> AuthorityAlignmentMonitor  --> allowed authority recommendations
       |
       +--> LearningDemotionRecommendations  --> retirement candidates
       |
       +--> StrategyDriftMonitor  --> drift snapshots / alerts
       |
       v
LearningGovernance (if circuit breaker allows and eligibility holds)
       |
       +--> append governance event to learning_governance_events
       |
       v
StrategicLearning.apply_governance_event (validate + apply allowed knobs)
       |
       v
RuntimeAuthority
```

## Evidence Flow

1. `RuntimeFeedback` records OPVAL sessions and turns.
2. `LearningEvidenceBuilder` verifies contract version and derives observations.
3. Observations carry `source_module`, `owner=learning-governance-v1`, `source_hash`, and `contract_version`.
4. M3a components read observations for metrics and recommendations.
5. M3b governance reads metrics/recommendations before recording decisions.

## Audit Flow

1. Every governance event stores `previous_hash` linking to the prior event.
2. `LearningGovernance.verify_chain()` replays the chain and validates hash continuity.
3. Circuit-breaker transitions are recorded in `learning_mirror_circuit_breaker`.
4. Dataset batches store `source_hash` for lineage.
5. All M3b tables include `owner` and `recorded_at` for audit.

## Key Invariants

- Append-only governance event chain.
- No UPDATE/DELETE on `learning_governance_events`.
- Promotion disabled during circuit-breaker degraded mode.
- 24-hour forced refreeze after degraded mode expires.
- No direct writes to `state.db` from learning code.
- All migrations idempotent (`IF NOT EXISTS`).
