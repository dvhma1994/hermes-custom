"""Regression tests for the Hunt-3 hardening pass (live agent core + subsystems).

Each test fails on the pre-fix code. Run with the logfire pytest plugins
disabled if the local opentelemetry/logfire versions have drifted:

    pytest tests/agent/test_hunt3_fixes.py -p no:logfire -p no:pytest_logfire
"""
import json

import pytest


# ── H3-1: _hydrate_todo_store crashed (TypeError) on a history tool message whose
# content is null (or a multimodal list) — '"todos" not in None'. ──────────────────
def test_hydrate_todo_store_skips_null_content():
    import run_agent

    class _Stub:
        quiet_mode = True
        log_prefix = ""
        _todo_store = type("T", (), {"write": lambda self, *a, **k: None})()
        def _vprint(self, *a, **k):
            pass

    stub = _Stub()
    # Pre-fix: TypeError. Now: null/list content skipped, real todo replayed.
    run_agent.AIAgent._hydrate_todo_store(stub, [{"role": "tool", "content": None}])
    run_agent.AIAgent._hydrate_todo_store(stub, [{"role": "tool", "content": [{"type": "text", "text": "x"}]}])
    run_agent.AIAgent._hydrate_todo_store(stub, [
        {"role": "tool", "content": json.dumps({"todos": [{"id": 1, "text": "a", "status": "pending"}]})},
    ])


# ── H3-2: get_messages_as_conversation ordered by wall-clock timestamp, so a
# backward clock step reordered the replayed transcript. Must use insertion id. ─────
def test_get_messages_as_conversation_orders_by_id(tmp_path):
    import hermes_state
    db = hermes_state.SessionDB(tmp_path / "state.db")
    db.create_session("s1", "cli")
    id1 = db.append_message("s1", "user", "first")
    id2 = db.append_message("s1", "assistant", "second")
    # Simulate a backward clock step: make the 2nd row's timestamp earlier.
    with db._lock:
        db._conn.execute("UPDATE messages SET timestamp = ? WHERE id = ?", (1.0, id2))
        db._conn.execute("UPDATE messages SET timestamp = ? WHERE id = ?", (2.0, id1))
        db._conn.commit()
    conv = db.get_messages_as_conversation("s1")
    roles_contents = [(m.get("role"), m.get("content")) for m in conv]
    assert roles_contents == [("user", "first"), ("assistant", "second")]


# ── H3-3 (security): hardline blocklist (rm -rf /, dd to raw device, shutdown)
# was bypassed by quoting the path/command word. The normalizer only stripped
# EMPTY quote pairs. ───────────────────────────────────────────────────────────────
def test_hardline_not_bypassed_by_quoting():
    import sys
    sys.argv = ["x"]
    from tools.approval import detect_hardline_command as d
    for cmd in ('rm -rf "/"', "rm -rf '/'", 'dd if=/dev/zero of="/dev/sda"', '"shutdown" now'):
        blocked, _ = d(cmd)
        assert blocked is True, f"quoted form bypassed hardline: {cmd!r}"
    # unquoted still blocked; benign commands still pass
    assert d("rm -rf /")[0] is True
    assert d('echo "hello world"')[0] is False
    assert d("git status")[0] is False


# ── H3-4: a failure inside compress_context's `except TypeError` fallback was not
# caught by the sibling `except BaseException`, leaking the compression lock and
# permanently stalling the session's compression. ─────────────────────────────────
def test_compress_context_releases_lock_when_fallback_raises():
    from agent import conversation_compression as cc

    class FakeDB:
        def __init__(self):
            self.released = []
        def try_acquire_compression_lock(self, sid, holder):
            return True
        def release_compression_lock(self, sid, holder):
            self.released.append((sid, holder))
        def get_compression_lock_holder(self, sid):
            return None

    class FakeCompressor:
        def __init__(self):
            self.calls = 0
        def compress(self, messages, current_tokens=None, focus_topic=None, force=False):
            self.calls += 1
            if self.calls == 1:
                raise TypeError("strict plugin signature rejects focus_topic/force")
            raise RuntimeError("summarizer LLM failed inside the fallback")

    class FakeAgent:
        model = "test/mock"
        session_id = "s1"
        _compression_feasibility_checked = True
        _memory_manager = None
        def __init__(self):
            self._session_db = FakeDB()
            self.context_compressor = FakeCompressor()
        def _emit_status(self, *a, **k):
            pass
        def _emit_warning(self, *a, **k):
            pass
        def _build_system_prompt(self, sm):
            return "sp"

    agent = FakeAgent()
    with pytest.raises(RuntimeError):
        cc.compress_context(agent, [{"role": "user", "content": "hi"}], "sys")
    # Pre-fix: released == [] (lock leaked). Now the lock is released.
    assert agent._session_db.released, "compression lock was leaked on fallback failure"


