# Hermes — custom build

A personalized fork of [hermes-agent](https://github.com/dvhma1994/hermes-agent) with a wired
self-improvement loop, hardening fixes, and a tuned operating doctrine. Installs and runs exactly
like upstream (same `pyproject.toml` / `setup.py` / `Dockerfile` / desktop bootstrap installer).

## What's different from upstream

**Code**
- **Learning loop wired end-to-end** (flag-gated by `HERMES_LEARNING=1`): runtime authority +
  feedback adaptation, `learning_cycle` (OPVAL → observations → effectiveness → governance →
  applied authority), strategy effectiveness/generation/drift, hash-chained governance, evidence
  builder, mirror circuit breaker. No-op when the flag is unset.
- **OPVAL telemetry fix** (`agent/opval/store.py`): table-aware migrations now run *before* index
  creation and add 4 previously-missing columns — restores session recording on pre-existing DBs.
- **Backup retention fix** (`hermes_state_backup.py`): hot/repair/daily snapshots are now capped by
  count *and* age, and the `repair/` tier (previously never pruned) is cleaned — prevents the
  state.db backup dir from growing without bound.
- Execution checkpoint, compression coordinator, dashboard API, plus a full test suite.

**Operating doctrine (baked into the default SOUL, `hermes_cli/default_soul.py`)**
- Internal thinking protocol, a non-negotiable **Definition-of-Done** self-verification gate,
  **scope-before-start** decomposition for open-ended tasks, and an **output-craft** standard.

**Bundled skills** (`./skills/`): deep-verify, scout, orchestrate, autonomous-auditor, claim-verify,
codebase-onboarding, local-web-research, local-data-explore, mermaid-diagrams, planning-with-files,
progressive-recall, self-eval, structured-output, dependency-analyzer.

## What is NOT included (by design)
No secrets and no personal data: `.env` (API keys/tokens), `state.db` (sessions/messages), backups,
auth tokens, and caches are git-ignored. Copy `.env.example` → `.env` and fill in your own keys.

## Install
Same as upstream. Pick one:

```bash
# from source (Python 3.11+)
git clone <your-private-repo-url> hermes && cd hermes
pip install -e .            # or: python setup.py install

# or Docker
docker build -t hermes . && docker compose up
```

Then configure:
```bash
cp .env.example .env        # fill in your own API keys / tokens
# enable the self-improvement loop (optional but recommended):
#   add HERMES_OPVAL=1 and HERMES_LEARNING=1 to .env
hermes                      # first run seeds the custom SOUL + skills into your HERMES_HOME
```

The desktop installer lives in `apps/bootstrap-installer/` (same as upstream).
