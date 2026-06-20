---
name: structured-output
description: Get reliable JSON from any model with validation and repair.
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [json, structured-output, validation, llm, prompts, self-repair]
    category: software-development
---

# Structured Output

Structured output turns a free-form language model into a reliable data source by
pinning it to an explicit schema, parsing the response defensively, validating it, and
re-prompting on failure. The full loop uses only the Python standard library (`json`, `re`)
plus optional `jsonschema`.

## When to Use

- You need the model's answer as typed data (ints, enums, lists), not prose.
- Downstream code consumes the result programmatically and must not crash on bad input.
- You want instructor/BAML-grade reliability without adding an external dependency.
- A single bad parse would break an automated pipeline or agent step.

## Prerequisites

- Python 3.8+ (uses only `json` and `re` from the standard library).
- Optional: `pip install jsonschema` for schema-based validation (not required).
- A model call function you control that takes a prompt string and returns text.

## Procedure

1. **Define the schema.** Write it as a JSON Schema dict. Be explicit about `type`,
   `required`, `enum`, and `additionalProperties: false`. Tight schemas catch more errors.

2. **Build the prompt.** Tell the model to respond with ONLY JSON, embed the schema, and
   give one short example. Add a system instruction: "Output valid JSON only. No prose,
   no markdown fences."

3. **Call the model** and capture the raw text response.

4. **Strip and parse.** Remove markdown code fences (` ```json ... ``` `) with a regex,
   then `json.loads()`. If `json.loads` raises, record the error message verbatim.

5. **Validate against the schema.** Prefer `jsonschema.validate(instance, schema)` if
   available; otherwise do manual checks (required keys, types, enum membership). Collect
   every violation into a single `errors` list.

6. **Self-repair loop.** If parsing or validation failed, re-prompt the SAME conversation
   with: the original schema, the raw failing output, and the exact error(s). Append
   "Return ONLY corrected JSON." Retry up to N (typically 2–3) times. Stop on success or
   when retries are exhausted (then raise / fall back to a default).

7. **Return the validated object** to the caller only after checks pass.

## Quick Reference

```python
import json, re

def extract_json(text):
    """Strip code fences and return parsed JSON, or raise ValueError with detail."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    raw = fenced.group(1).strip() if fenced else text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse failed: {e}\nRaw: {raw[:300]}") from e

def repair_loop(call_model, schema, prompt, max_retries=3):
    messages = [{"role": "user", "content": prompt}]
    last_error = None
    for attempt in range(max_retries):
        raw = call_model(messages)
        try:
            data = extract_json(raw)
            # Manual required/type checks (or jsonschema.validate(data, schema))
            missing = [k for k in schema.get("required", []) if k not in data]
            if missing:
                raise ValueError(f"Missing required keys: {missing}")
            return data                       # success
        except ValueError as e:
            last_error = e
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That output was invalid:\n{e}\nReturn ONLY corrected JSON matching the schema."},
            ]
    raise last_error
```

## Pitfalls

- **Markdown fences**: models wrap output in ` ```json `. Always strip fences before parsing.
- **Trailing prose**: some models append "Here is the JSON:". Instruct against it and use
  the first JSON object found if needed.
- **Loose schemas**: `additionalProperties: true` lets extra keys slip in silently. Set it
  to `false` and enumerate `required`.
- **Giving up after one failure**: a single re-prompt with the error fixes the majority of
  parse failures. Always allow at least one retry.
- **Non-deterministic keys**: if the model invents key names, constrain them with `enum`
  or a fixed `properties` map and reject anything else.

## Verification

- **Unit test the extractor** on fenced JSON, bare JSON, and garbage input.
- **Feed a deliberately broken completion** into the repair loop and confirm it re-prompts
  with the error and eventually succeeds or exhausts retries.
- **Run N=20–50 calls** against your real model+prompt and assert the parse success rate is
  100% after retries. If not, tighten the schema or improve the prompt's example.
