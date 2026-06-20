---
name: progressive-recall
description: Recall memory by filtering an index before fetching detail.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [memory, retrieval, token-efficiency, context]
    category: productivity
---

# Progressive-Recall Skill

When pulling from long-term memory, do not dump everything into context. Recall
in stages: first get a compact index (ids + one-line summaries), filter to the
few relevant ids, then fetch full detail only for those. "Filter before you
fetch" routinely cuts memory-recall tokens ~10x while improving precision,
because the model reasons over a short index instead of a wall of past content.

## When to Use
- Any turn that benefits from past context ("what did we decide about X?",
  "continue the Y work") where memory could return a lot.
- NOT when you already hold the needed fact in the current conversation.

## Prerequisites
- A configured memory provider (the `memory` tool) — honcho / mem0 / supermemory
  / byterover / MemOS, etc. All benefit from this read pattern.

## Procedure (search -> filter -> fetch)
1. **Search for an index, not bodies.** Query memory for matches and ask for a
   compact list: `id - one-line summary` per hit. Do not request full entries yet.
2. **Filter.** From the index, pick only the ids that actually bear on the task
   (usually 1-5). Discard the rest - they never enter context.
3. **Fetch full detail for the chosen ids only.** Now pull the complete content
   for that short list.
4. **Cite what you used.** Note which memory ids informed the answer so the
   reasoning is traceable.

## Quick Reference
- Step 1 returns ids + summaries, never full bodies.
- Open full content only for the filtered ids.
- Keep the recalled set small (prefer the top few, not "everything related").

## Pitfalls
- Pulling full memory bodies in the first query (defeats the savings).
- Keeping low-relevance hits "just in case" - they dilute attention and cost tokens.

## Verification
You applied this correctly if the context grew by a short index + a few full
entries (not a bulk dump), and the answer cites the specific memory ids used.
