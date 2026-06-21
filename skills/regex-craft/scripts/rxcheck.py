#!/usr/bin/env python3
"""Test a regex against must-match / must-not-match cases before you trust it.

Usage:
  python rxcheck.py '<pattern>' --yes good1 good2 --no bad1 bad2
        [--flags re.I,re.A] [--mode search|fullmatch|match]

Exit code 0 only if every --yes matches and every --no does NOT. Prints a table.
"""
import argparse, re, sys

_FLAGMAP = {"re.I": re.I, "re.IGNORECASE": re.I, "re.A": re.A, "re.ASCII": re.A,
            "re.M": re.M, "re.MULTILINE": re.M, "re.S": re.S, "re.DOTALL": re.S,
            "re.X": re.X, "re.VERBOSE": re.X}


def main(argv=None):
    ap = argparse.ArgumentParser(description="test-first regex checker")
    ap.add_argument("pattern")
    ap.add_argument("--yes", nargs="*", default=[], help="strings that MUST match")
    ap.add_argument("--no", nargs="*", default=[], help="strings that must NOT match")
    ap.add_argument("--flags", default="", help="comma list e.g. re.I,re.A")
    ap.add_argument("--mode", choices=["search", "fullmatch", "match"], default="search")
    a = ap.parse_args(argv)

    flags = 0
    for f in (x.strip() for x in a.flags.split(",") if x.strip()):
        if f not in _FLAGMAP:
            print(f"unknown flag: {f}", file=sys.stderr)
            return 2
        flags |= _FLAGMAP[f]
    try:
        rx = re.compile(a.pattern, flags)
    except re.error as e:
        print(f"INVALID PATTERN: {e}", file=sys.stderr)
        return 2

    test = getattr(rx, a.mode)
    ok = True
    for s in a.yes:
        hit = test(s) is not None
        ok &= hit
        print(f"  [{'PASS' if hit else 'FAIL'}] must-match    {s!r}")
    for s in a.no:
        hit = test(s) is not None
        ok &= (not hit)
        print(f"  [{'PASS' if not hit else 'FAIL'}] must-NOT-match {s!r}")
    print("ALL GOOD" if ok else "TABLE FAILED — fix the pattern, not the table")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