# ── H3-7: drift_pct/misalignment_pct are stored on a 0-100 PERCENT scale, but
# _extract_metrics treated them as 0-1 fractions: alignment collapsed to 0 for any
# misaligned turn (vs the 65 gate) and drift was ~100x inflated (vs the 0.15 gate),
# making promotion mathematically impossible. A clean strategy must now promote. ────
def test_extract_metrics_units_allow_promotion(tmp_path):
    import uuid
    from agent.opval.store import OpvalStore
    from agent.learning_evidence_builder import LearningEvidenceBuilder
    from agent.strategy_effectiveness_manager import StrategyEffectivenessManager

    store = OpvalStore(":memory:")

    def seed(sid, misalign_pct, drift_pct):
        store.insert_session({
            "session_id": sid, "task_id": "t", "parent_session_id": None,
            "root_session_id": sid, "platform": "test", "primary_domain": "coding",
            "secondary_domains": "[]", "start_time": 1.0, "end_time": 2.0,
            "turn_count": 1, "tool_execution_count": 1, "outcome": "success",
            "session_quality_score": 0.9, "session_tool_correctness": 0.9,
            "drift_pct": drift_pct, "misalignment_pct": misalign_pct,
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
        LearningEvidenceBuilder(store._conn, "strategy:coding").build_and_persist(
            store.get_session(sid), store.get_turns(sid)
        )

    # 4 clean sessions: 10% misaligned, 0 drift, all success (percent scale).
    for _ in range(4):
        seed(str(uuid.uuid4()), misalign_pct=10.0, drift_pct=0.0)

    eff = StrategyEffectivenessManager(store._conn).evaluate("strategy:coding")
    assert eff.avg_alignment == pytest.approx(90.0, abs=0.01)  # not 0
    assert eff.avg_drift == pytest.approx(0.0, abs=1e-9)        # not 0..100
    assert eff.win_rate == 1.0
    assert eff.promotion_eligible is True  # was impossible pre-fix


# ── H3-8: inline runtime-tool branches lacked an except-handler, so a raising
# tool aborted the whole turn. The shared middleware wrapper now converts a tool
# Exception to an error result (but lets KeyboardInterrupt propagate). ─────────────
def test_inline_tool_exception_becomes_error_result():
    from agent import tool_executor as te

    class _Agent:
        session_id = "s"
        _current_turn_id = "t"
        _current_api_request_id = "r"

    def boom(args):
        raise ValueError("bad task")

    res, obs = te._run_agent_tool_execution_middleware(
        _Agent(), function_name="delegate_task", function_args={"goal": "x"},
        effective_task_id="t", tool_call_id="c1", execute=boom,
    )
    assert json.loads(res)["error"].startswith("Tool 'delegate_task' failed")
    assert obs == {"goal": "x"}

    def interrupt(args):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):  # interrupts must still propagate
        te._run_agent_tool_execution_middleware(
            _Agent(), function_name="memory", function_args={},
            effective_task_id="t", tool_call_id="c", execute=interrupt,
        )


# ── H3-9: Bedrock image content was passed as the base64 STRING; boto3
# re-encodes it -> doubly-encoded/corrupt. Must be decoded bytes. ──────────────────
def test_bedrock_image_bytes_are_decoded():
    import base64
    from agent.bedrock_adapter import _convert_content_to_converse
    blocks = _convert_content_to_converse(
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]
    )
    src = blocks[0]["image"]["source"]["bytes"]
    assert isinstance(src, bytes)
    assert src == base64.b64decode("iVBORw0KGgo=")
    # malformed base64 -> graceful text fallback, no crash
    bad = _convert_content_to_converse(
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,@@@notb64@@@"}}]
    )
    assert "text" in bad[0]


# ── H3-10: email header/body decode raised LookupError on an unknown charset
# label, aborting the whole IMAP poll batch. Must decode best-effort. ──────────────
def test_email_unknown_charset_does_not_raise():
    from gateway.platforms.email import _decode_header_value, _safe_bytes_decode
    assert "Hello" in _decode_header_value("=?unknown-8bit?Q?Hello?=")
    assert _decode_header_value("=?utf-8?Q?Caf=C3=A9?=") == "Café"
    # raw byte decode tolerates unknown labels (latin-1 fallback)
    assert _safe_bytes_decode(b"\xff\xfe", "ks_c_5601-1987")  # no raise


