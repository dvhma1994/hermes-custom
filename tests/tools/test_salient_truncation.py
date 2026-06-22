"""Tests for salience-aware terminal-output truncation (HERMES_SALIENT_TRUNCATION).

Blind head/tail truncation discards the middle of a long test/build log — exactly
where the failure detail lives. _extract_salient_lines recovers it.
"""

from tools.terminal_tool import _extract_salient_lines


def test_extracts_failure_lines_ignores_noise():
    log = (
        "collected 200 items\n"
        "test_a.py ..........\n"
        "FAILED tests/test_b.py::test_y - AssertionError: expected 1 got 2\n"
        "E   assert 1 == 2\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 5\n'
        "lots of noise here\n"
        "3 failed, 197 passed in 4.5s\n"
    )
    d = _extract_salient_lines(log)
    assert "FAILED tests/test_b.py::test_y" in d
    assert "E   assert 1 == 2" in d
    assert "Traceback (most recent call last):" in d
    assert "3 failed, 197 passed" in d
    assert "collected 200 items" not in d
    assert "lots of noise here" not in d


def test_no_salient_returns_empty():
    assert _extract_salient_lines("") == ""
    assert _extract_salient_lines("building...\ncompiling module\ndone successfully") == ""


def test_dedupes_and_bounds_lines():
    assert _extract_salient_lines("\n".join(["E   assert x"] * 100)) == "E   assert x"
    many = "\n".join(f"FAILED test_{i}" for i in range(100))
    assert _extract_salient_lines(many, max_lines=40).count("\n") + 1 <= 40


def test_long_line_capped():
    d = _extract_salient_lines("FAILED " + "x" * 1000)
    assert len(d.splitlines()[0]) <= 300


def test_extracts_diverse_failure_formats():
    """Review Issue 2: go / jest / rust / eslint / pytest-summary failure formats."""
    log = "\n".join([
        "--- FAIL: TestFoo (0.01s)",
        "FAIL\tgithub.com/x/y\t0.5s",
        "thread 'main' panicked at 'boom', src/lib.rs:5",
        "Tests: 1 failed, 4 passed, 5 total",
        "=== 2 failed in 1.3s ===",
        "src/app.ts:5:1: error TS2322: type mismatch",
        "✕ should render",  # jest U+2715
        "ok unrelated line",
    ])
    d = _extract_salient_lines(log)
    assert "--- FAIL: TestFoo" in d
    assert "FAIL\tgithub.com/x/y" in d
    assert "panicked" in d
    assert "Tests: 1 failed" in d
    assert "2 failed in 1.3s" in d
    assert "src/app.ts:5:1: error TS2322" in d
    assert "✕ should render" in d
    assert "ok unrelated line" not in d


def test_colorized_failure_recovered_after_ansi_strip():
    """Review Issue 1: colorized pytest lines (leading ANSI escape) are recovered
    once ANSI is stripped — which the truncation block does before scanning."""
    from tools.ansi_strip import strip_ansi
    colored = "\x1b[31mE   assert 1 == 2\x1b[0m\n\x1b[1m3 failed\x1b[0m in 2s"
    d = _extract_salient_lines(strip_ansi(colored))
    assert "E   assert 1 == 2" in d and "3 failed" in d


def test_middle_failure_recovered_that_blind_truncation_loses():
    """The load-bearing case: a FAILED line buried in the omitted middle of a 60KB
    log is recovered by the salience slice, where blind head/tail would lose it."""
    lines = ["x" * 100] * 600  # ~60KB
    lines[300] = "FAILED tests/test_mid.py::test_z - AssertionError: boom"
    output = "\n".join(lines)
    MAX = 50_000
    budget = MAX - 4000
    head = int(budget * 0.4)
    tail = budget - head
    middle = output[head: len(output) - tail]
    digest = _extract_salient_lines(middle, max_chars=4000)
    assert "FAILED tests/test_mid.py::test_z" in digest          # recovered
    assert "FAILED tests/test_mid.py::test_z" not in (output[:head] + output[-tail:])  # blind loses it
