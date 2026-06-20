---
name: claim-verify
description: Fact-check a claim with keyless search and citations.
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [fact-check, verification, research, search]
    category: research
---

# Claim Verify

Decompose a single claim into atomic sub-claims, then verify each one against keyless web sources with an explicit adversarial pass. Output a per-subclaim verdict with citations and a rolled-up overall verdict with confidence. No API keys required.

## When to Use

- A factual assertion needs independent checking before you repeat it.
- The user asks "is X true?" or "is this accurate?"
- A source makes a load-bearing claim that downstream work depends on.
- A statistic, date, or attribution is about to be cited.

- Do NOT use when the statement is purely subjective/opinion, or it already comes from a fully-cited trusted primary source you can hand-link.

## Prerequisites

Requires the keyless DuckDuckGo MCP server and the markitdown MCP server. No API key is needed anywhere in this skill.

- `mcp_duckduckgo_duckduckgo_web_search` — keyless web search. Params: `query`, `limit` (1-20), `safeSearch` (strict/moderate/off), `region` (e.g. `us-en`, `wt-wt`), `time` (day/week/month/year/all).
- `mcp_duckduckgo_duckduckgo_news_search` — keyless recent-news search. Same params minus `region`.
- `mcp_markitdown_convert_to_markdown` — convert any `http`/`https`/`file`/`data` URI to readable markdown so you can actually read source pages.

## Procedure

1. **Restate the claim** as a single falsifiable sentence. If it cannot be falsified, say so and stop.
2. **Decompose** it into atomic sub-claims: each one must be independently checkable (a name, a number, a date, a causal link, a quote).
3. **Confirming search** — for each sub-claim run `mcp_duckduckgo_duckduckgo_web_search`. Quote the key phrase verbatim in the query. For time-sensitive sub-claims also run `mcp_duckduckgo_duckduckgo_news_search` with `time` set to `month` or `year`.
   ```json
   {"tool": "mcp_duckduckgo_duckduckgo_web_search",
    "query": "\"<exact key phrase from claim>\"", "limit": 5}
   ```
4. **Adversarial pass (mandatory)** — deliberately run a query that *should* disconfirm the sub-claim. Append `debunked`, `false`, `retracted`, `refuted`, `correction`, or the opposing side's terms.
   ```json
   {"tool": "mcp_duckduckgo_duckduckgo_web_search",
    "query": "\"<key phrase>\" debunked OR false OR retracted", "limit": 5}
   ```
5. **Read the sources** — pass top result URLs (from both passes) to `mcp_markitdown_convert_to_markdown` to actually read them; do not decide from the snippet alone.
   ```json
   {"tool": "mcp_markitdown_convert_to_markdown", "uri": "https://example.org/source"}
   ```
6. **Prefer primary/official sources** (government, court, peer-reviewed paper, original press release, company statement) over aggregators, listicles, and SEO pages. Skip pages that merely quote another outlet — trace to the origin.
7. **Per-subclaim verdict** — assign one of `supported` / `refuted` / `uncertain`, each with 1-2 citations (URL + the exact sentence in the source that supports the verdict).
8. **Roll up** to an overall verdict and state confidence (high/medium/low). A single refuted load-bearing sub-claim can sink the whole claim even if others are supported.

## Quick Reference

Tool signatures:
```
mcp_duckduckgo_duckduckgo_web_search(query, limit, safeSearch, region, time)
mcp_duckduckgo_duckduckgo_news_search(query, limit, safeSearch, time)
mcp_markitdown_convert_to_markdown(uri)
```

Verdict rubric:
- **supported** — a credible source confirms AND no credible source refutes it.
- **refuted** — a credible source directly contradicts it.
- **uncertain** — sources conflict, are absent, or are low-quality only.

Output template:
```
Claim: <one sentence>
| # | Sub-claim | Verdict | Citation (URL + quote) |
|---|-----------|---------|------------------------|
| 1 | ...       | ...     | ...                    |
Overall verdict: Supported | Refuted | Mixed | Uncertain
Confidence: High | Medium | Low
Notes: <disconfirming queries actually run, key gaps>
```

## Pitfalls

- **Confirmation bias** — only searching to confirm. The disconfirming query is mandatory for every sub-claim; if you didn't run one, the verdict is invalid.
- **SEO/affiliate/aggregator junk** ranking high — skip to the primary source it paraphrases.
- **LLM-generated content farms** — treat as non-sources; look for original publisher attribution.
- **Outdated cached facts** — use the `time` param and `_news_search` for recency-sensitive claims.
- **Citation laundering** — site A quotes site B quotes site C. Follow the chain to the origin before citing.
- **Absence of evidence ≠ refutation** — if you can't find anything either way, mark `uncertain`, not `refuted`.

## Verification

The check is sound only if all hold:
- Every verdict cites at least one real, reachable URL you actually opened via `mcp_markitdown_convert_to_markdown`.
- At least one disconfirming query was actually run per sub-claim (not skipped).
- Primary/official sources were preferred over aggregators where available.
- The overall verdict is the weighted roll-up of sub-claim verdicts, not a gut feeling; a refuted load-bearing sub-claim forces "Refuted" or "Mixed".
- Confidence is stated explicitly and justified by source quality and corroboration.