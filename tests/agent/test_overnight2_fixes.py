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

    # drift_pct/misalignment_pct are on the production 0-100 PERCENT scale
    # (collectors store (n/total)*100): 12% drift, 3% misaligned ->
    # avg_drift=0.12 (fraction), avg_alignment=97.0 (percent).
    for _ in range(5):
        _seed(str(uuid.uuid4()), drift=12.0, misalign=3.0)

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


# ── Hunt-2 #1 (HIGH): an impossible-but-well-formed cron expression (e.g.
# "0 9 31 2 *" = Feb 31) passes croniter.is_valid() but raises CroniterBadDateError
# from get_next(), which crashed the whole scheduler tick (DoS of every job). The
# fix rejects such expressions at validation time AND makes compute_next_run return
# None (disable just that one job) instead of propagating the crash. ───────────────
import pytest

_IMPOSSIBLE_CRON = "0 9 31 2 *"   # 09:00 on Feb 31 — never occurs
_VALID_CRON = "0 9 * * *"          # 09:00 daily


def test_impossible_cron_rejected_at_validation():
    from cron.jobs import _validate_parsed_schedule, HAS_CRONITER
    if not HAS_CRONITER:
        pytest.skip("croniter not installed")
    with pytest.raises(ValueError):
        _validate_parsed_schedule({"kind": "cron", "expr": _IMPOSSIBLE_CRON})
    # a real cron must still validate cleanly
    _validate_parsed_schedule({"kind": "cron", "expr": _VALID_CRON})


def test_impossible_cron_compute_next_run_returns_none_not_raises():
    from cron.jobs import compute_next_run, HAS_CRONITER
    if not HAS_CRONITER:
        pytest.skip("croniter not installed")
    # Pre-fix this raised CroniterBadDateError and aborted the tick.
    assert compute_next_run({"kind": "cron", "expr": _IMPOSSIBLE_CRON}) is None
    # a valid cron still yields a concrete next-run timestamp
    nxt = compute_next_run({"kind": "cron", "expr": _VALID_CRON})
    assert isinstance(nxt, str) and nxt


# ── Hunt-2 #7 & #12: a tz-naive ISO timestamp (no offset) was parsed as host-local
# time instead of UTC, shifting credential-exhaustion cooldowns / nous pool-entry
# expiry ordering by the host's UTC offset. The canonical sibling parsers
# (hermes_cli/auth.py, tools/skill_usage.py) tag naive values as UTC; these two
# did not. Under the suite's pinned TZ=UTC the bug is invisible, so we force a
# non-UTC zone (POSIX tzset) to make the pre-fix code fail. ───────────────────────
import os
import time
from datetime import datetime, timezone

_NAIVE_ISO = "2026-09-01T00:00:00"
_UTC_EPOCH = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()  # 1788220800.0


def _force_non_utc(monkeypatch):
    """Pin local tz to UTC+5:30 (no DST) where the platform supports it.

    Returns True if the host tz was actually changed (POSIX). On Windows there
    is no time.tzset(), so the UTC-correctness invariant is still asserted but
    the naive-vs-local divergence can't be forced; CI (Linux) exercises both.
    """
    if not hasattr(time, "tzset"):
        return False
    monkeypatch.setenv("TZ", "Asia/Kolkata")  # UTC+05:30, DST-free
    time.tzset()
    return True


def test_credential_pool_naive_reset_parsed_as_utc(monkeypatch):
    from agent.credential_pool import _parse_absolute_timestamp
    try:
        _force_non_utc(monkeypatch)
        # Pre-fix on a non-UTC host this returned the host-local epoch (off by
        # the UTC offset); the fix anchors naive values to UTC.
        assert _parse_absolute_timestamp(_NAIVE_ISO) == _UTC_EPOCH
        # tz-aware / Z inputs were always correct and must stay correct.
        assert _parse_absolute_timestamp(_NAIVE_ISO + "+00:00") == _UTC_EPOCH
        assert _parse_absolute_timestamp(_NAIVE_ISO + "Z") == _UTC_EPOCH
    finally:
        if hasattr(time, "tzset"):
            time.tzset()  # restore after monkeypatch reverts TZ


def test_nous_account_naive_expiry_parsed_as_utc(monkeypatch):
    from hermes_cli.nous_account import _parse_iso_timestamp
    try:
        _force_non_utc(monkeypatch)
        assert _parse_iso_timestamp(_NAIVE_ISO) == _UTC_EPOCH
        assert _parse_iso_timestamp(_NAIVE_ISO + "+00:00") == _UTC_EPOCH
        assert _parse_iso_timestamp(_NAIVE_ISO + "Z") == _UTC_EPOCH
    finally:
        if hasattr(time, "tzset"):
            time.tzset()


