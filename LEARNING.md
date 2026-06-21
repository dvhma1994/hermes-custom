# The self-improvement loop

```
OPVAL telemetry ─▶ observations ─▶ effectiveness ─▶ governance ─▶ applied authority
 (per session)     (evidence)      (win-rate+score)  (hash-chained)  (per session, gated)
```

Gated by `HERMES_OPVAL=1` (collect telemetry) and `HERMES_LEARNING=1` (run the loop).
A complete no-op when unset.

## See it working
```bash
python scripts/learning_status.py          # dashboard: sessions, domains, governance, readiness gates
python scripts/learning_status.py --cycle  # also run one offline digest and print decisions
```

## What was fixed/strengthened in this build
1. **Telemetry recording restored** — `agent/opval/store.py` ran `CREATE INDEX` on new columns
   before the additive migrations (and `_MIGRATIONS` was missing 4 columns), so on a pre-existing
   `state.db` the store raised `no such column` and OPVAL silently recorded nothing. Fixed →
   sessions accumulate again.
2. **Real learning signal** — the effectiveness score read `promotion_score`, which is ~always 0,
   so every strategy scored 0.0 and nothing was ever eligible. The evidence payload now also carries
   `session_quality_score` and `session_tool_correctness` (additive, backward-compatible), and
   `strategy_effectiveness_manager` derives the score from them when promotion_score is unset — so the
   score now *varies* (verified: tool-correctness 0/0.5/1.0 → score 50/75/100) and the loop can
   discriminate sessions instead of seeing a flat 0.

## Honest remaining work (next, in isolation)
The loop is alive, recording, and now has a usable score signal — but two upstream degeneracies still
limit how much it can learn, and both deserve careful isolated work (not a rushed live change):
- **`outcome` is ~always "success"** → `win_rate` is always 1.0. Needs a real success/failure
  classifier per session.
- **`session_quality_score` is ~always 1.0** → only `session_tool_correctness` currently carries
  variance. A richer per-session quality metric would sharpen the signal.

Readiness gates (>=50 sessions, >=5/domain, drift<=15%, misalignment<=10%, score>=80) still gate any
authority change, so the system is safe while the signal matures.
