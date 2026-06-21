"""Regression tests for the overnight-2 hardening pass (verified bugs in the
now-active learning loop + agent core). Each test fails on the pre-fix code.
"""
import json

import agent.learning_constants as lc
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager


def _sem():
    return StrategyEffectivenessManager.__new__(StrategyEffectivenessManager)


# ── Bug #1: turn-level 'recovery' observations were counted as samples, deflating
# win_rate and wrongly retiring strategies whose sessions all succeeded. ──────────
def test_recovery_observations_not_counted_as_samples():
    sem = _sem()

    def sess(outcome=lc.FEEDBACK_OUTCOME_SUCCESS):
        return {"evidence_type": "tool", "outcome": outcome,
                "payload_json": json.dumps({"session_quality_score": 1.0,
                                            "session_tool_correctness": 1.0})}

    def recovery():
        return {"evidence_type": "recovery", "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
                "payload_json": "{}"}

    # 3 fully-successful sessions, each with 2 failing turns (-> 6 recovery obs)
    obs = [sess(), sess(), sess()] + [recovery()] * 6
    m = sem._extract_metrics(obs)
    assert m["sample_count"] == 3            # sessions, not 9
    assert m["win_rate"] == 1.0              # all sessions succeeded; not 0.33
    # therefore NOT retirement-eligible (0.33 would have been <= RETIREMENT_WIN_RATE)
    assert m["win_rate"] > lc.RETIREMENT_WIN_RATE


# ── Bug #2: governance hash-chain forked when two events shared created_at, so
# verify_chain() returned False on the engine's OWN untampered chain (and stuck
# the circuit breaker in DEGRADED). Ordering by rowid (insertion order) fixes it. ──
def test_governance_chain_stable_under_equal_timestamps():
    import agent.learning_governance as g
    import agent.learning_constants as lc
    from agent.opval.store import OpvalStore

    g.LearningGovernance._now = staticmethod(lambda: 1781990000.0)  # force identical created_at
    gov = g.LearningGovernance(OpvalStore(":memory:")._conn)
    ad = g.AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT)
    for i in range(6):
        gov.enforce_policy("s", ad, f"r{i}")
    assert gov.verify_chain("s") is True            # untampered chain must verify
    # tamper detection must still work
    gov._conn.execute(
        "UPDATE learning_governance_events SET source_hash='deadbeef' "
        "WHERE strategy_id='s' AND rowid=(SELECT MIN(rowid)+1 FROM "
        "learning_governance_events WHERE strategy_id='s')"
    )
    assert gov.verify_chain("s") is False


# ── Bug #17: fallback chunk-split used the UTF-16 unit limit as a codepoint slice
# index, so emoji/CJK chunks could be ~2x the platform limit. ──────────────────
def test_split_text_chunks_respects_utf16_limit_for_wide_chars():
    from gateway.stream_consumer import GatewayStreamConsumer
    from gateway.platforms.base import utf16_len
    split = GatewayStreamConsumer._split_text_chunks
    text = "\U0001F4A5" * 350          # 350 emoji, each 2 UTF-16 units, no newlines
    chunks = split(text, 100, len_fn=utf16_len)
    assert all(utf16_len(c) <= 100 for c in chunks)   # was ~200 before the fix
    assert "".join(chunks) == text                    # no data lost
    # ASCII path (len_fn=len) unchanged
    assert all(len(c) <= 100 for c in split("a" * 250, 100))


# ── Bug #12: session_reset at_hour/idle_minutes weren't int-coerced (quoted YAML
# scalar crashed validation); a single exclude string was char-split by tuple(). ──
def test_session_reset_policy_coerces_and_handles_string_exclude():
    from gateway.config import SessionResetPolicy
    p = SessionResetPolicy.from_dict(
        {"at_hour": "4", "idle_minutes": "30", "notify_exclude_platforms": "api_server"})
    assert p.at_hour == 4 and isinstance(p.at_hour, int)
    assert p.idle_minutes == 30 and isinstance(p.idle_minutes, int)
    assert p.notify_exclude_platforms == ("api_server",)   # one platform, not chars
    assert SessionResetPolicy.from_dict({"at_hour": "oops"}).at_hour == 4   # bad -> default