# ── Hunt-2 #3: _detect_tool_failure raised TypeError on a memory result whose
# 'error' value is null/non-string ('"..." in None'). The key is present so the
# .get("error","") default never applied. Must classify, never raise. ─────────────
def test_detect_tool_failure_never_raises_on_null_error():
    from agent.display import _detect_tool_failure
    for result in (
        '{"success": false, "error": null}',
        '{"success": false, "error": 0}',
        '{"success": false, "error": [1, 2]}',
    ):
        out = _detect_tool_failure("memory", result)  # pre-fix: TypeError
        assert isinstance(out, tuple) and len(out) == 2
        assert isinstance(out[0], bool) and isinstance(out[1], str)
    # real "store full" classification preserved
    assert _detect_tool_failure(
        "memory", '{"success": false, "error": "you exceed the limit"}'
    ) == (True, " [full]")


# ── Hunt-2 #2: enforce_turn_budget sized a multimodal (list) tool result by
# len(list) = number of parts, not characters, so a 250k-char text part counted
# as ~2 and slipped under the 200k turn budget with no protection. ────────────────
def test_enforce_turn_budget_sizes_multimodal_list_by_chars():
    from tools.tool_result_storage import enforce_turn_budget, DEFAULT_BUDGET, _content_char_size
    big = "Q" * 250_000
    msg = {
        "role": "tool", "name": "vision_analyze", "tool_call_id": "c1",
        "content": [
            {"type": "text", "text": big},
            {"type": "image_url", "image_url": {"url": "x"}},
        ],
    }
    assert _content_char_size(msg["content"]) > DEFAULT_BUDGET.turn_budget
    out = enforce_turn_budget([msg], env=None, config=DEFAULT_BUDGET)
    content = out[0]["content"]
    # Pre-fix: nothing changed (early return). Now: oversized text spilled,
    # image part preserved, total back under budget.
    assert _content_char_size(content) <= DEFAULT_BUDGET.turn_budget
    text_parts = [p for p in content if isinstance(p, dict) and "text" in p]
    img_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "image_url"]
    assert big not in text_parts[0]["text"]
    assert len(img_parts) == 1


def test_enforce_turn_budget_string_path_still_works():
    from tools.tool_result_storage import enforce_turn_budget, DEFAULT_BUDGET, PERSISTED_OUTPUT_TAG
    msg = {"role": "tool", "tool_call_id": "s1", "content": "Z" * 250_000}
    out = enforce_turn_budget([msg], env=None, config=DEFAULT_BUDGET)
    assert len(out[0]["content"]) < 250_000  # truncated/persisted


# ── Hunt-2 #4: _content_length_for_budget counted raw base64 for the _multimodal
# DICT envelope (~20x over-count) because it's not a list and fell through to
# len(str(...)). The list shape already strips base64; the two must agree. ─────────
def test_content_length_multimodal_dict_strips_base64():
    from agent.context_compressor import _content_length_for_budget, _IMAGE_CHAR_EQUIVALENT
    b64 = "A" * 120_000
    list_form = [{"type": "image", "source": {"data": b64}}]
    dict_form = {"_multimodal": True, "text_summary": "shot",
                 "content": [{"type": "image", "source": {"data": b64}}]}
    L = _content_length_for_budget(list_form)
    D = _content_length_for_budget(dict_form)
    assert L == _IMAGE_CHAR_EQUIVALENT
    assert D < 10_000  # pre-fix ~120107
    assert abs(D - L) <= len("shot") + 4


# ── Hunt-2 #6: cron SILENT delivery suppression used a case-insensitive SUBSTRING
# match, silently dropping legit reports that merely mention the token. Suppress
# only when [SILENT] is used as a directive (leads / alone on a line). ─────────────
def test_cron_silent_directive_boundary():
    from cron.scheduler import _is_silent_directive
    # directive -> suppress (preserves intended leniency)
    for s in ("[SILENT]", "[SILENT] No changes", "[silent] nothing new",
              "long report...\n\n[SILENT]", "  [Silent]  "):
        assert _is_silent_directive(s) is True, s
    # mere mention -> deliver (the bug)
    for s in ("All systems normal. phone is in [silent] mode right now.",
              "Report: the user set notifications to [SILENT]."):
        assert _is_silent_directive(s) is False, s


