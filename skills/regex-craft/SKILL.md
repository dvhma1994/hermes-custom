---
name: regex-craft
description: "Build any regex test-first: define must-match and must-not-match cases (incl escaping, char-class, anchors, unicode) and run them BEFORE using the pattern."
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [regex, testing, correctness, edge-cases, software-development]
    category: software-development
---

# Regex-Craft Skill

A regex that "looks right" is the #1 source of silent wrong output. Never ship a
pattern you haven't run against a table of **must-match** and **must-not-match**
strings — including the traps below. Build the table first, then the pattern.

## When to use
- Any time you write or change a regex that touches real input (parsing, validation,
  slugs, splitting, extraction, search/replace).
- Reviewing a diff containing a regex — reconstruct the table and run it.

## When NOT to use
- A fixed literal string match (use `==` / `in`, not regex).
- Parsing structured formats with a real parser available (HTML/JSON/CSV) — regex is
  the wrong tool; use the parser.

## The drill
1. Write the cases FIRST — at least 3 must-match and 3 must-not-match, each targeting a trap.
2. Write the pattern.
3. Run the table. Every must-match passes, every must-not-match fails. Only then use it.

```bash
python scripts/rxcheck.py '<pattern>' --yes 'good1' 'good2' --no 'bad1' 'bad2'
```

## The trap catalog (put at least the relevant ones in your table)
- **Char-class brackets**: inside `[...]`, a `]` right after `[` closes the class early —
  `[+{}()[]:]` is NOT what you think. Put `]` first or escape it: `[]+{}()[:]` / `[\]\[]`.
  A `-` is a range unless first/last; `^` first negates the class.
- **Anchors + trailing newline**: `$` matches before a final `\n`, and `re.search('x$', 'x\n')`
  is True. Use `\Z` (or `re.fullmatch`) when a trailing newline must NOT match.
- **Greedy vs lazy**: `<.*>` eats the whole line; `<.*?>` stops at the first `>`.
- **Alternation precedence**: `^a|b$` means `(^a)|(b$)`, not `^(a|b)$` — group it.
- **Unicode**: `\w` / `\d` match `café` / `٣` by default; pass `re.ASCII` to restrict to ASCII.
- **Run-collapsing**: `[a-z0-9-]+` over-matches `-abc` and `a--b`; anchor/strip or post-process.
- **Escaping user input**: when a string goes INTO a pattern, wrap it in `re.escape(...)`.

## Verification
Lock the table into a parametrized test so the pattern can't silently regress:
```python
import re, pytest
P = re.compile(r"...")
@pytest.mark.parametrize("s,hit", [("good", True), ("BAD", False)])
def test_pattern(s, hit):
    assert bool(P.search(s)) is hit
```
