"""Tests for the plan-first nudge in ToolCallGuardrailController (HERMES_PLAN_NUDGE)."""

from agent.tool_guardrails import ToolCallGuardrailController


def _edit(c, path, has_plan=False, tool="write_file"):
    return c.after_call(tool, {"path": path}, "ok", failed=False, has_plan=has_plan)


def test_plan_nudge_fires_after_threshold_distinct_files(monkeypatch):
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    assert _edit(c, "a.py").code != "plan_first_nudge"
    assert _edit(c, "b.py", tool="patch").code != "plan_first_nudge"
    d = _edit(c, "c.py")  # 3rd distinct file, no plan -> nudge
    assert d.code == "plan_first_nudge" and d.action == "warn"
    assert "plan" in d.message.lower()


def test_plan_nudge_suppressed_when_plan_exists(monkeypatch):
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    for p in ("a.py", "b.py", "c.py", "d.py"):
        assert _edit(c, p, has_plan=True).code != "plan_first_nudge"


def test_plan_nudge_off_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_PLAN_NUDGE", raising=False)
    c = ToolCallGuardrailController()
    for p in ("a.py", "b.py", "c.py", "d.py"):
        assert _edit(c, p).code != "plan_first_nudge"


def test_plan_nudge_counts_distinct_paths_only(monkeypatch):
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    for _ in range(5):  # same file 5x -> 1 distinct -> never nudges
        assert _edit(c, "same.py").code != "plan_first_nudge"


def test_plan_nudge_fires_once_per_turn(monkeypatch):
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    fires = sum(
        1 for p in ("a.py", "b.py", "c.py", "d.py", "e.py")
        if _edit(c, p).code == "plan_first_nudge"
    )
    assert fires == 1


def test_plan_nudge_skips_unknown_plan_state(monkeypatch):
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    for p in ("a.py", "b.py", "c.py", "d.py"):  # has_plan=None -> can't confirm -> no nudge
        assert _edit(c, p, has_plan=None).code != "plan_first_nudge"


def test_plan_nudge_only_mutating_tools(monkeypatch):
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    for p in ("a", "b", "c", "d"):  # read_file is not a mutating tool
        assert _edit(c, p, tool="read_file").code != "plan_first_nudge"


def test_plan_nudge_counts_v4a_patch_files(monkeypatch):
    """Review Issue 1: a single V4A patch (mode='patch', no `path` arg) editing
    several files must count them — that bulk case is what the nudge targets."""
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    body = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n@@\n-x\n+y\n"
        "*** Add File: b.py\n+new\n"
        "*** Delete File: c.py\n"
        "*** End Patch\n"
    )
    d = c.after_call("patch", {"mode": "patch", "patch": body}, "ok", failed=False, has_plan=False)
    assert d.code == "plan_first_nudge"  # 3 files in one V4A call -> fires


def test_reset_for_turn_clears_nudge_state(monkeypatch):
    monkeypatch.setenv("HERMES_PLAN_NUDGE", "1")
    c = ToolCallGuardrailController()
    for p in ("a.py", "b.py", "c.py"):
        _edit(c, p)
    c.reset_for_turn()
    # window reset -> a single edit is below threshold again
    assert _edit(c, "x.py").code != "plan_first_nudge"
