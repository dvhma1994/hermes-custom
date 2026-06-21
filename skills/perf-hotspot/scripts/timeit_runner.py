#!/usr/bin/env python3
"""Evidence-grade wall-clock timer: warmup + repeats + median, not one noisy run.

Usage:
    python timeit_runner.py "import mymod; mymod.work(big_input)"
    python timeit_runner.py --setup "import mymod; x=mymod.load()" "mymod.work(x)"
    python timeit_runner.py --repeats 9 --inner 100 "sum(range(1000))"

Reports min / median / max over `repeats` rounds. Compare the MEDIAN across
before/after runs. A change is real only if the new median clears the noise
band of the old run (max-min spread). One number from one run proves nothing.
"""
from __future__ import annotations

import argparse
import statistics
import timeit


def main() -> None:
    p = argparse.ArgumentParser(description="Median-based microbenchmark.")
    p.add_argument("stmt", help="Statement to time (Python source).")
    p.add_argument("--setup", default="pass", help="Setup code, run once per repeat, not timed.")
    p.add_argument("--repeats", type=int, default=7, help="Number of timing rounds (default 7).")
    p.add_argument("--inner", type=int, default=0,
                   help="Inner loops per round. 0 = auto-calibrate to ~0.2s.")
    args = p.parse_args()

    t = timeit.Timer(stmt=args.stmt, setup=args.setup)

    inner = args.inner
    if inner <= 0:
        inner, _ = t.autorange()  # picks a loop count so one round takes >= ~0.2s

    t.timeit(number=min(inner, 1000))  # warmup: prime caches / JIT / imports

    rounds = [t.timeit(number=inner) / inner for _ in range(args.repeats)]
    lo, mid, hi = min(rounds), statistics.median(rounds), max(rounds)
    spread = (hi - lo) / mid * 100 if mid else 0.0

    print(f"inner loops/round : {inner}")
    print(f"rounds            : {args.repeats}")
    print(f"per-call median   : {mid * 1e6:10.3f} us   <-- compare THIS")
    print(f"per-call min/max  : {lo * 1e6:10.3f} / {hi * 1e6:.3f} us")
    print(f"noise band        : +/- {spread:.1f}%   (changes inside this are noise)")


if __name__ == "__main__":
    main()