# ── H3-12: repair_message_sequence read only tool_call['id'], dropping the tool
# result for Codex/Responses-shape tool_calls that carry only 'call_id'. ──────────
def test_repair_message_sequence_keeps_call_id_results():
    from agent.agent_runtime_helpers import repair_message_sequence
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"call_id": "call_abc", "type": "function",
                                              "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "result-data"},
    ]
    repairs = repair_message_sequence(None, msgs)
    assert repairs == 0
    assert [m for m in msgs if m.get("role") == "tool"][0]["content"] == "result-data"
    # a genuine orphan is still dropped
    assert repair_message_sequence(None, [{"role": "tool", "tool_call_id": "nope", "content": "x"}]) == 1


# ── H3-5: Signal mention offsets are UTF-16 code units; slicing the str by Python
# codepoints corrupted text when an astral char preceded the mention. ──────────────
def test_signal_render_mentions_utf16_offsets():
    from gateway.platforms.signal import _render_mentions
    # 😀 = 1 codepoint but 2 UTF-16 units; the ￼ placeholder is at unit index 2
    out = _render_mentions("\U0001F600￼ hi", [{"start": 2, "length": 1, "number": "+15551234567"}])
    assert "￼" not in out                       # placeholder replaced
    assert out == "\U0001F600@+15551234567 hi"        # correct location, ' hi' intact
    # BMP-only path unaffected
    assert _render_mentions("hi ￼", [{"start": 3, "length": 1, "number": "+1555"}]) == "hi @+1555"


# ── H3-6: archive_skill stored under the on-disk DIR name but restore_skill
# searched by frontmatter name, so a skill whose dir != frontmatter name could
# never be restored. restore now matches by frontmatter name. ─────────────────────
def test_skill_archive_restore_roundtrip_by_frontmatter_name():
    import tools.skill_usage as su
    skills = su._skills_dir()
    skills.mkdir(parents=True, exist_ok=True)
    sd = skills / "my-dir"
    sd.mkdir()
    (sd / "SKILL.md").write_text("---\nname: cool-skill\ndescription: x\n---\nbody\n", encoding="utf-8")
    su.mark_agent_created("cool-skill")
    ok, _ = su.archive_skill("cool-skill")
    assert ok
    ok2, msg = su.restore_skill("cool-skill")  # pre-fix: (False, 'not found in archive')
    assert ok2, msg
    assert su.get_record("cool-skill")["state"] == su.STATE_ACTIVE


# ── H3-11 (security): profile-scoped env writes mutated the shared process
# os.environ, leaking/clobbering credentials across profiles. ─────────────────────
def test_env_write_does_not_leak_across_profiles(tmp_path, monkeypatch):
    import importlib
    active = tmp_path / "active"
    other = tmp_path / "other"
    active.mkdir()
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(active))
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    import hermes_cli.config as cfg
    importlib.reload(cfg)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    key = "HUNT3_LEAK_KEY"
    monkeypatch.delenv(key, raising=False)

    cfg.save_env_value(key, "active_val")            # active profile -> os.environ updated
    assert __import__("os").environ.get(key) == "active_val"

    tok = set_hermes_home_override(str(other))        # scope to a DIFFERENT profile
    try:
        cfg.save_env_value(key, "scoped_val")
    finally:
        reset_hermes_home_override(tok)
    # process env must be untouched by the scoped write
    assert __import__("os").environ.get(key) == "active_val"
    assert "scoped_val" in (other / ".env").read_text()


# ── H3-30 (security): _remove_session_files interpolated session_id straight into
# filesystem paths, allowing path-traversal deletion outside sessions_dir. ─────────
def test_remove_session_files_rejects_traversal(tmp_path):
    import hermes_state as hs
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("secret")
    legit = sessions / "good.json"
    legit.write_text("x")
    hs.SessionDB._remove_session_files(sessions, "../victim")  # pre-fix: deletes victim
    assert victim.exists()
    hs.SessionDB._remove_session_files(sessions, "good")       # legit cleanup still works
    assert not legit.exists()


# ── H3-15: _summarize_tool_result crashed (AttributeError) when tool arguments
# JSON-decoded to a non-object (scalar/array), aborting compaction. ────────────────
def test_summarize_tool_result_non_dict_args():
    from agent.context_compressor import _summarize_tool_result
    for args in ('"a string"', "42", "[1, 2]", "null"):
        out = _summarize_tool_result("read_file", args, "content")  # must not raise
        assert isinstance(out, str)