# ── Bug #6: StrategyDriftMonitor.snapshot populated misalignment_pct from
# avg_drift (drift) instead of the misalignment signal (derived from
# avg_alignment).  The persisted misalignment_pct equalled drift_pct. ────────
def test_drift_snapshot_misalignment_not_sourced_from_drift():
    import uuid
    from agent.learning_evidence_builder import LearningEvidenceBuilder
    from agent.opval.store import OpvalStore
    from agent.strategy_drift_monitor import StrategyDriftMonitor

    store = OpvalStore(":memory:")

    def _seed(sid, drift, misalign):
        store.insert_session({
            "session_id": sid, "task_id": "t", "parent_session_id": None,
            "root_session_id": sid, "platform": "test", "primary_domain": "coding",
            "secondary_domains": "[]", "start_time": 1.0, "end_time": 2.0,
            "turn_count": 1, "tool_execution_count": 1, "outcome": "success",
            "session_quality_score": 0.9, "session_tool_correctness": 0.9,
            "drift_pct": drift, "misalignment_pct": misalign,
            "promotion_score": 0.9, "synthetic": 0, "synthetic_reason": None,
            "opval_enabled": 1, "recorded_at": 2.0,
        })
        store.insert_turn({
            "turn_id": str(uuid.uuid4()), "session_id": sid, "turn_number": 1,
            "outcome": "success", "tool_calls": "[]", "tool_outputs": "[]",
            "error_class": None, "latency_ms": 100, "tokens_used": 50,
            "quality_score": 0.9, "tool_execution_score": 0.9,
            "drift_flag": 0, "misalignment_flag": 0, "checkpoint_event": None,
            "started_at": 1.0, "ended_at": 2.0,
        })
        b = LearningEvidenceBuilder(store._conn, "strategy:coding")
        b.build_and_persist(store.get_session(sid), store.get_turns(sid))

    # drift=0.12, misalignment=0.03 -> avg_drift=0.12, avg_alignment=97.0
    for _ in range(5):
        _seed(str(uuid.uuid4()), drift=0.12, misalign=0.03)

    monitor = StrategyDriftMonitor(store._conn)
    snap = monitor.snapshot("strategy:coding")
    eff = monitor._effectiveness.evaluate("strategy:coding")

    expected = max(0.0, 1.0 - eff.avg_alignment / 100.0)
    assert abs(expected - 0.03) < 0.01                         # sanity
    assert abs(snap.misalignment_pct - expected) < 0.01        # must be ~0.03
    assert abs(snap.misalignment_pct - eff.avg_drift) > 0.01   # must NOT be 0.12


# ── Bug #7: count_sessions(synthetic=1) always returned 0 because
# get_eligible_sessions hard-coded WHERE synthetic=0, so the synthetic filter
# in count_sessions operated on an already-empty list. ──────────────────────
def test_count_sessions_synthetic_filter_works():
    import uuid
    from agent.opval.store import OpvalStore

    store = OpvalStore(":memory:")

    def _seed(sid, synthetic):
        store.insert_session({
            "session_id": sid, "task_id": "t", "parent_session_id": None,
            "root_session_id": sid, "platform": "test", "primary_domain": "coding",
            "secondary_domains": "[]", "start_time": 10.0, "end_time": 11.0,
            "turn_count": 3, "tool_execution_count": 2, "outcome": "success",
            "session_quality_score": 0.9, "session_tool_correctness": 0.9,
            "drift_pct": 0.04, "misalignment_pct": 0.04,
            "promotion_score": 0.9, "synthetic": synthetic, "synthetic_reason": None,
            "opval_enabled": 1, "recorded_at": 11.0,
        })

    for _ in range(3):
        _seed(str(uuid.uuid4()), synthetic=0)   # real
    for _ in range(2):
        _seed(str(uuid.uuid4()), synthetic=1)   # synthetic

    assert store.count_sessions() == 5                    # all
    assert store.count_sessions(synthetic=0) == 3         # real only
    assert store.count_sessions(synthetic=1) == 2         # synthetic only


# ── Bug #8: ReadinessGateEngine._estimate_recovery counted sessions instead of
# distinct recovered error-roots, so the rate could exceed 1.0 and inflate the
# RG05 promotion score.  Fix: count distinct roots, clamp to [0, 1]. ─────────
def test_estimate_recovery_counts_distinct_roots_and_clamps():
    from agent.opval.readiness import ReadinessGateEngine

    engine = ReadinessGateEngine.__new__(ReadinessGateEngine)
    root_error_sessions = {"err1", "err2", "err3", "err4", "err5"}  # 5 error roots
    # err1 has 3 successful child sessions, err3 has 2, err4 has 1
    # -> 3 distinct recovered roots / 5 total = 0.60
    eligible = [
        {"session_id": "s1", "root_session_id": "err1", "outcome": "success"},
        {"session_id": "s2", "root_session_id": "err1", "outcome": "success"},
        {"session_id": "s3", "root_session_id": "err1", "outcome": "success"},
        {"session_id": "s4", "root_session_id": "err3", "outcome": "success"},
        {"session_id": "s5", "root_session_id": "err3", "outcome": "success"},
        {"session_id": "s6", "root_session_id": "err4", "outcome": "success"},
    ]
    rate = engine._estimate_recovery(eligible, root_error_sessions)
    assert rate == 0.60                  # 3 distinct roots / 5, not 6/5 = 1.2
    assert 0.0 <= rate <= 1.0


# ── Bug #9: allow_self_delegate=True silently re-enabled self-delegation even when
# the policy forbade it (no_self_delegate). More-restrictive must win. ───────────
def test_no_self_delegate_policy_not_clobbered_by_allow_true():
    import agent.runtime_authority as ra

    class _A:
        pass

    a = _A()
    ra.apply_decision(a, ra.AuthorityDecision(
        policy=ra.AUTHORITY_POLICY_NO_SELF_DELEGATE, allow_self_delegate=True))
    assert a._authority_delegation_policy == ra.AUTHORITY_POLICY_NO_SELF_DELEGATE
    # explicit deny still restricts under a permissive policy
    b = _A()
    ra.apply_decision(b, ra.AuthorityDecision(
        policy=ra.AUTHORITY_POLICY_PERMISSIVE, allow_self_delegate=False))
    assert b._authority_delegation_policy == ra.AUTHORITY_POLICY_NO_SELF_DELEGATE


# ── Bug #14: filter_tool_scope crashed on a tool dict whose 'function' is None. ──
def test_filter_tool_scope_handles_none_function():
    import agent.runtime_authority as ra
    kept = ra.filter_tool_scope([{"function": None, "name": "mytool"}], frozenset({"mytool"}))
    assert len(kept) == 1
