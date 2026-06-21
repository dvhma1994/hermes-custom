---
name: perf-hotspot
description: Evidence-driven performance optimization: measure, find the one hotspot, change one thing, re-measure, keep only proven wins. Use when code/tests/builds are slow.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [performance, profiling, optimization, cprofile, benchmarking, python]
    category: software-development
---

# Performance Hotspot Skill

Optimize with **evidence, never intuition**. You are not allowed to "speed up"
code you have not measured. Every change must be proven by a before/after number.

## The Iron Law

```
NO OPTIMIZATION WITHOUT A BEFORE-NUMBER AND AN AFTER-NUMBER.
```

If you cannot show a measurement that improved, you did not optimize â€” you
gambled. Revert it.

## When to use

- A real workload is slow: a request, a script, a test suite, a build, a query.
- You have (or can build) a repeatable way to trigger that workload.
- Someone asked "why is this slow?" or "make this faster."

## When NOT to use

- **No measurement is possible yet.** Build the timer first; do not guess.
- The code is not actually on a hot path (run once at startup, tiny input). A
  10x speedup of 0.1% of runtime is 0.0% faster overall â€” that is Amdahl's law.
- Correctness or clarity is the real problem. Slow-but-correct beats fast-but-wrong.
- You're tempted to "just add caching / async / a C extension" before profiling.
  That is speculative optimization. Forbidden.

## The loop (do this in order, every time)

1. **Reproduce the real workload.** Use representative input size and data, not a
   toy. Make it a single command you can re-run.
2. **Measure the baseline.** Wall-clock the whole workload AND profile it. Write
   the number down.
3. **Find the ONE hotspot.** Read the profile. The top line by cumulative/total
   time is your target. Ignore everything else for now.
4. **Form ONE hypothesis.** "Function X is slow because it re-sorts the list on
   every call." Specific, falsifiable, about that one hotspot.
5. **Change ONE thing.** The smallest edit that tests the hypothesis. Do not
   bundle a refactor.
6. **Re-measure** the same workload, same way.
7. **Decide with evidence:**
   - Faster beyond the noise band â†’ keep it, lock it in, go to step 2.
   - Same or slower â†’ **revert it.** A non-improving change is dead weight and
     risk. Form a new hypothesis.
8. **Stop** when the workload is fast enough, or the top hotspot is now something
   you can't change (network, disk, a dependency). Don't micro-optimize forever.

## Step 2a â€” wall-clock the whole thing (is it even slow?)

Coarsest, truest signal. Time the actual command:

```bash
# Unix
time python run_workload.py
# -> real 0m4.812s   (real = wall clock, what users feel)

# Cross-platform / Windows PowerShell
Measure-Command { python run_workload.py }
```

In-code, time a region precisely with a monotonic clock (NOT `time.time()`,
which can jump backward on clock sync):

```python
import time

t0 = time.perf_counter()
result = do_work(big_input)
elapsed = time.perf_counter() - t0
print(f"do_work: {elapsed:.3f}s")
```

Reusable context-manager timer:

```python
import time, contextlib

@contextlib.contextmanager
def timer(label: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        print(f"{label}: {time.perf_counter() - t0:.3f}s")

with timer("parse"):
    rows = parse(path)
with timer("aggregate"):
    out = aggregate(rows)
```

For small/fast functions, one run is pure noise. Use median of repeats with
warmup (handles cache/import warmup) â€” the bundled `scripts/timeit_runner.py`:

```bash
python scripts/timeit_runner.py --setup "import mymod; x=mymod.load()" "mymod.work(x)"
# per-call median   :    842.107 us   <-- compare THIS
# noise band        : +/- 6.3%        (changes inside this are noise)
```

Quick one-liner equivalent:

```bash
python -m timeit -s "import mymod; x=mymod.load()" "mymod.work(x)"
```

## Step 3 â€” profile to find the ONE hotspot

Wall-clock tells you IF it's slow. `cProfile` (stdlib, deterministic) tells you
WHERE. Profile the real workload:

```bash
# Sort by cumulative time: which call chains dominate end-to-end.
python -m cProfile -s cumulative run_workload.py | head -25

# Sort by total/own time: which functions burn time in THEIR OWN body
# (excluding callees) â€” the real CPU hogs.
python -m cProfile -s tottime run_workload.py | head -25
```

