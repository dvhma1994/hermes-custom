#!/usr/bin/env python3
"""boundary_probes.py - stdlib-only demonstrations of the eight boundary-bug classes.

Run it to SEE each bug fire, then fix the real code the same way.

    python3 boundary_probes.py            # run all probes
    python3 boundary_probes.py rollover   # run one probe by name

Probe names: offbyone rollover empty range mutation intdiv negative overflow
No third-party deps. Python 3.7+.
"""
import sys


# 1. OFF-BY-ONE  (>= vs >, len vs len-1)
def probe_offbyone():
    arr = [10, 20, 30]
    # BUG: <= walks one past the end -> IndexError
    def last_buggy(a):
        i = 0
        while i <= len(a):      # should be i < len(a)
            i += 1
        return a[i - 1]
    try:
        last_buggy(arr)
        print("offbyone: NO CRASH (unexpected)")
    except IndexError:
        print("offbyone: IndexError as expected -> use `i < len(a)`, not `<=`")


# 2. ROUNDING ROLLOVER  (round THEN re-check the unit boundary)
def fmt_buggy(n):
    units = ["B", "kB", "MB", "GB", "TB"]; i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000; i += 1
    return f"{n:.0f} {units[i]}"          # 999999 -> "1000 kB"  WRONG


def fmt_fixed(n):
    units = ["B", "kB", "MB", "GB", "TB"]; i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000; i += 1
    if round(n) >= 1000 and i < len(units) - 1:   # re-check AFTER rounding
        n /= 1000; i += 1
    return f"{n:.0f} {units[i]}"          # 999999 -> "1 MB"  CORRECT


def probe_rollover():
    for v in (999999, 999500):
        print(f"rollover: {v} -> buggy={fmt_buggy(v)!r}  fixed={fmt_fixed(v)!r}")


# 3. EMPTY / ZERO INPUT  (average of nothing)
def probe_empty():
    nums = []
    try:
        print(sum(nums) / len(nums))
    except ZeroDivisionError:
        print("empty: ZeroDivisionError -> guard `if not nums: return 0` first")


# 4. INCLUSIVE vs EXCLUSIVE RANGE  (does 'to' include the endpoint?)
def probe_range():
    # "items 1 through 3" -> humans mean 3 items; range(1,3) gives only 2
    exclusive = list(range(1, 3))          # [1, 2]
    inclusive = list(range(1, 3 + 1))      # [1, 2, 3]
    print(f"range: exclusive={exclusive} inclusive={inclusive} "
          f"-> if the spec says 'through N', use N+1")


# 5. INPUT MUTATION  (function eats its caller's data)
def probe_mutation():
    def add_tax_buggy(items):
        items.append("tax"); return items      # mutates caller's list
    orig = ["a", "b"]
    add_tax_buggy(orig)
    print(f"mutation: caller now {orig} -> copy first: `items = list(items)`")


# 6. INTEGER vs FLOAT DIVISION  (// truncates, / does not)
def probe_intdiv():
    total, n = 7, 2
    print(f"intdiv: 7//2={total // n} (truncated)  7/2={total / n} "
          f"-> use `/` for averages, `//` only when you truly want floor")


# 7. NEGATIVE INPUT  (assumed non-negative)
def probe_negative():
    def pct_of_bar(value, width=10):
        return "#" * int(value / 100 * width)   # negative -> int(<0) -> "" silently
    print(f"negative: pct_of_bar(-50)={pct_of_bar(-50)!r} (silently empty) "
          f"-> clamp: `max(0, min(100, value))`")


# 8. UNIT / TYPE OVERFLOW  (value exceeds the bucket's range)
def probe_overflow():
    # clock minutes that roll past 59, hours past 23
    minutes = 75
    buggy = f"00:{minutes}"                     # "00:75"  invalid
    fixed = f"{minutes // 60:02d}:{minutes % 60:02d}"
    print(f"overflow: buggy={buggy!r} fixed={fixed!r} "
          f"-> carry with divmod/`% base` at every unit boundary")


PROBES = {
    "offbyone": probe_offbyone, "rollover": probe_rollover, "empty": probe_empty,
    "range": probe_range, "mutation": probe_mutation, "intdiv": probe_intdiv,
    "negative": probe_negative, "overflow": probe_overflow,
}

if __name__ == "__main__":
    names = sys.argv[1:] or list(PROBES)
    for name in names:
        fn = PROBES.get(name)
        if fn:
            fn()
        else:
            print(f"unknown probe {name!r}; choose from {', '.join(PROBES)}")
