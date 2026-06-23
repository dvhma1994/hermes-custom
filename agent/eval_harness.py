"""Portable offline eval harness — score the agent's coding ability against fixture
tasks and track a scoreboard over time.

A fixture eval is a (name, spec, oracle, project) tuple: a hidden oracle test suite is
run against a candidate ``project`` directory (the agent's output, or a reference
solution) and the pass/fail counts are recorded. This generalises the ad-hoc capability
trials into a reusable, 100%-offline regression check the operator/cron can re-run to
catch drift in the model or agent over time.

Design: pure stdlib + subprocess. No hot-path coupling, no network, no hardcoded paths
(eval definitions are passed in or loaded from a JSON file). Fail-open at the harness
level (a broken eval scores as an error, never crashes the run).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

_PASSED = re.compile(r"(\d+) passed")
_FAILED = re.compile(r"(\d+) failed")
_ERRORS = re.compile(r"(\d+) error")


@dataclass
class EvalDef:
    name: str
    oracle: str          # path to the hidden oracle test file
    project: str         # dir put on PYTHONPATH (the candidate solution)
    spec: str = ""       # optional path to the task spec (for re-running the agent)


@dataclass
class EvalResult:
    name: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    total: int = 0
    ok: bool = False
    detail: str = ""


def parse_pytest_summary(text: str) -> Dict[str, int]:
    """Extract {passed, failed, errors} from pytest's textual summary. Robust to the
    line appearing anywhere; missing counts default to 0."""
    def _last(rx):
        m = rx.findall(text or "")
        return int(m[-1]) if m else 0
    return {"passed": _last(_PASSED), "failed": _last(_FAILED), "errors": _last(_ERRORS)}


def score_project(oracle: str, project: str, python_exe: Optional[str] = None,
                  timeout: int = 300) -> EvalResult:
    """Run an oracle test file against a candidate project dir; return counts.

    Deterministic + offline (subprocess pytest). Fail-open: any error → an EvalResult
    with ok=False and a detail message, never raises.
    """
    name = os.path.basename(project.rstrip("/\\")) or "eval"
    try:
        if not os.path.exists(oracle) or not os.path.isdir(project):
            return EvalResult(name=name, ok=False, detail="oracle or project path missing")
        py = python_exe or _default_python()
        env = dict(os.environ)
        env["PYTHONPATH"] = project + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [py, "-m", "pytest", oracle, "-p", "no:logfire", "-p", "no:pytest_logfire",
             "-p", "no:cacheprovider", "-q"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        c = parse_pytest_summary(out)
        total = c["passed"] + c["failed"] + c["errors"]
        ok = c["failed"] == 0 and c["errors"] == 0 and c["passed"] > 0
        return EvalResult(name=name, passed=c["passed"], failed=c["failed"],
                          errors=c["errors"], total=total, ok=ok,
                          detail=out.strip().splitlines()[-1] if out.strip() else "")
    except subprocess.TimeoutExpired:
        return EvalResult(name=name, ok=False, detail=f"timeout after {timeout}s")
    except Exception as e:  # fail-open
        return EvalResult(name=name, ok=False, detail=f"{type(e).__name__}: {e}")


def run_evals(evals: List[EvalDef], python_exe: Optional[str] = None) -> Dict:
    """Score every eval; return a scoreboard dict (no timestamp — caller stamps it)."""
    results = []
    for e in evals:
        r = score_project(e.oracle, e.project, python_exe)
        if e.name:
            r.name = e.name  # label by the eval's name, not the project dir basename
        results.append(r)
    passed = sum(r.passed for r in results)
    total = sum(r.total for r in results)
    return {
        "summary": {
            "evals": len(results),
            "evals_ok": sum(1 for r in results if r.ok),
            "tests_passed": passed,
            "tests_total": total,
            "pass_rate": round(passed / total, 4) if total else 0.0,
        },
        "results": [asdict(r) for r in results],
    }


def load_eval_defs(path: str) -> List[EvalDef]:
    """Load eval definitions from a JSON file: [{name, oracle, project, spec?}, ...].
    Returns [] on any error (fail-open)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict) and d.get("oracle") and d.get("project"):
                out.append(EvalDef(name=d.get("name", ""), oracle=d["oracle"],
                                   project=d["project"], spec=d.get("spec", "")))
        return out
    except Exception:
        return []


def save_scoreboard(scoreboard: Dict, path: str, timestamp: Optional[str] = None) -> bool:
    """Append a stamped scoreboard row to a JSONL history file. Caller supplies the
    timestamp (kept injectable for determinism/testing). Fail-open → False."""
    try:
        row = dict(scoreboard)
        if timestamp is not None:
            row["timestamp"] = timestamp
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _default_python() -> str:
    import sys
    return sys.executable or "python"
