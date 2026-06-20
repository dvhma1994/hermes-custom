---
name: orchestrate
description: Fan out parallel workers, synthesize, then verify.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [orchestration, delegation, multi-agent, workflow]
    category: autonomous-ai-agents
---

# Orchestrate Skill

Work like a manager of a fleet, not a lone worker. For any task that decomposes
into independent parts — or that benefits from independent perspectives before
committing — fan the work out to parallel worker subagents via `delegate_task`,
then synthesize their results yourself and adversarially verify the important
ones. This is how to be comprehensive (cover in parallel), confident (cross-check
before acting), and fast (wall-clock = slowest worker, not the sum).

## When to Use
- The task splits into independent sub-tasks (survey N areas, review N files,
  implement N isolated pieces).
- You want independent opinions before committing (design choices, risky claims).
- Discovery of unknown size (find all bugs / all usages) — loop until dry.
- NOT for a single small step you can just do directly.

## Prerequisites
- `delegate_task` (delegation toolset). Batch form runs workers in parallel
  (cap: `delegation.max_concurrent_children`). `role="orchestrator"` lets a
  worker spawn its own sub-workers (up to `delegation.max_spawn_depth`).

## Core Patterns
1. **Fan-out (parallel workers).** Pass a `tasks` list to one `delegate_task`
   call — each entry is one focused worker, all run concurrently:
   ```
   delegate_task(tasks=[
     {goal: "Worker 1: <one focused job>. Return STRUCTURED findings, not raw dumps."},
     {goal: "Worker 2: <another focused job>. Same contract."},
     ... ])
   ```
2. **Synthesize.** YOU combine the workers' returns into one answer — dedup,
   resolve disagreements, decide. Don't just concatenate.
3. **Adversarial verify.** For load-bearing findings/claims, spawn 1-3 skeptic
   workers prompted to REFUTE ("try to prove this wrong; default to refuted if
   unsure"). Keep a finding only if it survives.
4. **Loop-until-dry.** For unknown-size discovery, repeat fan-out rounds until K
   consecutive rounds surface nothing new (dedup against everything seen).
5. **Nested orchestration.** A sub-task that is itself big → give that worker
   `role="orchestrator"` so it fans out its own workers.

## Procedure
1. Decompose the task into 3-N independent worker jobs; write each goal sharply
   (one job per worker, explicit return contract).
2. Fan them out in ONE batch `delegate_task` call.
3. Synthesize the returns.
4. Adversarially verify the high-stakes parts; drop what doesn't survive.
5. If discovery is open-ended, loop until dry. Then report the synthesized,
   verified result.

## Quick Reference
- One `delegate_task(tasks=[...])` = a parallel wave of workers.
- Workers return structured findings; the orchestrator synthesizes.
- Verify before you commit; refute, don't rubber-stamp.
- `role="orchestrator"` for workers that must spawn their own workers.

## Pitfalls
- Sequential delegation when the parts are independent (wastes wall-clock) —
  batch them.
- Over-broad worker goals — one focused job each.
- Trusting a single worker's claim on a high-stakes point — verify.
- Letting workers dump raw output back — demand a structured/citation return.

## Verification
You orchestrated well if the work ran in parallel waves, the final answer is a
synthesis (not a pile of worker outputs), and the load-bearing claims were
independently verified before you acted on them.
