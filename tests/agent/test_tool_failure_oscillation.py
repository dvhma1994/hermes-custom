"""Tests for the cross-turn tool-failure oscillation detector."""

from types import SimpleNamespace

from agent.tool_failure_oscillation import (
    ToolFailureOscillationTracker,
    _coerce_text,
    _fingerprint,
    _is_error_result,
    get_osc_tracker,
)


def test_is_error_result():
    assert _is_error_result('{"error": "x"}')
    assert _is_error_result("Error executing tool 'patch': boom")
    assert _is_error_result("Traceback (most recent call last):\n  ...")
    assert _is_error_result('{"success": false, "error": "denied"}')
    assert not _is_error_result("5 passed in 0.3s")
    assert not _is_error_result("wrote 10 lines")
    assert not _is_error_result("")


def test_fingerprint_collapses_numbers_and_paths():
    a = _fingerprint("terminal", "AssertionError: expected 5 at /tmp/foo.py:41")
    b = _fingerprint("terminal", "AssertionError: expected 9 at /tmp/bar.py:77")
    assert a == b  # cosmetic differences (numbers, paths) collapse to one loop


def test_fingerprint_distinguishes_real_differences():
    a = _fingerprint("terminal", "AssertionError: wrong value")
    b = _fingerprint("terminal", "ImportError: no module named foo")
    assert a != b


def test_coerce_multimodal_content():
    assert "boom" in _coerce_text([{"type": "text", "text": "boom"}, {"type": "image_url"}])
    assert _coerce_text("plain") == "plain"
    assert _coerce_text(None) == ""


def test_no_nudge_below_threshold():
    t = ToolFailureOscillationTracker(repeat_threshold=3)
    t.note("terminal", "AssertionError: expected 1"); assert t.flush_iteration() is None
    t.note("terminal", "AssertionError: expected 2"); assert t.flush_iteration() is None


def test_nudge_at_threshold_same_signature():
    t = ToolFailureOscillationTracker(repeat_threshold=3)
    t.note("terminal", "AssertionError: expected 5 got 3"); assert t.flush_iteration() is None
    t.note("terminal", "AssertionError: expected 7 got 9"); assert t.flush_iteration() is None
    t.note("terminal", "AssertionError: expected 1 got 2"); nudge = t.flush_iteration()
    assert nudge is not None and "oscillating" in nudge.lower()
    assert "terminal" in nudge


def test_nudge_only_once_per_signature():
    t = ToolFailureOscillationTracker(repeat_threshold=2)
    t.note("patch", "error: no match"); assert t.flush_iteration() is None
    t.note("patch", "error: no match"); assert t.flush_iteration() is not None
    t.note("patch", "error: no match"); assert t.flush_iteration() is None  # already fired


def test_clean_iteration_rearms_the_window():
    """A clean (zero-failure) iteration means the loop broke — reset, so a future
    relapse must rebuild the threshold before nudging again (HIGH-2 re-arm)."""
    t = ToolFailureOscillationTracker(repeat_threshold=2)
    t.note("terminal", "AssertionError: expected 1"); assert t.flush_iteration() is None
    t.note("terminal", "5 passed in 0.1s"); assert t.flush_iteration() is None  # clean -> reset
    # window was reset; one more failing iteration is NOT enough to nudge
    t.note("terminal", "AssertionError: expected 2"); assert t.flush_iteration() is None
    t.note("terminal", "AssertionError: expected 3"); assert t.flush_iteration() is not None


# ── Review fixes: HIGH-1 progress arc · HIGH-2 escalation · MED · LOW ──────────

def test_progress_arc_does_not_false_nudge():
    """HIGH-1: a decreasing failure count (linear progress) must NOT trip the guard —
    the count tag keeps 2-failed / 1-failed / 0-failed as distinct (and 0 is clean)."""
    t = ToolFailureOscillationTracker(repeat_threshold=3)
    for n in (5, 4, 3, 2, 1):
        t.note("terminal", f"{n} failed, {10 - n} passed in 0.3s")
        assert t.flush_iteration() is None
    t.note("terminal", "0 failed, 10 passed in 0.3s")  # green — not even an error
    assert t.flush_iteration() is None


def test_stuck_failure_count_nudges():
    """The flip side: a STUCK count (2 failed, 2 failed, 2 failed) IS oscillation."""
    t = ToolFailureOscillationTracker(repeat_threshold=3)
    assert t.flush_iteration.__doc__  # noqa
    t.note("terminal", "2 failed, 3 passed in 0.3s"); assert t.flush_iteration() is None
    t.note("terminal", "2 failed, 3 passed in 0.4s"); assert t.flush_iteration() is None
    t.note("terminal", "2 failed, 3 passed in 0.2s"); assert t.flush_iteration() is not None


def test_escalation_renudges_when_ignored():
    """HIGH-2: if the model ignores the nudge and keeps looping, escalate (re-nudge
    every `threshold` further failing iterations) instead of going silent."""
    t = ToolFailureOscillationTracker(repeat_threshold=3)
    fires = 0
    for _ in range(6):
        t.note("patch", "error: no matching text found")
        if t.flush_iteration() is not None:
            fires += 1
    assert fires == 2  # at count 3 and again at count 6


def test_zero_failed_is_not_an_error():
    assert _is_error_result("0 failed, 12 passed in 1.2s") is False


def test_json_error_null_not_flagged():
    """MED: a present-but-null error field (or the word 'error' in data) is not a failure."""
    assert _is_error_result('{"rows": [1, 2], "error": null, "ok": true}') is False
    assert _is_error_result('{"matches": ["def handle_error()"]}') is False
    # a genuine error object is still caught
    assert _is_error_result('{"error": "permission denied"}') is True


def test_fingerprint_prefers_error_line_over_noisy_first_line():
    """LOW: a command-echo / cwd first line must not split two identical errors."""
    a = _fingerprint("terminal", "$ cd /a/b && pytest\nAssertionError: mismatch")
    b = _fingerprint("terminal", "$ cd /x/y && pytest\nAssertionError: mismatch")
    assert a == b


def test_distinct_signatures_independent():
    t = ToolFailureOscillationTracker(repeat_threshold=2)
    t.note("terminal", "AssertionError: aaa"); assert t.flush_iteration() is None
    t.note("patch", "error: no match"); assert t.flush_iteration() is None  # different sig, 1 each


def test_get_osc_tracker_is_lazy_and_sticky():
    ag = SimpleNamespace()
    t1 = get_osc_tracker(ag)
    t2 = get_osc_tracker(ag)
    assert t1 is t2 and isinstance(t1, ToolFailureOscillationTracker)