# ── H3-13: a text-only list tool-result was json.dumps()'d (leaking OpenAI part
# structure) instead of converted to Anthropic text blocks. ───────────────────────
def test_anthropic_text_only_list_tool_result_converted():
    from agent.anthropic_adapter import convert_messages_to_anthropic
    _system, msgs = convert_messages_to_anthropic([
        {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "real result text"}]},
    ])
    tr = None
    for m in msgs:
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tr = b["content"]
    # proper Anthropic text blocks, NOT a json.dumps'd string
    assert isinstance(tr, list)
    assert any(isinstance(b, dict) and b.get("type") == "text" and b.get("text") == "real result text" for b in tr)


# ── H3-17: empty eligible_session_ids fell back to SQL '1=1', aggregating stats
# over ALL non-synthetic sessions instead of zero. ────────────────────────────────
def test_rolling_turn_stats_empty_eligible_is_zero():
    from agent.opval.store import OpvalStore
    st = OpvalStore(":memory:")
    st.insert_session({"session_id": "s1", "task_id": "t", "parent_session_id": None,
        "root_session_id": "s1", "platform": "p", "primary_domain": "c", "secondary_domains": "[]",
        "start_time": 100.0, "end_time": 200.0, "turn_count": 1, "tool_execution_count": 1,
        "outcome": "success", "session_quality_score": 0.9, "session_tool_correctness": 0.9,
        "drift_pct": 0, "misalignment_pct": 0, "promotion_score": 0.9, "synthetic": 0,
        "synthetic_reason": None, "opval_enabled": 1, "recorded_at": 200.0})
    st.insert_turn({"turn_id": "tt", "session_id": "s1", "turn_number": 1, "outcome": "success",
        "tool_calls": "[]", "tool_outputs": "[]", "error_class": None, "latency_ms": 10,
        "tokens_used": 5, "quality_score": 0.9, "tool_execution_score": 0.9, "drift_flag": 1,
        "misalignment_flag": 1, "checkpoint_event": None, "started_at": 100.0, "ended_at": 150.0})
    assert st.get_rolling_turn_stats(0.0, 1000.0, eligible_session_ids=set())["total_turns"] == 0
    assert st.get_rolling_turn_stats(0.0, 1000.0, eligible_session_ids=None)["total_turns"] >= 1


# ── H3-20: lineage-based get_compression_tip was shadowed by a second same-named
# method, so compression_lineage records were never consulted. ────────────────────
def test_get_compression_tip_uses_lineage(tmp_path):
    import hermes_state as hs
    db = hs.SessionDB(tmp_path / "state.db")
    db.create_session("A", "cli")
    db.create_session("B", "cli")
    db.insert_compression_lineage("B", "A", "strategy", "auto", 100, 40, "hash", "tok")
    assert db.get_compression_tip("A") == "B"
    db.create_session("Z", "cli")
    assert db.get_compression_tip("Z") == "Z"


# ── H3-0: memory content containing the entry delimiter ("\n§\n") split one entry
# into many on reload (data corruption the drift guard couldn't detect). ──────────
def test_memory_rejects_entry_delimiter():
    import tools.memory_tool as mt
    store = mt.MemoryStore()
    r = store.add("memory", "Project layout:\n§\nUse the build script")
    assert r["success"] is False and "delimiter" in r["error"].lower()
    # normal content round-trips as exactly one entry
    assert store.add("memory", "Project layout: use the build script")["success"]
    store2 = mt.MemoryStore()
    store2.load_from_disk()
    assert len(store2._entries_for("memory")) == 1


# ── H3-19: repair_message_sequence reset known ids on every assistant, so two
# back-to-back assistant(tool_calls) turns dropped the FIRST turn's tool result
# (it arrives after the second turn). Accumulate ids across consecutive turns. ─────
def test_repair_message_sequence_back_to_back_assistants():
    from agent.agent_runtime_helpers import repair_message_sequence
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "a1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "assistant", "tool_calls": [{"id": "b1", "function": {"name": "g", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a1", "content": "RA"},
        {"role": "tool", "tool_call_id": "b1", "content": "RB"},
    ]
    assert repair_message_sequence(None, msgs) == 0
    assert [m["content"] for m in msgs if m.get("role") == "tool"] == ["RA", "RB"]
    # genuine orphan still dropped
    assert repair_message_sequence(None, [{"role": "user", "content": "x"},
                                          {"role": "tool", "tool_call_id": "nope", "content": "o"}]) == 1


