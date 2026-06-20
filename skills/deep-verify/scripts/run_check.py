"""Run a check command and emit a clean, unambiguous PASS/FAIL verdict.

Usage:
    python run_check.py "<shell command>"        # e.g. "pytest tests/test_x.py -q"
    python run_check.py --timeout 120 "<command>"

Exit code mirrors the check (0 = pass). Designed so the agent gets a crisp
signal instead of scrolling raw output, supporting the deep-verify ladder.
"""
import argparse
import subprocess
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("command", help="The check command to run (quoted)")
    args = ap.parse_args()

    start = time.time()
    try:
        proc = subprocess.run(
            args.command, shell=True, capture_output=True, text=True, timeout=args.timeout
        )
    except subprocess.TimeoutExpired:
        print(f"VERDICT: FAIL (timeout after {args.timeout}s)")
        print(f"COMMAND: {args.command}")
        return 1

    dt = time.time() - start
    verdict = "PASS" if proc.returncode == 0 else "FAIL"
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-15:])
    print(f"VERDICT: {verdict} (exit {proc.returncode}, {dt:.1f}s)")
    print(f"COMMAND: {args.command}")
    print("--- last lines ---")
    print(tail)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
