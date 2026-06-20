---
name: self-eval
description: Benchmark Hermes coding ability across models and configs.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [eval, benchmark, quality, regression-gate]
    category: devops
---

# Self-Eval Skill

Measure Hermes's coding ability objectively and compare models/configs, so a
provider swap or prompt change can be gated on real numbers instead of vibes.
Runs a fixed set of self-contained programming tasks with hidden tests; each
candidate solution is executed in an isolated subprocess and scored pass/fail —
no LLM-as-judge, no self-grading. Use it before changing the default model, or
periodically as a regression gate.

## When to Use
- Before/after changing `model` / provider, to confirm capability didn't drop.
- To compare two models head-to-head on the same tasks.
- NOT for grading prose — this measures executable code only.

## Prerequisites
- The harness at `C:\Users\ZpLp\agent_bench\bench.py` (suites: easy / hard / frontier).
- A working `hermes` CLI (the harness drives it via `hermes -z`).

## How to Run
Run the current Hermes config on a suite and score it:
```
python C:\Users\ZpLp\agent_bench\bench.py hermes --suite frontier --tag current
```
Compare a candidate model without changing config (per-invocation override):
```
python C:\Users\ZpLp\agent_bench\bench.py hermes --suite frontier --model deepseek-v4-pro --provider deepseek --tag deepseek_v4
```
Show a side-by-side scoreboard:
```
python C:\Users\ZpLp\agent_bench\bench.py report --suite frontier results_current.json results_deepseek_v4.json
```

## Procedure
1. Establish a baseline: run the current config on `frontier` (the trap-heavy
   suite that actually separates models).
2. Run the candidate (other model/provider/`--style strong`) with a distinct `--tag`.
3. `report` the two result files side by side; treat a drop in any task as a
   regression to investigate before switching.
4. Validate the scorer itself first with `score --file reference_*.json` (should
   be 100%) so you trust the numbers.

## Quick Reference
- Suites: `easy` (40 cases) · `hard` (59) · `frontier` (67).
- Per-invocation overrides: `--model`, `--provider`, `--style strong`.
- Results saved to `results_<tag>.json`; compare with `report`.

## Pitfalls
- Comparing across different suites — always use the same `--suite`.
- Trusting numbers without validating the scorer (run a reference first).

## Verification
A model is safe to adopt when its frontier score matches or beats the current
baseline with no per-task regressions.