# ── H3-39: convert_to_trajectory_format raised KeyError when an assistant/user
# message omits the 'content' key (reasoning-only / content-stripped turn). ────────
def test_convert_to_trajectory_format_missing_content():
    from agent.agent_runtime_helpers import convert_to_trajectory_format

    class _Stub:
        def _format_tools_for_system_message(self):
            return ""

    traj = convert_to_trajectory_format(_Stub(), [
        {"role": "assistant", "reasoning": "thinking"},  # no 'content' key
        {"role": "user"},                                 # no 'content' key
    ], "q", True)
    assert isinstance(traj, list)


# ── H3-25: is_drift_alert recorded a snapshot every call; _baseline_win_rate
# averages snapshots, so polling dragged the baseline toward current win_rate and
# self-extinguished a real sustained drift. is_drift_alert is now read-only. ──────
def test_is_drift_alert_does_not_self_extinguish(tmp_path):
    import uuid
    from agent.opval.store import OpvalStore
    from agent.learning_evidence_builder import LearningEvidenceBuilder
    from agent.strategy_drift_monitor import StrategyDriftMonitor

    st = OpvalStore(":memory:")

    def seed(succ):
        sid = str(uuid.uuid4())
        oc = "success" if succ else "failure"
        st.insert_session({"session_id": sid, "task_id": "t", "parent_session_id": None,
            "root_session_id": sid, "platform": "p", "primary_domain": "coding",
            "secondary_domains": "[]", "start_time": 1.0, "end_time": 2.0, "turn_count": 1,
            "tool_execution_count": 1, "outcome": oc, "session_quality_score": 0.9,
            "session_tool_correctness": 0.9, "drift_pct": 0, "misalignment_pct": 0,
            "promotion_score": 0.9, "synthetic": 0, "synthetic_reason": None,
            "opval_enabled": 1, "recorded_at": 2.0})
        st.insert_turn({"turn_id": str(uuid.uuid4()), "session_id": sid, "turn_number": 1,
            "outcome": oc, "tool_calls": "[]", "tool_outputs": "[]", "error_class": None,
            "latency_ms": 10, "tokens_used": 5, "quality_score": 0.9, "tool_execution_score": 0.9,
            "drift_flag": 0, "misalignment_flag": 0, "checkpoint_event": None,
            "started_at": 1.0, "ended_at": 2.0})
        LearningEvidenceBuilder(st._conn, "strategy:coding").build_and_persist(
            st.get_session(sid), st.get_turns(sid))

    for _ in range(5):
        seed(True)
    m = StrategyDriftMonitor(st._conn)
    m.snapshot("strategy:coding")          # record baseline at win_rate 1.0
    for _ in range(5):
        seed(False)                         # win_rate drops to 0.5
    assert all(m.is_drift_alert("strategy:coding") for _ in range(8))  # persists

# NOTE: H3-23 (_deliver_result retry-fallback abort), H3-28 (_submit_with_guard
# id leak on submit failure), and H3-31 (no_agent workdir os.chdir → subprocess
# cwd) are covered by tests/cron/test_scheduler.py (136 pass) — their seams live
# inside the tick/delivery machinery and are exercised end-to-end there.


# ── H3-14 (security): detect_dangerous_command returned only the FIRST matching
# pattern, so approving one common pattern (e.g. "recursive delete") for the
# session silently authorized a different destructive op bundled into the same
# command. Multi-pattern commands now get a COMBINED key. ──────────────────────────
def test_dangerous_command_multi_pattern_composite_key():
    import sys
    sys.argv = ["x"]
    from tools.approval import detect_dangerous_command as d
    is_d, key, desc = d("rm -rf node_modules && git push origin main --force")
    assert is_d
    _, single_key, _ = d("rm -rf build")
    assert key != single_key            # composite != single -> no cross-approval
    assert "+" in key and ";" in desc   # combined key + combined description
    # single-pattern commands keep their plain key
    assert d("rm -rf build")[1] == d("rm -rf build")[2]
    assert d("ls -la")[0] is False


# ── H3-35: on detected external drift the mutation is correctly aborted, but
# _reload_target had already overwritten live in-memory entries with parsed disk
# content — diverging the session state. Live state must be left unchanged. ────────
def test_memory_reload_preserves_state_on_drift():
    import tools.memory_tool as mt
    store = mt.MemoryStore()
    store.add("memory", "original entry A")
    before = list(store._entries_for("memory"))
    # externally corrupt the on-disk file with an over-limit entry (triggers drift)
    path = store._path_for("memory")
    limit = store._char_limit("memory")
    path.write_text("X" * (limit + 500), encoding="utf-8")
    r = store.add("memory", "entry B")            # detects drift -> aborts
    assert r.get("success") is False
    assert list(store._entries_for("memory")) == before  # live state untouched
