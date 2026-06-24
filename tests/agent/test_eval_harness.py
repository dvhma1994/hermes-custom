"""Tests for agent/eval_harness.py — portable offline eval scorer + scoreboard."""
import json
import os
import sys
import textwrap

from agent import eval_harness as eh


class TestParse:
    def test_passed_only(self):
        assert eh.parse_pytest_summary("===== 23 passed in 0.04s =====") == \
            {"passed": 23, "failed": 0, "errors": 0}

    def test_passed_and_failed(self):
        c = eh.parse_pytest_summary("2 failed, 21 passed in 1.2s")
        assert c["passed"] == 21 and c["failed"] == 2

    def test_errors(self):
        c = eh.parse_pytest_summary("1 error in 0.1s")
        assert c["errors"] == 1

    def test_empty(self):
        assert eh.parse_pytest_summary("") == {"passed": 0, "failed": 0, "errors": 0}


def _make_project(tmp_path, ok=True):
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "mymod.py").write_text("def add(a, b):\n    return a + b\n")
    oracle = tmp_path / "oracle_test.py"
    expected = "3" if ok else "999"
    oracle.write_text(textwrap.dedent(f"""
        import mymod
        def test_add():
            assert mymod.add(1, 2) == {expected}
    """))
    return str(oracle), str(proj)


class TestScoreProject:
    def test_passing_solution(self, tmp_path):
        oracle, proj = _make_project(tmp_path, ok=True)
        r = eh.score_project(oracle, proj, python_exe=sys.executable, timeout=120)
        assert r.ok is True and r.passed == 1 and r.failed == 0

    def test_failing_solution(self, tmp_path):
        oracle, proj = _make_project(tmp_path, ok=False)
        r = eh.score_project(oracle, proj, python_exe=sys.executable, timeout=120)
        assert r.ok is False and r.failed == 1

    def test_missing_paths_failopen(self):
        r = eh.score_project("/nope/oracle.py", "/nope/project", python_exe=sys.executable)
        assert r.ok is False and "missing" in r.detail


class TestRunEvalsAndIO:
    def test_run_evals_aggregates(self, tmp_path):
        o1, p1 = _make_project(tmp_path / "a" if False else tmp_path, ok=True)
        evals = [eh.EvalDef(name="a", oracle=o1, project=p1)]
        sb = eh.run_evals(evals, python_exe=sys.executable)
        assert sb["summary"]["evals"] == 1
        assert sb["summary"]["tests_passed"] == 1
        assert sb["summary"]["pass_rate"] == 1.0

    def test_load_eval_defs(self, tmp_path):
        p = tmp_path / "defs.json"
        p.write_text(json.dumps([
            {"name": "x", "oracle": "o.py", "project": "proj"},
            {"bad": "missing oracle/project"},
        ]))
        defs = eh.load_eval_defs(str(p))
        assert len(defs) == 1 and defs[0].name == "x"

    def test_load_eval_defs_bad_path(self):
        assert eh.load_eval_defs("/no/such/file.json") == []

    def test_save_scoreboard_appends_jsonl(self, tmp_path):
        path = str(tmp_path / "sub" / "scoreboard.jsonl")
        assert eh.save_scoreboard({"summary": {"pass_rate": 1.0}}, path, timestamp="2026-06-22T00:00:00")
        assert eh.save_scoreboard({"summary": {"pass_rate": 0.9}}, path, timestamp="2026-06-22T01:00:00")
        lines = open(path, encoding="utf-8").read().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["timestamp"] == "2026-06-22T00:00:00"
