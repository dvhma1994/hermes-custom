"""Cross-turn tool-failure oscillation detector.

Hermes has structured recovery for API/transport errors (``agent/error_classifier.py``),
but tool-RESULT failures (a test that keeps failing, a patch that keeps mismatching,
a build that stays broken) only get the error string handed back to the model. When
the model gets stuck — fail -> micro-tweak -> same fail -> micro-tweak -> ... — it
silently burns its whole iteration budget (``max_iterations``, default ~90) and then
gives up with a generic apology. That silent oscillation is the dominant way
long-horizon autonomous sessions die.

This tracker watches the tool results of each loop iteration, fingerprints genuine
failures (the most specific error line + a non-zero failure COUNT, with paths/line
numbers stripped so cosmetic differences collapse but a *decreasing* failure count
does NOT), and once the SAME failure recurs across enough iterations returns a nudge
that forces the SOUL "diagnose, don't retry" move: STOP -> re-read -> hypothesis ->
a DIFFERENT change, or delegate / honest BLOCKED. It re-arms after a clean iteration
and escalates if the model ignores the first nudge and keeps looping.

Pure logic, no IO — unit-tested. The conversation loop feeds it each iteration's tool
results and injects the returned nudge (if any) as a user message. Gated on
``HERMES_OSC_GUARD`` at the call site; fully inert otherwise.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

# Strip hex, decimals-with-optional-unit ("0.30s", "1.5ms"), and standalone ints —
# so a varying run-time or line number collapses — but NOT a digit glued inside an
# identifier ("test_5"), so distinct failing tests stay distinct.
_NUM_RE = re.compile(r"0x[0-9a-fA-F]+|\d+\.\d+\w*|\b\d+\b")
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+|[\w./-]+\.[A-Za-z]{1,5}:\d+")
_WS_RE = re.compile(r"\s+")

# A non-zero test/lint failure count: "2 failed", "3 errors" — but NOT "0 failed".
_FAIL_COUNT_RE = re.compile(r"\b[1-9]\d*\s+(?:failed|errors?)\b", re.I)
# Unambiguous failure markers (scanned anywhere in the body).
_EXPLICIT_SIGNATURES = (
    "traceback (most recent call last)",
    "assertionerror",
    "fatal:",
    "error[",
    "syntaxerror",
    "typeerror",
    "nameerror",
    "importerror",
    "modulenotfounderror",
    "panic:",
    "segmentation fault",
)
# Lines worth fingerprinting on (the real error, not a command echo or a bare count).
_ERROR_LINE_RE = re.compile(r"(?i)(assert|error|exception|traceback|^E\s|^FAILED\b|panic|fatal)")


def _coerce_text(result: Any) -> str:
    """Best-effort flatten a tool result (str, or multimodal content list) to text."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = []
        for part in result:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    return ""


def _is_error_result(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:80].lower()
    if head.startswith("error executing") or head.startswith("error:"):
        return True
    if head.startswith('{"error"'):
        return True
    if '"success": false' in text[:200].lower():
        return True
    low = text.lower()
    if any(sig in low for sig in _EXPLICIT_SIGNATURES):
        return True
    # A non-zero failure count ("2 failed", "3 errors"). "0 failed" is NOT an error,
    # so a finished-green run never registers. A bare quoted "error" token (e.g.
    # {"error": null} or a grep hit on the word) is deliberately NOT treated as a
    # failure — only the genuine {"error": ...} OBJECT head above is.
    return bool(_FAIL_COUNT_RE.search(text))


def _fingerprint(tool_name: str, text: str) -> str:
    """Stable signature for a failure.

    Built from the most specific error line(s) — skipping a noisy command-echo /
    cwd first line — with paths and line numbers stripped so cosmetic differences
    collapse. CRUCIALLY a non-zero failure COUNT is preserved as a tag, so a
    *decreasing* count (2->1, linear progress) yields DIFFERENT signatures and does
    not trip the guard, while a *stuck* count (2->2->2) yields the SAME signature
    and does.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    err_lines = [ln for ln in lines if _ERROR_LINE_RE.search(ln)]
    basis = " ".join(err_lines[:2]) if err_lines else (lines[0] if lines else "")
    basis = _PATH_RE.sub("<path>", basis)
    basis = _NUM_RE.sub("<n>", basis)
    basis = _WS_RE.sub(" ", basis).strip().lower()[:200]
    m = _FAIL_COUNT_RE.search(text)
    count_tag = f"[{m.group(0).split()[0]}f]" if m else ""
    return f"{(tool_name or 'tool').strip()}:{count_tag}{basis}"


class ToolFailureOscillationTracker:
    """Detects a tool failure recurring across iterations and nudges (with escalation).

    ``repeat_threshold``: failing iterations sharing a signature before nudging.
    ``history``: rolling window of recent failing iterations to look within.
    """

    def __init__(self, repeat_threshold: int = 3, history: int = 8):
        self.repeat_threshold = repeat_threshold
        self.history = history
        self._iters: List[Set[str]] = []     # signature set per failing iteration
        self._pending: List[str] = []        # this iteration's failure signatures
        self._fired_at: Dict[str, int] = {}  # sig -> count at its last nudge

    def note(self, tool_name: str, result: Any) -> None:
        """Record one tool result from the current iteration (called per tool)."""
        text = _coerce_text(result)
        if text and _is_error_result(text):
            self._pending.append(_fingerprint(tool_name, text))

    def flush_iteration(self) -> Optional[str]:
        """End the current iteration; return a nudge if a failure is oscillating."""
        pending = set(self._pending)
        self._pending = []
        if not pending:
            # A clean iteration means the loop broke — reset so a future relapse
            # must rebuild the threshold before nudging again (re-arm).
            self._iters.clear()
            self._fired_at.clear()
            return None
        self._iters.append(pending)
        if len(self._iters) > self.history:
            self._iters = self._iters[-self.history:]
        for sig in pending:
            count = sum(1 for s in self._iters if sig in s)
            # Fire at the threshold, and again every further `threshold` failing
            # iterations, so a model that ignores the first nudge gets escalating
            # pressure instead of silence.
            if count >= self.repeat_threshold and count - self._fired_at.get(sig, 0) >= self.repeat_threshold:
                self._fired_at[sig] = count
                return self._nudge(sig)
        return None

    def _nudge(self, sig: str) -> str:
        tool = sig.split(":", 1)[0]
        return (
            f"[System: the `{tool}` failure below has recurred "
            f"{self.repeat_threshold}+ times with no progress — you are oscillating. "
            "STOP repeating the same change. Instead: (1) re-READ the relevant file(s) "
            "and the FULL error; (2) state a concrete hypothesis for the ROOT cause; "
            "(3) make a DIFFERENT change that tests that hypothesis. If you cannot, "
            "delegate a focused sub-task or report BLOCKED with exactly what is stuck "
            "and why. Do not repeat the failing action.]"
        )


def get_osc_tracker(agent) -> ToolFailureOscillationTracker:
    """Lazily attach a per-agent tracker (one oscillation history per session)."""
    tracker = getattr(agent, "_osc_tracker", None)
    if tracker is None:
        tracker = ToolFailureOscillationTracker()
        agent._osc_tracker = tracker
    return tracker
