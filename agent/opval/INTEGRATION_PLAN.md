# OPVAL Integration Plan for conversation_loop.py

## Goal
Wire `OpvalSessionRecorder` into the agent turn lifecycle so OPVAL collects
evidence automatically when `HERMES_OPVAL=1`.

## What not to touch
- Execution Checkpoint System code (closed).
- Architecture Freeze v1 logic.
- Strategic Learning activation.

## Hook points

1. **Turn start** in `run_conversation`
   - Record `_turn_start = time.time()` and initialize `OpvalSessionRecorder`
     for the session if `opval_enabled()`.

2. **Tool call capture**
   - Extend `agent` to track `_tool_calls_this_turn` and `_tool_results_this_turn`
     in the existing tool execution path (do not rewrite tool handling).

3. **Turn end**
   - After `build_turn_context` returns `_ctx`, call `_opval_recorder.record_turn()`
     with user message, agent response, tool calls, tool outputs, latency, and tokens.

4. **Session end**
   - On `run_conversation` exit (success, failure, or interrupt), call
     `_opval_recorder.finalize()` and store session record.

5. **Compression / rotation**
   - Pass `parent_session_id` and `session_lineage` to `finalize()` so OPVAL
     understands session compression.

## Fallback
- If OPVAL collection raises an exception, log it and continue the turn.
  OPVAL must never break the user-facing run.
