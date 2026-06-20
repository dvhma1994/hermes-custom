---
name: deep-verify
description: Prove a fix with a reproducing check before saying done.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [reliability, testing, verification, agent-loop]
    category: devops
---

# Deep-Verify Skill

Before declaring any code or tool change "done", prove it with a reproducing
check. This is the single technique that most separates reliable 2026 agents
from guess-and-hope ones: a deterministic FAIL→PASS gate beats an LLM's opinion
that the fix "looks right". Use it for bug fixes, refactors, and any change with
a checkable outcome. Do NOT use it for pure prose/answer tasks where there is
nothing executable to assert.

## When to Use
- You changed code to fix a bug or add behavior and want certainty it works.
- You are about to tell the user "fixed" / "done" on something executable.
- An autonomous task (audit, migration) must not ship an unverified change.

## Prerequisites
- A way to run the relevant check: the project's test command, a script, or a
  one-off repro you write. Use the `terminal` tool to run it.

## How to Run
1. Run the helper to capture a clean pass/fail verdict for a check command:
   `python <skill_dir>/scripts/run_check.py "<check command>"`
2. Or run the check directly with `terminal` and read the exit code.

## Procedure (reproduce → fix → re-verify)
1. **Reproduce first.** Before fixing, write or identify a check (a test, an
   assertion, a CLI invocation) that FAILS because of the bug. If you cannot
   make it fail, you have not understood the bug — stop and investigate with
   `search_files` / `read_file`.
2. **Fix at the source.** Make the minimal change; do not edit the check to make
   it pass.
3. **Re-verify.** Run the same check — it must now PASS. Then run the surrounding
   suite (or related checks) to confirm no regression.
4. **Adversarial reproduction (for autonomous changes).** Re-run the new check
   against the ORIGINAL code (e.g. `git stash` or `git checkout -- <file>`): it
   must FAIL there and PASS on the fix. If it passes on the original, it is not a
   real bug — discard the change.
5. **Layered ladder.** Prefer deterministic checks (tests, exit codes, type
   checks). Only fall back to an LLM-judge when no deterministic check is
   possible, and say so explicitly.

## Quick Reference
- Reproduce: make it fail on purpose first.
- Fix the source, never the test.
- Re-verify: fails-on-old AND passes-on-new.
- Report the actual command output, not a claim.

## Pitfalls
- Editing the test to go green instead of fixing the bug.
- Claiming "all tests pass" without showing the command + output.
- Skipping the regression run after the targeted fix.

## Verification
The change is "done" only when: the targeted check passes, the broader suite is
green, and (for autonomous work) the check provably failed on the original code.
Quote the before/after output in your summary.
