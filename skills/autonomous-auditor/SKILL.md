---
name: autonomous-auditor
description: Scheduled agent that audits a repo and opens fix PRs.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [automation, code-review, ci]
    category: devops
---

# Autonomous Auditor Skill

A scheduled pipeline that lets Hermes audit a GitHub repository on its own, find
ONE genuine bug per run, fix it, and open a pull request — without ever touching
the live install or pushing to `main`. It does NOT hunt the whole repo blindly
and it will not open a PR it cannot prove.

## When to Use
- You want a recurring, hands-off "find & fix a real bug" agent on a repo.
- You want every autonomous change gated behind a reproduction proof and human PR review.

## Prerequisites
- `gh` authenticated for the target repo (write/`repo` scope).
- Telegram (or another platform) configured for `hermes send` notifications.
- The runner at `C:\Users\ZpLp\hermes_auditor\auditor.py` and launcher at
  `~/.hermes/scripts/auto_audit.py`.

## How to Run
- One real cycle: `python C:\Users\ZpLp\hermes_auditor\auditor.py`
- Safe rehearsal (no push/PR/notify): `python ...\auditor.py --dry-run`
- Scheduled: registered via `hermes cron` running `auto_audit.py` (`--no-agent`).

## Procedure (one cycle)
1. Refresh an **isolated** shallow clone of the target repo (never the live tree).
2. Run `hermes -z` with an honesty-bound prompt: find one genuine bug, prove it
   with a failing test, fix it, branch `auto-audit/<slug>` + commit. If none, it
   prints `NO_BUG_FOUND` and stops.
3. **Adversarial verify** (the key gate): the new test must FAIL on the original
   source and PASS on the fix. If it does not reproduce, the cycle aborts.
4. Rate-limit: skip when an `auto-audit/*` PR is already open, or the branch was
   already handled (`state.json`).
5. Push branch → `gh pr create` (base `main`) → `hermes send` Telegram notice.

## Guardrails
- Isolated clone only; PRs never push to `main`; one open auto-PR at a time.
- Honesty rule (no fabricated bugs) + adversarial reproduction (no false-positive PRs).
- Full logs under `C:\Users\ZpLp\hermes_auditor\logs\`; outcomes in `state.json`.

## Pitfalls
- If `gh` auth expires, push/PR fails — re-run `gh auth login`.
- A long-unreviewed open PR pauses new runs by design (rate-limit). Merge/close it to resume.

## Verification
- `--dry-run` reaching status `dry_run_ok` means clone + audit + reproduction all worked.
- Pause/stop anytime: `hermes cron pause <id>` / `hermes cron remove <id>`.
