---
name: boundary-bugs
description: Pre-ship checklist and one-line probes for edge-case bugs that pass the happy path - off-by-one, rounding rollover, empty/zero/negative inputs, inclusive vs exclusive ranges, input mutation, integer division, unit overflow.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [testing, edge-cases, debugging, code-review, off-by-one, boundary-analysis, pre-ship]
    category: software-development
---

# Boundary Bugs Skill

The happy path passing means nothing. Code breaks at the *edges*: the first/last
element, the empty list, the zero, the negative, the value that rounds up across a
unit boundary. This skill is a pre-ship checklist of eight bug classes, each with a
copy-pasteable probe you run *before* you call the code done.

## When to Use

- Before shipping/merging any function that does indexing, slicing, formatting,
  arithmetic over user data, or range/loop logic.
- During code review of a diff that touches loops, comparisons (`<` vs `<=`),
  formatters (sizes, dates, currency, durations), or anything that takes a list/string.
- When a bug report says "works for normal values but breaks for X" â€” X is almost
  always one of the eight classes below.

## When NOT to Use

- Pure I/O glue, config wiring, or logging with no arithmetic and no indexing â€”
  there are no boundaries to cross.
- When you have not yet made the happy path pass. Get it green first, then harden
  the edges. Do not gold-plate dead code.

## The Drill

For each function under review, run the matching probe. A probe is the *smallest input
that lives on the boundary*. If the output is wrong, surprising, or throws â€” you found it.

The companion script demonstrates every class firing for real:

```bash
python3 scripts/boundary_probes.py            # all eight
python3 scripts/boundary_probes.py rollover   # just one
```

---

### 1. Off-by-one (`>=` vs `>`, `len` vs `len-1`)
The classic: a loop or index that walks one step too far or stops one short.
**Probe â€” feed the boundary index and an empty/one-element collection:**
```python
f([])            # does first/last logic blow up on empty?
f([x])           # does a 1-element list work?
f(items)[-1]     # is the LAST element handled, or skipped?
# loop bound: is it `i < len(a)` (correct) or `i <= len(a)` (IndexError)?
```
Trap: `range(len(a))` is correct; `range(len(a)+1)` and `while i <= len(a)` are off by one.

### 2. Rounding rollover (round THEN re-check the unit boundary)
A size/number formatter must turn `999999` into `"1 MB"`, **not** `"1000 kB"`. The
divide-down loop stops at `999.999 kB`, then `%.0f` rounds it to `1000` â€” which should
have rolled to the next unit. **Fix: after rounding, re-check the boundary and carry.**
**Probe â€” feed a value just under each unit threshold:**
```python
fmt(999999)      # expect "1 MB", a bug yields "1000 kB"
fmt(999500)      # rounds up across the boundary too
fmt(1023.9 * 1024)  # for base-1024 formatters
```
Same trap hits currency (`$9.999` -> `$10.00`) and any "N.9 -> rounds to whole next unit".

### 3. Empty / zero input
Averages, percentages, "first of", and any denominator die on empty/zero.
**Probe:**
```python
f([])            # ZeroDivisionError on sum/len? IndexError on [0]?
divide(x, 0)     # guarded, or crash?
```
Fix: `if not items: return <neutral value>` before the math, never inside it.

### 4. Inclusive vs exclusive range
"Items 1 through 3" means three items to a human; `range(1, 3)` gives two. Off-by-one's
sibling, but it hides in *specs*, not loops.
**Probe â€” count the result against the spoken intent:**
```python
list(range(start, end))      # exclusive: end NOT included
list(range(start, end + 1))  # inclusive: end included
# A date range "Jan 1 to Jan 31": 31 days (inclusive) or 30 (exclusive)?
```
Trap: SQL `BETWEEN` is inclusive, Python slices are exclusive, `.slice()` in JS is
exclusive. Mixing them silently drops or doubles an endpoint.

### 5. Input mutation
A function that appends/sorts/pops its argument corrupts the caller's data and creates
spooky action-at-a-distance bugs.
**Probe â€” pass a value, then re-inspect the ORIGINAL:**
```python
orig = [3, 1, 2]
f(orig)
assert orig == [3, 1, 2]   # did f sort/append/clear it in place?
```
Fix: copy at the top â€” `items = list(items)`, `d = dict(d)`, `s = set(s)`. Especially
dangerous with mutable default args: `def f(x, acc=[])` shares one list across calls.

### 6. Integer vs float division
`//` truncates toward negative infinity; `/` returns a float. Using `//` for an average
or a ratio silently drops the fraction. In statically typed langs, `int/int` may even be
integer division by default (C, Java, Go).
**Probe:**
```python
avg = sum(xs) / len(xs)    # float â€” correct for averages
half = total // 2          # floor â€” correct ONLY when you want whole units
print(7 // 2, 7 / 2)       # 3 vs 3.5 â€” pick deliberately
# negative floor surprise: -7 // 2 == -4, not -3
```

### 7. Negative input
Code that assumes non-negative (counts, widths, percentages, indices) does something
quietly wrong rather than loud â€” `"#" * int(-5)` is `""`, `a[-1]` is the *last* element
not an error.
**Probe:**
```python
f(-1)            # silent empty? wrong wraparound via negative index?
progress(-50)    # clamp to 0, or render garbage?
```
Fix: `value = max(lo, min(hi, value))` at the entry, or reject with a clear error.

### 8. Unit / type overflow
A value that exceeds its bucket's range: minute `75`, hour `25`, month `13`, a byte
holding `256`, an index past the array. The fix is always carry with `divmod` / `% base`.
**Probe â€” feed a value one past the bucket max:**
```python
f"{m // 60:02d}:{m % 60:02d}"   # 75 min -> "01:15", not "00:75"
divmod(total_seconds, 60)        # carry seconds into minutes
# month 13 -> year+1, month 1
```

---

## Pre-ship Checklist (copy into the PR description)

```
[ ] 1. off-by-one     - tested [], [x], and the LAST element
[ ] 2. rollover       - formatter: 999999 -> "1 MB" not "1000 kB"
[ ] 3. empty/zero     - empty input + zero denominator guarded
[ ] 4. range          - inclusive vs exclusive matches the spec's intent
[ ] 5. mutation       - original arg unchanged after the call
[ ] 6. int/float div  - / vs // chosen deliberately (and negatives)
[ ] 7. negative       - negative input clamped or rejected, not silently wrong
[ ] 8. overflow       - values carry across unit boundaries (divmod/% base)
```

## Pitfalls

- **Testing only the middle of the range.** The bug is never at `n=5`; it is at `0`,
  `1`, `len-1`, `len`, and `max+1`. Always test the extremes, not a comfortable value.
- **"Round then format" vs "format then round".** Formatting first hides the rollover.
  Round the magnitude, THEN decide the unit. (Class 2.)
- **Trusting the happy-path test as coverage.** One passing example with normal data
  proves almost nothing about edges. Add one boundary case per class that applies.
- **Fixing the symptom downstream.** If `"1000 kB"` shows in the UI, fix the formatter's
  boundary re-check, not a string replace in the view layer.

## Verification

After fixing, prove it with a boundary table, not a single value:
```python
import pytest
@pytest.mark.parametrize("n,want", [
    (0, "0 B"), (999, "999 B"), (1000, "1 kB"),
    (999999, "1 MB"),           # the rollover case
    (1023*1024, "1 MB"),        # adjust per base
])
def test_fmt_boundaries(n, want):
    assert fmt(n) == want
```
A green table across `0 / 1 / max / max+1 / just-under-threshold` is the bar. One value is not.