# ── Hunt-2 #8: the rate-limit "resets in" parser used \b after each unit, which
# is absent between a unit letter and a digit ("2h30m": h->3), so compact
# multi-unit durations failed to parse. ───────────────────────────────────────────
def test_extract_api_error_compact_duration():
    from agent.agent_runtime_helpers import extract_api_error_context
    import time as _t

    def secs(msg):
        c = extract_api_error_context(Exception(msg))
        return None if "reset_at" not in c else round(c["reset_at"] - _t.time())

    assert abs(secs("Rate limited. Resets in 2h30m.") - 9000) <= 2
    assert abs(secs("Resets in 1h15m") - 4500) <= 2
    assert abs(secs("Resets in 2h 30m") - 9000) <= 2      # spaced form still ok
    assert abs(secs("Resets in 4hr 5min") - 14700) <= 2
    assert abs(secs("Resets in 2hours") - 7200) <= 2      # full word not split at 'h'


# ── Hunt-2 #9: the concurrent-tools heartbeat indexed parsed_calls by future
# position, but futures is built only from runnable_calls (blocked calls
# excluded), so a leading blocked call shifted every reported name. ────────────────
def test_running_tool_names_skips_blocked_offset():
    from agent.tool_executor import _running_tool_names
    parsed_calls = [
        ("tc0", "delete_everything", "a0", "mw0", "BLOCKED", True),
        ("tc1", "read_file", "a1", "mw1", None, False),
        ("tc2", "web_search", "a2", "mw2", None, False),
    ]
    runnable_calls = [
        (i, tc, name, args)
        for i, (tc, name, args, mw, br, bg) in enumerate(parsed_calls)
        if br is None
    ]
    futures = ["F1", "F2"]  # parallel to runnable_calls
    not_done = ["F1", "F2"]
    assert _running_tool_names(not_done, futures, runnable_calls) == ["read_file", "web_search"]


# ── Hunt-2 #10: compress() with abort_on_summary_failure reassigned `messages`
# to the lossily-pruned list BEFORE the abort check, so the "preserved unchanged /
# frozen" path actually returned a mutated transcript. ────────────────────────────
def test_compress_abort_returns_byte_for_byte_original():
    import copy as _copy
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="test/mock", protect_first_n=1, protect_last_n=3,
                          quiet_mode=True, abort_on_summary_failure=True)
    c._generate_summary = lambda *a, **k: None  # force summary failure
    dup = "X" * 900
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do a thing"},
    ]
    for i in range(1, 5):
        messages.append({"role": "assistant",
                         "tool_calls": [{"id": f"t{i}", "function": {"name": "read_file", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "content": dup})
    messages += [
        {"role": "user", "content": "and another"},
        {"role": "assistant", "content": "ok done"},
        {"role": "user", "content": "thanks"},
    ]
    snapshot = _copy.deepcopy(messages)
    out = c.compress(messages, current_tokens=10_000_000, force=True)
    assert c._last_compress_aborted is True
    assert out == snapshot  # byte-for-byte, duplicate tool results not pruned
    assert all(m["content"] == dup for m in out if m.get("role") == "tool")


# ── Hunt-2 #5: Slack markdown-link label was stashed behind a placeholder before
# the escaping pass, so '<'/'>'/'&' in the label broke the <url|label> entity. ─────
def test_slack_link_label_is_escaped():
    import gateway.platforms.slack as s
    f = lambda c: s.SlackAdapter.format_message(None, c)
    assert f("[x > y here](https://x.com)") == "<https://x.com|x &gt; y here>"
    assert f("[Tom & Jerry](https://x.com)") == "<https://x.com|Tom &amp; Jerry>"
    assert f("[a < b](https://x.com)") == "<https://x.com|a &lt; b>"


# ── Hunt-2 #11: Telegram MarkdownV2 step-12 safety net escaped '(' inside a link
# URL, corrupting the target (.../Mercury_\(element) → 404). It must stay bare. ─────
def test_telegram_keeps_url_paren_bare():
    import gateway.platforms.telegram as t
    out = t.TelegramAdapter.format_message(
        None, "See [Mercury (element)](https://en.wikipedia.org/wiki/Mercury_(element))"
    )
    # URL '(' bare, URL ')' escaped; display-text parens escaped
    assert "Mercury_(element" in out
    assert "Mercury_\\(element" not in out
    assert "[Mercury \\(element\\)]" in out