Profile a specific region and save stats for repeat inspection:

```python
import cProfile, pstats

prof = cProfile.Profile()
prof.enable()
result = do_work(big_input)
prof.disable()

st = pstats.Stats(prof)
st.strip_dirs().sort_stats("tottime").print_stats(15)
prof.dump_stats("baseline.prof")   # save to diff later

# Who calls the hotspot, and what does it call? (callers / callees)
st.print_callers("expensive_func")
st.print_callees("expensive_func")
```

### How to read the cProfile table

```
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
  1000000    2.310    0.000    2.310    0.000 mymod.py:88(distance)
       50    0.040    0.001    4.620    0.092 mymod.py:40(cluster)
```

- **tottime** â€” time in this function's *own* body, excluding sub-calls. High
  tottime = optimize this function's code. **This is usually your hotspot.**
- **cumtime** â€” time in this function *including* everything it calls. High
  cumtime + low tottime = the cost is in a callee; follow it down.
- **ncalls** â€” call count. A cheap function called 1,000,000 times is a hotspot:
  the fix is often "call it less," not "make it faster."
- **percall** â€” tottime/ncalls. Tiny percall but huge ncalls â†’ reduce the calls.

Read it as: *high tottime â†’ fix the body; high ncalls â†’ fix the caller; high
cumtime alone â†’ descend into callees.*

### Line-level when the function is big

`cProfile` is per-function. If the hotspot is one fat function and you need the
exact line, drop in manual `perf_counter` timers around blocks, or install
`line_profiler` (`pip install line_profiler`, then `@profile` + `kernprof -l -v`).
Don't reach for it until cProfile has named the function.

## The disciplines (what makes this "evidence only")

- **Change ONE thing per measurement.** Two edits at once and you can't tell
  which helped â€” or which one made it worse while the other hid it. Hopeless.
- **Same workload, same machine, same measure** for before and after. Otherwise
  you're comparing noise.
- **Respect the noise band.** If runtimes vary +/- 8%, a "6% improvement" is
  nothing. Use the median-of-repeats and require the gain to clear the spread.
- **Revert non-wins immediately.** Code only earns its place by a proven number.
  Speculative complexity that "should be faster" is a bug waiting to happen.
- **Re-profile after every kept win.** Fixing hotspot #1 makes hotspot #2 the new
  #1. The old profile is stale. Top-down, always.
- **Stop at good enough.** Optimization has diminishing returns; clarity has
  ongoing cost. When it's fast enough, ship.

## Traps that fake out your numbers

- **Profiling overhead.** `cProfile` inflates absolute times (especially for
  many tiny calls) â€” use it for *relative* ranking, use `perf_counter`/`time`
  for the true wall-clock number.
- **Caching / warmup.** First run pays import, disk, and CPU-cache costs. Warm up
  once, then measure. (`timeit_runner.py` does this.)
- **Dead-code elimination / constant folding** in micro-benchmarks: if the result
  is unused, the interpreter may skip work. Use the result (return/print it).
- **Optimizing off the hot path.** Always confirm the function you're editing is
  actually near the top of the profile. Amdahl's law is unforgiving.
- **Tiny / unrepresentative input.** O(n^2) looks fine at n=10. Profile at real n.
- **Different I/O state.** Warm OS file cache vs cold disk, populated vs empty DB â€”
  pin the conditions or your before/after is meaningless.
- **Background load.** A busy machine adds variance. Close the noise, take medians.

## Quick reference

| Goal | Tool / command |
|------|----------------|
| Is the whole thing slow? | `time cmd` / `Measure-Command { cmd }` |
| Time a code region | `time.perf_counter()` around it |
| Time a small function reliably | `scripts/timeit_runner.py` / `python -m timeit` |
| Find the hotspot (call chains) | `python -m cProfile -s cumulative app.py` |
| Find the hotspot (own CPU) | `python -m cProfile -s tottime app.py` |
| Inspect saved profile | `pstats.Stats("baseline.prof")` |
| Who calls / is called by hotspot | `.print_callers()` / `.print_callees()` |
| Exact slow line in a big function | manual `perf_counter` blocks, then `line_profiler` |

**No before-number, no after-number, no optimization. Measure, change one thing,
re-measure, keep only what's proven.**
