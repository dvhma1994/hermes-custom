---
name: local-web-research
description: Research topics via keyless search and fetch-to-markdown.
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [research, web, search, keyless, markitdown]
    category: research
---

# Local Web Research

Do web research without any paid API key by combining keyless search with
fetch-to-markdown. Search the open web via DuckDuckGo, pull clean markdown
from the best pages, and synthesize a cited answer — no Firecrawl or paid
search API keys required.

## When to Use

- Answering factual questions that need current or external information.
- Investigating a topic where you don't already know the authoritative source.
- Researching when no Firecrawl / web-search API key is configured.
- Gathering multiple viewpoints before summarizing.

## Prerequisites

All of these are **keyless** — no API keys needed:

- The **DuckDuckGo MCP server** (keyless web search). Tool family
  `mcp_ddg_*` / `duckduckgo-search`. Primary tool:
  `mcp_duckduckgo_duckduckgo_web_search`. For news, use
  `mcp_duckduckgo_duckduckgo_news_search`.
- The **markitdown MCP server** (`mcp_markitdown_convert_to_markdown`),
  which fetches a URL and returns clean markdown. Alternatively, the
  `markitdown` CLI (`markitdown <url>`) works from the shell if the MCP
  server is unavailable.

## Procedure

1. **Form a focused search query.** State the question plainly, then turn it
   into a tight query. Prefer specific nouns over filler words. Add a domain
   (`site:gov`, `site:wikipedia.org`) or a year when precision matters.
2. **Run a keyless DuckDuckGo search** and collect the top result URLs and
   titles. Take 5–10 results so you can filter.
   ```
   mcp_duckduckgo_duckduckgo_web_search(query="rust async runtime tokio vs async-std", limit=8)
   ```
3. **Pick the 2–4 most relevant URLs.** Favor official docs, reputable
   sources, and pages whose title/snippet directly address the question.
   Skip obvious clickbait or low-quality aggregators.
4. **Fetch each with markitdown** to get clean markdown of the page body.
   ```
   mcp_markitdown_convert_to_markdown(uri="https://tokio.rs/tokio/tutorial")
   mcp_markitdown_convert_to_markdown(uri="https://docs.rs/async-std")
   ```
   If a page is huge, pass `max_length` (or slice with `start_index` on the
   fetch tool) to read it in chunks.
5. **Extract the answer from each source.** Pull the specific facts that
   answer the question — note which source each fact came from. Record the
   exact URL for citation.
6. **Synthesize with inline citations** and a Sources list. Use bracketed
   markers `[1]`, `[2]` next to non-trivial claims, then list sources:

   > Tokio is the most widely used async runtime for Rust [1], while
   > async-std mirrors the std-library API [2].
   >
   > **Sources**
   > [1] Tokio Tutorial — https://tokio.rs/tokio/tutorial
   > [2] async-std docs — https://docs.rs/async-std

## Quick Reference

| Step | Tool | Example |
|------|------|---------|
| Web search (keyless) | `mcp_duckduckgo_duckduckgo_web_search` | `query="...", limit=8` |
| News search (keyless) | `mcp_duckduckgo_duckduckgo_news_search` | `query="...", limit=5` |
| Fetch URL → markdown | `mcp_markitdown_convert_to_markdown` | `uri="https://..."` |
| Fetch (CLI fallback) | `markitdown <url>` (shell) | `markitdown https://example.com` |

## Pitfalls

- **DuckDuckGo rate limits / occasional empty results.** Retry once, then
  rephrase the query (drop or add a word, change domain filter). Avoid
  hammering it with dozens of back-to-back calls.
- **markitdown truncation on huge pages.** Use `max_length` / `start_index`
  or read in chunks so you don't lose the relevant section.
- **JS-heavy SPAs may render poorly** (dashboards, React apps). If markdown
  comes back nearly empty, try a different source or the site's static/sitemap
  URL rather than fighting the page.
- **Always cite source URLs.** Every non-trivial claim should map to a real
  page in the Sources list — not to a memory of "something I read."
- **Don't trust a single source.** Cross-check at least two independent
  sources for consequential or surprising claims.

## Verification

Before returning the answer, confirm:

- The synthesis directly addresses the **original question**, not a tangential
  one.
- Every non-trivial claim has a `[N]` citation, and each `[N]` maps to a
  **real, fetchable URL** listed in the Sources section.
- You actually fetched and read each cited source this turn (no invented URLs
  or content).
- Sources are recent and relevant enough for the question; flag if the best
  available info is stale.
