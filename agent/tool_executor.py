"""Tool-call execution — sequential and concurrent dispatch.

Both AIAgent methods (``_execute_tool_calls_sequential`` and
``_execute_tool_calls_concurrent``) live here as module-level
functions that take the parent ``AIAgent`` as their first argument.

``run_agent`` keeps thin wrappers so existing call sites work; tests
that patch ``run_agent._set_interrupt`` are honored because the
extracted functions reach back through the ``run_agent`` module via
``_ra()`` for that symbol.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Optional

from agent.display import (
    KawaiiSpinner,
    build_tool_preview as _build_tool_preview,
    get_cute_tool_message as _get_cute_tool_message_impl,
    get_tool_emoji as _get_tool_emoji,
    _detect_tool_failure,
)
from agent.tool_guardrails import ToolGuardrailDecision
from agent.tool_dispatch_helpers import (
    _is_destructive_command,
    _is_multimodal_tool_result,
    _multimodal_text_summary,
    _append_subdir_hint_to_multimodal,
    make_tool_result_message,
)
from tools.terminal_tool import (
    get_active_env,
)
from tools.thread_context import propagate_context_to_thread
from tools.tool_result_storage import (
    maybe_persist_tool_result,
    enforce_turn_budget,
)

logger = logging.getLogger(__name__)

# Maximum number of concurrent worker threads for parallel tool execution.
# Mirrors the constant in ``run_agent`` for tests/imports that look here.
_MAX_TOOL_WORKERS = 8


def _ra():
    """Lazy reference to ``run_agent`` so patches like ``run_agent._set_interrupt`` work."""
    import run_agent
    return run_agent


def _emit_terminal_post_tool_call(
    agent,
    *,
    function_name: str,
    function_args: dict,
    result: Any,
    effective_task_id: str,
    tool_call_id: str,
    duration_ms: int = 0,
    status: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    middleware_trace: Optional[list[dict[str, Any]]] = None,
) -> None:
    try:
        from model_tools import _emit_post_tool_call_hook
        _emit_post_tool_call_hook(
            function_name=function_name,
            function_args=function_args,
            result=result,
            task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
            error_message=error_message,
            middleware_trace=list(middleware_trace or []),
        )
    except Exception:
        pass


def _cancelled_tool_result(reason: str = "user interrupt") -> str:
    return json.dumps(
        {
            "error": f"Tool execution cancelled by {reason}",
            "status": "cancelled",
        },
        ensure_ascii=False,
    )


def _running_tool_names(not_done, futures, runnable_calls):
    """Tool names for the futures still running (for the heartbeat message).

    ``futures`` is built ONLY from ``runnable_calls`` (blocked calls are
    excluded), so a future's position must be mapped back through
    ``runnable_calls`` — NOT ``parsed_calls``, which still contains the blocked
    calls. Indexing ``parsed_calls`` by the future position shifts every name by
    the number of earlier blocked calls, so the heartbeat reports the wrong
    tools (and can even name a blocked tool that never ran).

    Each ``runnable_calls`` entry is ``(orig_index, tool_call, name, args)``.
    """
    names = []
    for f in not_done:
        if f in futures:
            names.append(runnable_calls[futures.index(f)][2])
    return names


def _record_tool_outcome_safe(agent, function_name: str, is_error: bool) -> None:
    """Milestone 1: record a tool outcome to RuntimeFeedback if available.

    Failures are swallowed so tool execution is never disrupted.
    """
    try:
        collector = getattr(agent, "_runtime_feedback", None)
        if collector is None:
            return
        from agent.runtime_feedback import RuntimeFeedbackCollector
        if not isinstance(collector, RuntimeFeedbackCollector):
            return
        collector.record_tool_outcome(
            {
                "tool_name": function_name,
                "outcome": "failure" if is_error else "success",
                "timestamp": time.time(),
            }
        )
    except Exception:
        logger.debug("Runtime feedback tool-outcome recording failed", exc_info=True)


def _emit_cancelled_terminal_post_tool_call(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
    start_time: float,
    reason: str = "user interrupt",
    error_type: str = "keyboard_interrupt",
    middleware_trace: Optional[list[dict[str, Any]]] = None,
) -> str:
    result = _cancelled_tool_result(reason)
    _emit_terminal_post_tool_call(
        agent,
        function_name=function_name,
        function_args=function_args,
        result=result,
        effective_task_id=effective_task_id,
        tool_call_id=tool_call_id,
        duration_ms=int((time.time() - start_time) * 1000),
        status="cancelled",
        error_type=error_type,
        error_message=f"Tool execution cancelled by {reason}",
        middleware_trace=list(middleware_trace or []),
    )
    return result


def _tool_search_scoped_names(agent) -> frozenset:
    """Return the deferrable tool names the session may invoke via tool_call.

    The Tool Search unwrap dispatches the underlying tool directly, bypassing
    the bridge branch (and its scope check) in
    ``model_tools.handle_function_call``. To keep a restricted-toolset session
    (subagent, kanban worker, curated gateway session) from reaching tools it
    was never granted, the unwrap validates the underlying name against this
    set: the deferrable subset of the session's own enabled/disabled toolset
    scope.

    Result is cached on the agent and refreshed when the tool registry's
    generation changes (e.g. an MCP server reconnects), so the common case is
    a dict lookup, not a full tool-defs rebuild on every tool call.
    """
    try:
        import model_tools
        from tools import tool_search as _ts
        from tools.registry import registry as _registry
    except Exception:
        return frozenset()

    enabled = getattr(agent, "enabled_toolsets", None)
    disabled = getattr(agent, "disabled_toolsets", None)
    cache_key = (
        getattr(_registry, "_generation", 0),
        frozenset(enabled) if enabled is not None else None,
        frozenset(disabled) if disabled is not None else None,
    )
    cached = getattr(agent, "_tool_search_scope_cache", None)
    if cached is not None and cached[0] == cache_key:
        return cached[1]
    try:
        scoped_defs = model_tools.get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        ) or []
        names = _ts.scoped_deferrable_names(scoped_defs)
    except Exception:
        names = frozenset()
    try:
        agent._tool_search_scope_cache = (cache_key, names)
    except Exception:
        pass
    return names


def _apply_tool_request_middleware_for_agent(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
) -> tuple[dict, list[dict[str, Any]]]:
    try:
        from hermes_cli.middleware import apply_tool_request_middleware

        result = apply_tool_request_middleware(
            function_name,
            function_args,
            task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
        )
        payload = result.payload if isinstance(result.payload, dict) else function_args
        return payload, list(result.trace)
    except Exception as exc:
        logger.debug("tool_request middleware error: %s", exc)
        return function_args, []


def _run_agent_tool_execution_middleware(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
    execute,
) -> tuple[Any, dict]:
    observed_args = function_args

    def _execute(next_args: dict) -> Any:
        nonlocal observed_args
        observed_args = next_args if isinstance(next_args, dict) else function_args
        return execute(observed_args)

    from hermes_cli.middleware import run_tool_execution_middleware

    try:
        result = run_tool_execution_middleware(
            function_name,
            function_args,
            _execute,
            original_args=function_args,
            task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
        )
    except Exception as tool_error:
        # A raising inline tool (todo/session_search/memory/clarify/
        # read_terminal/delegate_task) or its execution middleware must NOT
        # abort the entire turn — several of those branches lacked an
        # except-handler, so the exception escaped to the conversation loop and
        # killed the turn. Convert it to an error tool-result here (centralized)
        # so the single tool fails and the turn continues, matching the
        # context-engine/memory-manager branches. Only Exception is caught:
        # KeyboardInterrupt/BaseException still propagate so user-interrupt and
        # cancellation semantics are preserved.
        logger.error(
            "inline tool %r raised during execution: %s",
            function_name, tool_error, exc_info=True,
        )
        return (
            json.dumps({"error": f"Tool '{function_name}' failed: {tool_error}"}),
            observed_args,
        )
    return result, observed_args


_VERIFICATION_MARKERS = (
    "pytest", "py.test", "unittest", "nosetests", "tox",
    "npm test", "npm run test", "yarn test", "pnpm test", "jest", "vitest", "mocha",
    "tsc", "mypy", "pyright", "ruff", "flake8", "pylint", "black --check", "eslint",
    "cargo test", "cargo check", "cargo clippy", "go test", "go build", "go vet",
    "make test", "make check", "make lint", "pre-commit", "phpunit", "rspec",
    "rubocop", "gradle test", "mvn test", "dotnet test", "ctest",
)


# Launchers/wrappers that legitimately precede a verification tool; stripped from
# the head of each sub-command so "python -m pytest" / "npx tsc" still register.
_COMMAND_PREFIXES = (
    "sudo", "env", "time", "nice", "xvfb-run", "command", "exec", "timeout",
    "poetry run", "uv run", "pdm run", "pipenv run", "hatch run", "rye run",
    "python -m", "python3 -m", "py -m", "npx", "pnpm exec", "pnpm dlx",
)


def _is_verification_command(command: str) -> bool:
    """Does this shell command actually RUN tests / type-checks / linters / a
    build? Inspects the HEAD of each sub-command (split on shell sequencing, with
    env-assignments and known launchers stripped) rather than matching a marker
    anywhere in the string — so `git commit -m "fix ruff"`, `cat pytest.ini`, and
    `ls | grep tsc` don't register as checks. Missing an exotic wrapper only
    under-reports (the ledger is additive; the agent then falls back to its
    Definition-of-Done baseline), which is the safe direction to err."""
    for part in re.split(r"&&|\|\||;|\n|\|", command.lower()):
        tokens = part.split()
        while tokens and re.fullmatch(r"[a-z_][a-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]  # drop leading VAR=val env assignments
        head = " ".join(tokens)
        stripped = True
        while stripped:
            stripped = False
            for prefix in _COMMAND_PREFIXES:
                if head == prefix or head.startswith(prefix + " "):
                    head = head[len(prefix):].strip()
                    stripped = True
                    break
        if any(head == m or head.startswith(m + " ") for m in _VERIFICATION_MARKERS):
            return True
    return False


_SECRET_KV_RE = re.compile(
    r'(?i)\b(api[_-]?key|secret|passw(?:or)?d|token|access[_-]?token|refresh[_-]?token|'
    r'authorization|auth[_-]?token|cookie|set-cookie)\b\s*[=:]\s*\S+'
)
_SECRET_BEARER_RE = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-]+')
_SECRET_TOKEN_RE = re.compile(
    r'\b(?:(?:sk|pk|rk)-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|'
    r'xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,})\b'
)
# Inline secret-bearing env-var assignments, e.g. `PGPASSWORD=… pytest`,
# `GITHUB_TOKEN=ghp_… python -m pytest`. The bare-keyword KV regex above misses
# COMPOUND names (`API_KEY`, `DB_PASSWORD`) because the keyword isn't \b-bounded;
# require a `_`-segment secret word or a known compound, so `MONKEY=`/`AUTHOR=`
# don't false-match.
_SECRET_ENVVAR_RE = re.compile(
    # (?:[A-Z0-9]+_)+ — repeatable so MULTI-segment names (AWS_SECRET_ACCESS_KEY,
    # GOOGLE_API_KEY, STRIPE_SECRET_KEY) are caught, not just single-segment ones.
    r'(?i)\b((?:[A-Z0-9]+_)+(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?)'
    r'|PGPASSWORD|APIKEY|MYSQL_PWD)(\s*=\s*)\S+'
)


def _redact_secrets(text: str) -> str:
    """Strip obvious credentials before a failed-check output tail is PERSISTED to
    state.db and re-injected every future turn. The raw output reached the model
    in-band once already, but storing it would extend a leaked key's lifetime from
    one context window to permanent + retransmitted — so redact at capture time,
    before it touches disk. Conservative (key=value, Bearer, common token shapes);
    misses are possible but every match shrinks the leak surface."""
    text = _SECRET_BEARER_RE.sub("bearer <redacted>", text)
    text = _SECRET_KV_RE.sub(r"\1=<redacted>", text)
    text = _SECRET_ENVVAR_RE.sub(r"\1\2<redacted>", text)
    text = _SECRET_TOKEN_RE.sub("<redacted-token>", text)
    return text


def _record_execution_ledger(agent, function_name, function_args, function_result) -> None:
    """Best-effort: log a landed file mutation (write_file/patch) AND a run
    verification command (terminal) to the durable ledgers so both survive
    context compression. fail-open — a ledger write must never disrupt tool
    execution. Paired with the post-compression injection in
    conversation_compression.py so the agent neither re-edits a file it already
    finished nor forgets it still owes a re-verification.

    Runs when EITHER HERMES_EXEC_LEDGER or HERMES_DOD_GATE is set: the
    Definition-of-Done gate AND the regression-test gate consume the rows recorded
    here, so this must not silently no-op when only one of those flags is enabled.
    """
    if (
        os.getenv("HERMES_EXEC_LEDGER") != "1"
        and os.getenv("HERMES_DOD_GATE") != "1"
        and os.getenv("HERMES_REGRESSION_GATE") != "1"
    ):
        return
    db = getattr(agent, "_session_db", None)
    if db is None:
        return
    session_id = getattr(agent, "session_id", "") or ""
    try:
        if function_name in ("write_file", "patch"):
            # Prefer the structured tool RESULT as the source of truth: it carries
            # the RESOLVED absolute paths and a reliable success/error signal.
            # Keying off raw args instead both records failed writes as if they
            # landed (write_file's WriteResult emits {"bytes_written":0,...,
            # "error":...} with no "success" field) and splits one physical file
            # across path spellings. Decide structurally; fall back to args only
            # when the result isn't JSON (e.g. multimodal).
            obj = None
            if isinstance(function_result, str):
                try:
                    obj = json.loads(function_result)
                except Exception:
                    obj = None
            if isinstance(obj, dict) and (obj.get("error") or obj.get("success") is False):
                return
            if obj is None and isinstance(function_result, str):
                head = function_result.lstrip()[:64].lower()
                if head.startswith("error executing") or head.startswith("error:"):
                    return

            paths = []
            if isinstance(obj, dict):
                if obj.get("resolved_path"):
                    paths.append(obj["resolved_path"])
                for key in ("files_modified", "files_created", "files_deleted"):
                    value = obj.get(key)
                    if isinstance(value, list):
                        paths.extend(p for p in value if p)
            if not paths and isinstance(function_args, dict):
                direct = function_args.get("path") or function_args.get("file_path")
                if direct:
                    paths.append(direct)
                elif function_name == "patch" and function_args.get("mode") == "patch":
                    # V4A bulk patch (mode='patch') carries no `path` arg — the
                    # target files live inside the patch body.
                    for raw in str(function_args.get("patch") or "").splitlines():
                        line = raw.strip()
                        for marker in ("*** Update File:", "*** Add File:", "*** Delete File:"):
                            if line.startswith(marker):
                                target = line[len(marker):].strip()
                                if target:
                                    paths.append(target)
                                break

            seen = set()
            for path in paths:
                norm = os.path.normpath(path)
                if norm in seen:
                    continue
                seen.add(norm)
                db.record_file_mutation(session_id, norm, function_name)

        elif function_name == "terminal":
            if not isinstance(function_args, dict):
                return
            command = (function_args.get("command") or "").strip()
            if not command or not _is_verification_command(command):
                return
            # Opportunistically read exit_code + output from the JSON result;
            # unknown is OK (the command is still recorded so the agent remembers
            # it ran).
            exit_code = None
            output_text = None
            if isinstance(function_result, str):
                try:
                    obj = json.loads(function_result)
                    if isinstance(obj, dict):
                        if isinstance(obj.get("exit_code"), int):
                            exit_code = obj["exit_code"]
                        if isinstance(obj.get("output"), str):
                            output_text = obj["output"]
                except Exception:
                    exit_code = None
            # The terminal tool uses a negative exit code (-1) as its sentinel for
            # "never ran" (disabled / pending approval / blocked / invalid command),
            # not a real verdict (real codes are 0–255). Recording it would stamp a
            # spurious FAIL on a command that didn't execute — treat as unknown.
            if exit_code is not None and exit_code < 0:
                exit_code = None
            # Keep the diagnostic tail only for a non-pass (fail/unknown) — a clean
            # pass needs no traceback, and passing None lets the store CLEAR a stale
            # one. The tail is where tracebacks/assertions live.
            output_snippet = None
            if output_text and exit_code != 0:
                output_snippet = _redact_secrets(output_text[-500:])
            # Redact the COMMAND too: it is persisted + re-injected every turn, and
            # `_is_verification_command` accepts env-prefixed checks (e.g.
            # `PGPASSWORD=… pytest`) — so the command can itself carry a credential.
            db.record_verification(
                session_id, _redact_secrets(command), exit_code, output_snippet=output_snippet
            )
    except Exception:
        pass  # ledger is advisory; never break the tool path


def execute_tool_calls_concurrent(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0) -> None:
    """Execute multiple tool calls concurrently using a thread pool.

    Results are collected in the original tool-call order and appended to
    messages so the API sees them in the expected sequence.
    """
    tool_calls = assistant_message.tool_calls
    num_tools = len(tool_calls)

    # ── Pre-flight: interrupt check ──────────────────────────────────
    if agent._interrupt_requested:
        print(f"{agent.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
        for tc in tool_calls:
            messages.append(make_tool_result_message(
                tc.function.name,
                f"[Tool execution cancelled — {tc.function.name} was skipped due to user interrupt]",
                tc.id,
            ))
        return

    # ── Parse args + pre-execution bookkeeping ───────────────────────
    parsed_calls = []  # list of (tool_call, function_name, function_args, middleware_trace, block_result, blocked_by_guardrail)
    for tool_call in tool_calls:
        function_name = tool_call.function.name

        # Reset nudge counters
        if function_name == "memory":
            agent._turns_since_memory = 0
        elif function_name == "skill_manage":
            agent._iters_since_skill = 0

        try:
            function_args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            function_args = {}
        if not isinstance(function_args, dict):
            function_args = {}

        # ── Tool Search unwrap ────────────────────────────────────────
        # When the model invokes the tool_call bridge, peel it open so
        # every downstream check (checkpointing, guardrails, plugin
        # pre-tool-call hooks, the display/activity feed, the post-call
        # callback) sees the underlying tool — not the bridge. This is
        # the OpenClaw lesson: hooks must observe the real tool name.
        #
        # The original tool_call entry on ``tool_call.function`` is left
        # untouched so the conversation transcript and the matching
        # tool_call_id are preserved exactly as the model emitted them.
        #
        # Scope gate: the unwrap dispatches the underlying tool directly
        # (bypassing the bridge branch in handle_function_call and its
        # scope check), so we enforce session toolset scope HERE. A tool
        # the session was not granted is rejected before any checkpoint,
        # hook, or dispatch fires.
        _ts_scope_block = None
        try:
            from tools import tool_search as _ts
            if function_name == _ts.TOOL_CALL_NAME:
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
                        function_name = _underlying
                        function_args = _underlying_args
                    else:
                        _ts_scope_block = json.dumps({
                            "error": (
                                f"'{_underlying}' is not available in this session. "
                                "Use tool_search to find tools you can call."
                            ),
                        }, ensure_ascii=False)
        except Exception:
            pass

        function_args, middleware_trace = _apply_tool_request_middleware_for_agent(
            agent,
            function_name=function_name,
            function_args=function_args,
            effective_task_id=effective_task_id,
            tool_call_id=getattr(tool_call, "id", "") or "",
        )

        # ── Block evaluation (BEFORE checkpoint preflight) ───────────
        # We must know whether the tool will execute before touching
        # checkpoint state (dedup slot, real snapshots).
        block_result = None
        blocked_by_guardrail = False
        if _ts_scope_block is not None:
            # Out-of-scope tool_call: reject before hooks/guardrails/dispatch.
            block_result = _ts_scope_block
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=block_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type="tool_scope_block",
                error_message=_ts_scope_block,
                middleware_trace=list(middleware_trace),
            )
        else:
            try:
                from hermes_cli.plugins import get_pre_tool_call_block_message
                block_message = get_pre_tool_call_block_message(
                    function_name,
                    function_args,
                    task_id=effective_task_id or "",
                    session_id=getattr(agent, "session_id", "") or "",
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    turn_id=getattr(agent, "_current_turn_id", "") or "",
                    api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                    middleware_trace=list(middleware_trace),
                )
            except Exception:
                block_message = None

            if block_message is not None:
                block_result = json.dumps({"error": block_message}, ensure_ascii=False)
                _emit_terminal_post_tool_call(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    result=block_result,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    status="blocked",
                    error_type="plugin_block",
                    error_message=block_message,
                    middleware_trace=list(middleware_trace),
                )
            else:
                guardrail_decision = agent._tool_guardrails.before_call(function_name, function_args)
                if not guardrail_decision.allows_execution:
                    block_result = agent._guardrail_block_result(guardrail_decision)
                    blocked_by_guardrail = True
                    _emit_terminal_post_tool_call(
                        agent,
                        function_name=function_name,
                        function_args=function_args,
                        result=block_result,
                        effective_task_id=effective_task_id,
                        tool_call_id=getattr(tool_call, "id", "") or "",
                        status="blocked",
                        error_type="guardrail_block",
                        error_message=getattr(guardrail_decision, "message", None) or "Tool blocked by guardrail policy",
                        middleware_trace=list(middleware_trace),
                    )

        # ── Checkpoint preflight (only for tools that will execute) ──
        if block_result is None:
            # Checkpoint for file-mutating tools
            if function_name in {"write_file", "patch"} and agent._checkpoint_mgr.enabled:
                try:
                    file_path = function_args.get("path", "")
                    if file_path:
                        work_dir = agent._checkpoint_mgr.get_working_dir_for_path(file_path)
                        agent._checkpoint_mgr.ensure_checkpoint(work_dir, f"before {function_name}")
                except Exception:
                    pass

            # Checkpoint before destructive terminal commands
            if function_name == "terminal" and agent._checkpoint_mgr.enabled:
                try:
                    cmd = function_args.get("command", "")
                    if _is_destructive_command(cmd):
                        cwd = function_args.get("workdir") or os.getenv("TERMINAL_CWD", os.getcwd())
                        agent._checkpoint_mgr.ensure_checkpoint(
                            cwd, f"before terminal: {cmd[:60]}"
                        )
                except Exception:
                    pass

        parsed_calls.append((tool_call, function_name, function_args, middleware_trace, block_result, blocked_by_guardrail))

    # ── Logging / callbacks ──────────────────────────────────────────
    tool_names_str = ", ".join(name for _, name, _, _, _, _ in parsed_calls)
    if not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
        print(f"  ⚡ Concurrent: {num_tools} tool calls — {tool_names_str}")
        for i, (tc, name, args, middleware_trace, block_result, blocked_by_guardrail) in enumerate(parsed_calls, 1):
            args_str = json.dumps(args, ensure_ascii=False)
            if agent.verbose_logging:
                print(f"  📞 Tool {i}: {name}({list(args.keys())})")
                print(agent._wrap_verbose("Args: ", json.dumps(args, indent=2, ensure_ascii=False)))
            else:
                args_preview = args_str[:agent.log_prefix_chars] + "..." if len(args_str) > agent.log_prefix_chars else args_str
                print(f"  📞 Tool {i}: {name}({list(args.keys())}) - {args_preview}")

    for tc, name, args, middleware_trace, block_result, blocked_by_guardrail in parsed_calls:
        if block_result is not None:
            continue
        if agent.tool_progress_callback:
            try:
                preview = _build_tool_preview(name, args)
                agent.tool_progress_callback("tool.started", name, preview, args)
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

    for tc, name, args, middleware_trace, block_result, blocked_by_guardrail in parsed_calls:
        if block_result is not None:
            continue
        if agent.tool_start_callback:
            try:
                agent.tool_start_callback(tc.id, name, args)
            except Exception as cb_err:
                logging.debug(f"Tool start callback error: {cb_err}")

    # ── Concurrent execution ─────────────────────────────────────────
    # Each slot holds (function_name, function_args, function_result, duration, error_flag, blocked_flag, middleware_trace)
    results = [None] * num_tools
    for i, (tc, name, args, middleware_trace, block_result, blocked_by_guardrail) in enumerate(parsed_calls):
        if block_result is not None:
            results[i] = (name, args, block_result, 0.0, True, True, middleware_trace)

    # Touch activity before launching workers so the gateway knows
    # we're executing tools (not stuck).
    agent._current_tool = tool_names_str
    agent._touch_activity(f"executing {num_tools} tools concurrently: {tool_names_str}")

    def _run_tool(index, tool_call, function_name, function_args, middleware_trace):
        """Worker function executed in a thread."""
        # Register this worker tid so the agent can fan out an interrupt
        # to it — see AIAgent.interrupt().  Must happen first thing, and
        # must be paired with discard + clear in the finally block.
        _worker_tid = threading.current_thread().ident
        with agent._tool_worker_threads_lock:
            agent._tool_worker_threads.add(_worker_tid)
        # Race: if the agent was interrupted between fan-out (which
        # snapshotted an empty/earlier set) and our registration, apply
        # the interrupt to our own tid now so is_interrupted() inside
        # the tool returns True on the next poll.
        if agent._interrupt_requested:
            try:
                _ra()._set_interrupt(True, _worker_tid)
            except Exception:
                pass
        # Set the activity callback on THIS worker thread so
        # _wait_for_process (terminal commands) can fire heartbeats.
        # The callback is thread-local; the main thread's callback
        # is invisible to worker threads.
        try:
            from tools.environments.base import set_activity_callback
            set_activity_callback(agent._touch_activity)
        except Exception:
            pass
        # Approval/sudo callbacks (thread-local) and the agent turn's
        # ContextVars are propagated by propagate_context_to_thread() at the
        # submit site below (GHSA-qg5c-hvr5-hjgr, #13617).
        start = time.time()
        try:
            try:
                result = agent._invoke_tool(
                    function_name,
                    function_args,
                    effective_task_id,
                    tool_call.id,
                    messages=messages,
                    pre_tool_block_checked=True,
                    skip_tool_request_middleware=True,
                    tool_request_middleware_trace=list(middleware_trace),
                )
            except KeyboardInterrupt:
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                result = _emit_cancelled_terminal_post_tool_call(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    start_time=start,
                    middleware_trace=list(middleware_trace),
                )
                duration = time.time() - start
                logger.info("tool %s cancelled (%.2fs)", function_name, duration)
                # A user/keyboard cancellation is NOT a tool failure: is_error
                # must be False so it isn't fed to RuntimeFeedback as a failure,
                # isn't recorded as a failed file mutation, and doesn't surface as
                # is_error in the progress feed. Keep blocked=False so the
                # downstream tool.completed event still fires and closes the
                # progress entry (tool.started was already emitted pre-submit).
                results[index] = (function_name, function_args, result, duration, False, False, middleware_trace)
                return
            except Exception as tool_error:
                result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("_invoke_tool raised for %s: %s", function_name, tool_error, exc_info=True)
            duration = time.time() - start
            is_error, _ = _detect_tool_failure(function_name, result)
            if is_error:
                logger.info("tool %s failed (%.2fs): %s", function_name, duration, result[:200])
            else:
                logger.info("tool %s completed (%.2fs, %d chars)", function_name, duration, len(result))
            results[index] = (function_name, function_args, result, duration, is_error, False, middleware_trace)
        finally:
            # Tear down worker-tid tracking.  Clear any interrupt bit we may
            # have set so the next task scheduled onto this recycled tid
            # starts with a clean slate.  This MUST be in a finally block
            # because BaseException subclasses (CancelledError, KeyboardInterrupt)
            # bypass ``except Exception`` and would otherwise leak the tid
            # into _interrupted_threads, poisoning the recycled thread.
            with agent._tool_worker_threads_lock:
                agent._tool_worker_threads.discard(_worker_tid)
            try:
                _ra()._set_interrupt(False, _worker_tid)
            except Exception:
                pass

    # Start spinner for CLI mode (skip when TUI handles tool progress)
    spinner = None
    if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
        face = random.choice(KawaiiSpinner.get_waiting_faces())
        spinner = KawaiiSpinner(f"{face} ⚡ running {num_tools} tools concurrently", spinner_type='dots', print_fn=agent._print_fn)
        spinner.start()

    try:
        runnable_calls = [
            (i, tc, name, args)
            for i, (tc, name, args, middleware_trace, block_result, blocked_by_guardrail) in enumerate(parsed_calls)
            if block_result is None
        ]
        futures = []
        if runnable_calls:
            max_workers = min(len(runnable_calls), _MAX_TOOL_WORKERS)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                for i, tc, name, args in runnable_calls:
                    # Propagate the agent turn's ContextVars (e.g.
                    # _approval_session_key) AND thread-local approval/sudo
                    # callbacks into the worker thread; clears callbacks on exit.
                    f = executor.submit(
                        propagate_context_to_thread(_run_tool), i, tc, name, args, parsed_calls[i][3]
                    )
                    futures.append(f)

                # Wait for all to complete with periodic heartbeats so the
                # gateway's inactivity monitor doesn't kill us during long
                # concurrent tool batches. Also check for user interrupts
                # so we don't block indefinitely when the user sends /stop
                # or a new message during concurrent tool execution.
                _conc_start = time.time()
                _interrupt_logged = False
                while True:
                    done, not_done = concurrent.futures.wait(
                        futures, timeout=5.0,
                    )
                    if not not_done:
                        break

                    # Check for interrupt — the per-thread interrupt signal
                    # already causes individual tools (terminal, execute_code)
                    # to abort, but tools without interrupt checks (web_search,
                    # read_file) will run to completion. Cancel any futures
                    # that haven't started yet so we don't block on them.
                    if agent._interrupt_requested:
                        if not _interrupt_logged:
                            _interrupt_logged = True
                            agent._vprint(
                                f"{agent.log_prefix}⚡ Interrupt: cancelling "
                                f"{len(not_done)} pending concurrent tool(s)",
                                force=True,
                            )
                        for f in not_done:
                            f.cancel()
                        # Give already-running tools a moment to notice the
                        # per-thread interrupt signal and exit gracefully.
                        concurrent.futures.wait(not_done, timeout=3.0)
                        break

                    _conc_elapsed = int(time.time() - _conc_start)
                    # Heartbeat every ~30s (6 × 5s poll intervals)
                    if _conc_elapsed > 0 and _conc_elapsed % 30 < 6:
                        _still_running = _running_tool_names(not_done, futures, runnable_calls)
                        agent._touch_activity(
                            f"concurrent tools running ({_conc_elapsed}s, "
                            f"{len(not_done)} remaining: {', '.join(_still_running[:3])})"
                        )
    finally:
        if spinner:
            # Build a summary message for the spinner stop
            completed = sum(1 for r in results if r is not None)
            total_dur = sum(r[3] for r in results if r is not None)
            spinner.stop(f"⚡ {completed}/{num_tools} tools completed in {total_dur:.1f}s total")

    # ── Post-execution: display per-tool results ─────────────────────
    for i, (tc, name, args, middleware_trace, block_result, blocked_by_guardrail) in enumerate(parsed_calls):
        r = results[i]
        blocked = False
        if r is None:
            # Tool was cancelled (interrupt) or thread didn't return
            if agent._interrupt_requested:
                function_result = f"[Tool execution cancelled — {name} was skipped due to user interrupt]"
                _emit_terminal_post_tool_call(
                    agent,
                    function_name=name,
                    function_args=args,
                    result=function_result,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tc, "id", "") or "",
                    status="cancelled",
                    error_type="keyboard_interrupt",
                    error_message="Tool execution cancelled by user interrupt",
                    middleware_trace=list(middleware_trace),
                )
            else:
                function_result = f"Error executing tool '{name}': thread did not return a result"
                _emit_terminal_post_tool_call(
                    agent,
                    function_name=name,
                    function_args=args,
                    result=function_result,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tc, "id", "") or "",
                    status="error",
                    error_type="thread_missing_result",
                    error_message=function_result,
                    middleware_trace=list(middleware_trace),
                )
            tool_duration = 0.0
            # Close the progress entry: tool.started was already emitted for
            # every call in the pre-submit loop, but a tool cancelled before its
            # worker stored a result (r is None) never fired tool.completed,
            # leaking a perpetually-"running" entry in the progress feed. Emit it
            # now (is_error=False — a cancellation is not a failure).
            if agent.tool_progress_callback:
                try:
                    agent.tool_progress_callback(
                        "tool.completed", name, None, None,
                        duration=0.0, is_error=False, result=function_result,
                    )
                except Exception as cb_err:
                    logging.debug(f"Tool progress callback error (cancelled): {cb_err}")
        else:
            function_name, function_args, function_result, tool_duration, is_error, blocked, middleware_trace = r

            if not blocked:
                function_result = agent._append_guardrail_observation(
                    function_name,
                    function_args,
                    function_result,
                    failed=is_error,
                )

            if is_error:
                _err_text = _multimodal_text_summary(function_result)
                result_preview = _err_text[:200] if len(_err_text) > 200 else _err_text
                logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, result_preview)

            # Milestone 1: record tool outcome for runtime feedback.
            if not blocked:
                _record_tool_outcome_safe(agent, function_name, is_error)

            # Track file-mutation outcome for the turn-end verifier.
            # `blocked` calls never actually ran — don't let a guardrail
            # block count as either a failure or a success.
            if not blocked:
                try:
                    agent._record_file_mutation_result(
                        function_name, function_args, function_result, is_error,
                    )
                except Exception as _ver_err:
                    logging.debug("file-mutation verifier record failed: %s", _ver_err)

            if not blocked and agent.tool_progress_callback:
                try:
                    agent.tool_progress_callback(
                        "tool.completed", function_name, None, None,
                        duration=tool_duration, is_error=is_error,
                        result=function_result,
                    )
                except Exception as cb_err:
                    logging.debug(f"Tool progress callback error: {cb_err}")

            if agent.verbose_logging:
                logging.debug(f"Tool {function_name} completed in {tool_duration:.2f}s")
                logging.debug(f"Tool result ({len(function_result)} chars): {function_result}")

        # Print cute message per tool
        if agent._should_emit_quiet_tool_messages():
            cute_msg = _get_cute_tool_message_impl(name, args, tool_duration, result=function_result)
            agent._safe_print(f"  {cute_msg}")
        elif not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
            _preview_str = _multimodal_text_summary(function_result)
            if agent.verbose_logging:
                print(f"  ✅ Tool {i+1} completed in {tool_duration:.2f}s")
                print(agent._wrap_verbose("Result: ", _preview_str))
            else:
                response_preview = _preview_str[:agent.log_prefix_chars] + "..." if len(_preview_str) > agent.log_prefix_chars else _preview_str
                print(f"  ✅ Tool {i+1} completed in {tool_duration:.2f}s - {response_preview}")

        agent._current_tool = None
        agent._touch_activity(f"tool completed: {name} ({tool_duration:.1f}s)")

        if not blocked and agent.tool_complete_callback:
            try:
                agent.tool_complete_callback(tc.id, name, args, function_result)
            except Exception as cb_err:
                logging.debug(f"Tool complete callback error: {cb_err}")

        function_result = maybe_persist_tool_result(
            content=function_result,
            tool_name=name,
            tool_use_id=tc.id,
            env=get_active_env(effective_task_id),
        ) if not _is_multimodal_tool_result(function_result) else function_result

        _record_execution_ledger(agent, name, args, function_result)

        subdir_hints = agent._subdirectory_hints.check_tool_call(name, args)
        if subdir_hints:
            if _is_multimodal_tool_result(function_result):
                # Append the hint to the text summary part so the model
                # still sees it; don't touch the image blocks.
                _append_subdir_hint_to_multimodal(function_result, subdir_hints)
            else:
                function_result += subdir_hints

        # Unwrap _multimodal dicts to an OpenAI-style content list so any
        # vision-capable provider receives [{type:text},{type:image_url}]
        # rather than a raw Python dict.  The Anthropic adapter already
        # accepts content lists; vision-capable OpenAI-compatible servers
        # (mlx-vlm, GPT-4o, …) accept image_url in tool messages natively.
        # Text-only servers get a string-safe fallback here so a rejected
        # image tool result never poisons canonical session history.
        # String results pass through unchanged.
        _tool_content = agent._tool_result_content_for_active_model(name, function_result)
        messages.append(make_tool_result_message(name, _tool_content, tc.id))

        # ── Per-tool /steer drain ───────────────────────────────────
        # Same as the sequential path: drain between each collected
        # result so the steer lands as early as possible.
        agent._apply_pending_steer_to_tool_results(messages, 1)

    # ── Per-turn aggregate budget enforcement ─────────────────────────
    num_tools = len(parsed_calls)
    if num_tools > 0:
        turn_tool_msgs = messages[-num_tools:]
        enforce_turn_budget(turn_tool_msgs, env=get_active_env(effective_task_id))

    # ── /steer injection ──────────────────────────────────────────────
    # Append any pending user steer text to the last tool result so the
    # agent sees it on its next iteration. Runs AFTER budget enforcement
    # so the steer marker is never truncated. See steer() for details.
    if num_tools > 0:
        agent._apply_pending_steer_to_tool_results(messages, num_tools)



def execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0) -> None:
    """Execute tool calls sequentially (original behavior). Used for single calls or interactive tools."""
    for i, tool_call in enumerate(assistant_message.tool_calls, 1):
        # SAFETY: check interrupt BEFORE starting each tool.
        # If the user sent "stop" during a previous tool's execution,
        # do NOT start any more tools -- skip them all immediately.
        if agent._interrupt_requested:
            remaining_calls = assistant_message.tool_calls[i-1:]
            if remaining_calls:
                agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {len(remaining_calls)} tool call(s)", force=True)
            for skipped_tc in remaining_calls:
                skipped_name = skipped_tc.function.name
                # Build via make_tool_result_message so the persisted message
                # carries the internal 'tool_name' field (and goes through the
                # same untrusted-wrap path) like every other tool result — the
                # raw dict here persisted tool_name=NULL only for interrupt-
                # skipped messages, which downstream consumers read as missing.
                messages.append(make_tool_result_message(
                    skipped_name,
                    f"[Tool execution cancelled — {skipped_name} was skipped due to user interrupt]",
                    skipped_tc.id,
                ))
            break

        function_name = tool_call.function.name

        try:
            function_args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as e:
            logger.warning(f"Unexpected JSON error after validation: {e}")
            function_args = {}
        if not isinstance(function_args, dict):
            function_args = {}

        # Tool Search unwrap — see execute_tool_calls_concurrent for full
        # rationale, including the scope gate (the unwrap dispatches the
        # underlying tool directly, so session toolset scope is enforced here).
        _ts_scope_block: Optional[str] = None
        try:
            from tools import tool_search as _ts
            if function_name == _ts.TOOL_CALL_NAME:
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
                        function_name = _underlying
                        function_args = _underlying_args
                    else:
                        _ts_scope_block = (
                            f"'{_underlying}' is not available in this session. "
                            "Use tool_search to find tools you can call."
                        )
        except Exception:
            pass

        function_args, middleware_trace = _apply_tool_request_middleware_for_agent(
            agent,
            function_name=function_name,
            function_args=function_args,
            effective_task_id=effective_task_id,
            tool_call_id=getattr(tool_call, "id", "") or "",
        )

        # Check plugin hooks for a block directive before executing.
        _block_msg: Optional[str] = None
        _block_error_type = "plugin_block"
        if _ts_scope_block is not None:
            _block_msg = _ts_scope_block
            _block_error_type = "tool_scope_block"
        else:
            try:
                from hermes_cli.plugins import get_pre_tool_call_block_message
                _block_msg = get_pre_tool_call_block_message(
                    function_name,
                    function_args,
                    task_id=effective_task_id or "",
                    session_id=getattr(agent, "session_id", "") or "",
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    turn_id=getattr(agent, "_current_turn_id", "") or "",
                    api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                    middleware_trace=list(middleware_trace),
                )
            except Exception:
                pass

        _guardrail_block_decision: ToolGuardrailDecision | None = None
        if _block_msg is None:
            guardrail_decision = agent._tool_guardrails.before_call(function_name, function_args)
            if not guardrail_decision.allows_execution:
                _guardrail_block_decision = guardrail_decision

        _execution_blocked = _block_msg is not None or _guardrail_block_decision is not None

        if _execution_blocked:
            # Tool blocked by plugin or guardrail policy — skip counters,
            # callbacks, checkpointing, activity mutation, and real execution.
            pass
        # Reset nudge counters when the relevant tool is actually used
        elif function_name == "memory":
            agent._turns_since_memory = 0
        elif function_name == "skill_manage":
            agent._iters_since_skill = 0

        if not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
            args_str = json.dumps(function_args, ensure_ascii=False)
            if agent.verbose_logging:
                print(f"  📞 Tool {i}: {function_name}({list(function_args.keys())})")
                print(agent._wrap_verbose("Args: ", json.dumps(function_args, indent=2, ensure_ascii=False)))
            else:
                args_preview = args_str[:agent.log_prefix_chars] + "..." if len(args_str) > agent.log_prefix_chars else args_str
                print(f"  📞 Tool {i}: {function_name}({list(function_args.keys())}) - {args_preview}")

        if not _execution_blocked:
            agent._current_tool = function_name
            agent._touch_activity(f"executing tool: {function_name}")

        # Set activity callback for long-running tool execution (terminal
        # commands, etc.) so the gateway's inactivity monitor doesn't kill
        # the agent while a command is running.
        if not _execution_blocked:
            try:
                from tools.environments.base import set_activity_callback
                set_activity_callback(agent._touch_activity)
            except Exception:
                pass

        if not _execution_blocked and agent.tool_progress_callback:
            try:
                preview = _build_tool_preview(function_name, function_args)
                agent.tool_progress_callback("tool.started", function_name, preview, function_args)
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

        if not _execution_blocked and agent.tool_start_callback:
            try:
                agent.tool_start_callback(tool_call.id, function_name, function_args)
            except Exception as cb_err:
                logging.debug(f"Tool start callback error: {cb_err}")

        # Checkpoint: snapshot working dir before file-mutating tools
        if not _execution_blocked and function_name in {"write_file", "patch"} and agent._checkpoint_mgr.enabled:
            try:
                file_path = function_args.get("path", "")
                if file_path:
                    work_dir = agent._checkpoint_mgr.get_working_dir_for_path(file_path)
                    agent._checkpoint_mgr.ensure_checkpoint(
                        work_dir, f"before {function_name}"
                    )
            except Exception:
                pass  # never block tool execution

        # Checkpoint before destructive terminal commands
        if not _execution_blocked and function_name == "terminal" and agent._checkpoint_mgr.enabled:
            try:
                cmd = function_args.get("command", "")
                if _is_destructive_command(cmd):
                    cwd = function_args.get("workdir") or os.getenv("TERMINAL_CWD", os.getcwd())
                    agent._checkpoint_mgr.ensure_checkpoint(
                        cwd, f"before terminal: {cmd[:60]}"
                    )
            except Exception:
                pass  # never block tool execution

        tool_start_time = time.time()

        if _block_msg is not None:
            # Tool blocked by plugin policy — return error without executing.
            function_result = json.dumps({"error": _block_msg}, ensure_ascii=False)
            tool_duration = 0.0
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=function_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type=_block_error_type,
                error_message=_block_msg,
                middleware_trace=list(middleware_trace),
            )
        elif _guardrail_block_decision is not None:
            # Tool blocked by tool-loop guardrail — synthesize exactly one
            # tool result for the original tool_call_id without executing.
            function_result = agent._guardrail_block_result(_guardrail_block_decision)
            tool_duration = 0.0
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=function_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                status="blocked",
                error_type="guardrail_block",
                error_message=getattr(_guardrail_block_decision, "message", None) or "Tool blocked by guardrail policy",
                middleware_trace=list(middleware_trace),
            )
        elif function_name == "todo":
            def _execute(next_args: dict) -> Any:
                from tools.todo_tool import todo_tool as _todo_tool
                return _todo_tool(
                    todos=next_args.get("todos"),
                    merge=next_args.get("merge", False),
                    store=agent._todo_store,
                )
            function_result, function_args = _run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('todo', function_args, tool_duration, result=function_result)}")
        elif function_name == "session_search":
            def _execute(next_args: dict) -> Any:
                session_db = agent._get_session_db_for_recall()
                if not session_db:
                    from hermes_state import format_session_db_unavailable
                    return json.dumps({"success": False, "error": format_session_db_unavailable()})
                from tools.session_search_tool import session_search as _session_search
                return _session_search(
                    query=next_args.get("query", ""),
                    role_filter=next_args.get("role_filter"),
                    limit=next_args.get("limit", 3),
                    session_id=next_args.get("session_id"),
                    around_message_id=next_args.get("around_message_id"),
                    window=next_args.get("window", 5),
                    sort=next_args.get("sort"),
                    db=session_db,
                    current_session_id=agent.session_id,
                )
            function_result, function_args = _run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('session_search', function_args, tool_duration, result=function_result)}")
        elif function_name == "memory":
            def _execute(next_args: dict) -> Any:
                target = next_args.get("target", "memory")
                from tools.memory_tool import memory_tool as _memory_tool
                result = _memory_tool(
                    action=next_args.get("action"),
                    target=target,
                    content=next_args.get("content"),
                    old_text=next_args.get("old_text"),
                    store=agent._memory_store,
                )
                # Bridge: notify external memory provider of built-in memory writes
                if agent._memory_manager and next_args.get("action") in {"add", "replace"}:
                    try:
                        agent._memory_manager.on_memory_write(
                            next_args.get("action", ""),
                            target,
                            next_args.get("content", ""),
                            metadata=agent._build_memory_write_metadata(
                                task_id=effective_task_id,
                                tool_call_id=getattr(tool_call, "id", None),
                            ),
                        )
                    except Exception:
                        pass
                return result
            function_result, function_args = _run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('memory', function_args, tool_duration, result=function_result)}")
        elif function_name == "clarify":
            def _execute(next_args: dict) -> Any:
                from tools.clarify_tool import clarify_tool as _clarify_tool
                return _clarify_tool(
                    question=next_args.get("question", ""),
                    choices=next_args.get("choices"),
                    callback=agent.clarify_callback,
                )
            function_result, function_args = _run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('clarify', function_args, tool_duration, result=function_result)}")
        elif function_name == "read_terminal":
            def _execute(next_args: dict) -> Any:
                from tools.read_terminal_tool import read_terminal_tool as _read_terminal_tool
                return _read_terminal_tool(
                    start_line=next_args.get("start_line"),
                    count=next_args.get("count"),
                    callback=getattr(agent, "read_terminal_callback", None),
                )
            function_result, function_args = _run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('read_terminal', function_args, tool_duration, result=function_result)}")
        elif function_name == "delegate_task":
            tasks_arg = function_args.get("tasks")
            if tasks_arg and isinstance(tasks_arg, list):
                spinner_label = f"🔀 delegating {len(tasks_arg)} tasks · (/agents to monitor)"
            else:
                goal_preview = (function_args.get("goal") or "")[:30]
                spinner_label = (
                    f"🔀 {goal_preview} · (/agents to monitor)"
                    if goal_preview
                    else "🔀 delegating · (/agents to monitor)"
                )
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                spinner = KawaiiSpinner(f"{face} {spinner_label}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            agent._delegate_spinner = spinner
            _delegate_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return agent._dispatch_delegate_task(next_args)
                function_result, function_args = _run_agent_tool_execution_middleware(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=_execute,
                )
                _delegate_result = function_result
            finally:
                agent._delegate_spinner = None
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl('delegate_task', function_args, tool_duration, result=_delegate_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent._context_engine_tool_names and function_name in agent._context_engine_tool_names:
            # Context engine tools (lcm_grep, lcm_describe, lcm_expand, etc.)
            spinner = None
            if agent._should_emit_quiet_tool_messages():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                preview = _build_tool_preview(function_name, function_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _ce_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return agent.context_compressor.handle_tool_call(function_name, next_args, messages=messages)
                function_result, function_args = _run_agent_tool_execution_middleware(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=_execute,
                )
                _ce_result = function_result
            except Exception as tool_error:
                function_result = json.dumps({"error": f"Context engine tool '{function_name}' failed: {tool_error}"})
                logger.error("context_engine.handle_tool_call raised for %s: %s", function_name, tool_error, exc_info=True)
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=_ce_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent._memory_manager and agent._memory_manager.has_tool(function_name):
            # Memory provider tools (hindsight_retain, honcho_search, etc.)
            # These are not in the tool registry — route through MemoryManager.
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                preview = _build_tool_preview(function_name, function_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _mem_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return agent._memory_manager.handle_tool_call(function_name, next_args)
                function_result, function_args = _run_agent_tool_execution_middleware(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=_execute,
                )
                _mem_result = function_result
            except Exception as tool_error:
                function_result = json.dumps({"error": f"Memory tool '{function_name}' failed: {tool_error}"})
                logger.error("memory_manager.handle_tool_call raised for %s: %s", function_name, tool_error, exc_info=True)
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=_mem_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent.quiet_mode:
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                preview = _build_tool_preview(function_name, function_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _spinner_result = None
            try:
                function_result = _ra().handle_function_call(
                    function_name, function_args, effective_task_id,
                    tool_call_id=tool_call.id,
                    session_id=agent.session_id or "",
                    turn_id=getattr(agent, "_current_turn_id", "") or "",
                    api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                    enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                    skip_pre_tool_call_hook=True,
                    skip_tool_request_middleware=True,
                    enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                    disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                    tool_request_middleware_trace=list(middleware_trace),
                )
                _spinner_result = function_result
            except KeyboardInterrupt:
                function_result = _emit_cancelled_terminal_post_tool_call(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    start_time=tool_start_time,
                    middleware_trace=list(middleware_trace),
                )
                _spinner_result = function_result
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                raise
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("handle_function_call raised for %s: %s", function_name, tool_error, exc_info=True)
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=_spinner_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        else:
            try:
                function_result = _ra().handle_function_call(
                    function_name, function_args, effective_task_id,
                    tool_call_id=tool_call.id,
                    session_id=agent.session_id or "",
                    turn_id=getattr(agent, "_current_turn_id", "") or "",
                    api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                    enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                    skip_pre_tool_call_hook=True,
                    skip_tool_request_middleware=True,
                    enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                    disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                    tool_request_middleware_trace=list(middleware_trace),
                )
            except KeyboardInterrupt:
                _emit_cancelled_terminal_post_tool_call(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    start_time=tool_start_time,
                    middleware_trace=list(middleware_trace),
                )
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                raise
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("handle_function_call raised for %s: %s", function_name, tool_error, exc_info=True)
            tool_duration = time.time() - tool_start_time

        if isinstance(function_result, str):
            result_preview = function_result if agent.verbose_logging else (
                function_result[:200] if len(function_result) > 200 else function_result
            )
            _result_len = len(function_result)
        else:
            # Multimodal dict result (_multimodal=True) — not sliceable as string
            result_preview = function_result
            _result_len = len(str(function_result))

        # Log tool errors to the persistent error log so [error] tags
        # in the UI always have a corresponding detailed entry on disk.
        _is_error_result, _ = _detect_tool_failure(function_name, function_result)
        # The agent-runtime tools above (todo, session_search, memory,
        # context-engine, memory-manager, clarify, delegate_task) are
        # dispatched inline — they never reach handle_function_call, so the
        # executor is the one that has to fire post_tool_call. For
        # registry-dispatched tools the else-branch above invoked
        # handle_function_call, which already fires the hook.
        from agent.agent_runtime_helpers import agent_runtime_owns_post_tool_hook
        _executor_must_emit_post_hook = (
            not _execution_blocked
            and agent_runtime_owns_post_tool_hook(agent, function_name)
        )
        if _executor_must_emit_post_hook:
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=function_result,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                duration_ms=int(tool_duration * 1000),
                middleware_trace=list(middleware_trace),
            )
        if not _execution_blocked:
            function_result = agent._append_guardrail_observation(
                function_name,
                function_args,
                function_result,
                failed=_is_error_result,
            )
            result_preview = function_result if agent.verbose_logging else (
                function_result[:200] if len(function_result) > 200 else function_result
            )
        if _is_error_result:
            logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, result_preview)
        else:
            logger.info("tool %s completed (%.2fs, %d chars)", function_name, tool_duration, _result_len)

        # Milestone 1: record tool outcome for runtime feedback.
        if not _execution_blocked:
            _record_tool_outcome_safe(agent, function_name, _is_error_result)

        # Track file-mutation outcome for the turn-end verifier.  See
        # the concurrent path for the rationale; both paths must feed
        # the same state so the footer reflects every tool call in the
        # turn, not just the parallel ones.
        if not _execution_blocked:
            try:
                agent._record_file_mutation_result(
                    function_name, function_args, function_result, _is_error_result,
                )
            except Exception as _ver_err:
                logging.debug("file-mutation verifier record failed: %s", _ver_err)

        if not _execution_blocked and agent.tool_progress_callback:
            try:
                agent.tool_progress_callback(
                    "tool.completed", function_name, None, None,
                    duration=tool_duration, is_error=_is_error_result,
                    result=function_result,
                )
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

        agent._current_tool = None
        agent._touch_activity(f"tool completed: {function_name} ({tool_duration:.1f}s)")

        if agent.verbose_logging:
            logging.debug(f"Tool {function_name} completed in {tool_duration:.2f}s")
            _log_result = _multimodal_text_summary(function_result)
            logging.debug(f"Tool result ({len(_log_result)} chars): {_log_result}")

        if not _execution_blocked and agent.tool_complete_callback:
            try:
                agent.tool_complete_callback(tool_call.id, function_name, function_args, function_result)
            except Exception as cb_err:
                logging.debug(f"Tool complete callback error: {cb_err}")

        function_result = maybe_persist_tool_result(
            content=function_result,
            tool_name=function_name,
            tool_use_id=tool_call.id,
            env=get_active_env(effective_task_id),
        ) if not _is_multimodal_tool_result(function_result) else function_result

        _record_execution_ledger(agent, function_name, function_args, function_result)

        # Discover subdirectory context files from tool arguments
        subdir_hints = agent._subdirectory_hints.check_tool_call(function_name, function_args)
        if subdir_hints:
            if _is_multimodal_tool_result(function_result):
                _append_subdir_hint_to_multimodal(function_result, subdir_hints)
            else:
                function_result += subdir_hints

        # Unwrap _multimodal dicts to an OpenAI-style content list
        # (see parallel path for rationale). String results pass through.
        _tool_content = agent._tool_result_content_for_active_model(function_name, function_result)
        messages.append(make_tool_result_message(function_name, _tool_content, tool_call.id))

        # ── Per-tool /steer drain ───────────────────────────────────
        # Drain pending steer BETWEEN individual tool calls so the
        # injection lands as soon as a tool finishes — not after the
        # entire batch.  The model sees it on the next API iteration.
        agent._apply_pending_steer_to_tool_results(messages, 1)

        if not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
            if agent.verbose_logging:
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s")
                print(agent._wrap_verbose("Result: ", function_result))
            else:
                _fr_str = function_result if isinstance(function_result, str) else str(function_result)
                response_preview = _fr_str[:agent.log_prefix_chars] + "..." if len(_fr_str) > agent.log_prefix_chars else _fr_str
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s - {response_preview}")

        if agent._interrupt_requested and i < len(assistant_message.tool_calls):
            remaining = len(assistant_message.tool_calls) - i
            agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {remaining} remaining tool call(s)", force=True)
            for skipped_tc in assistant_message.tool_calls[i:]:
                skipped_name = skipped_tc.function.name
                messages.append(make_tool_result_message(
                    skipped_name,
                    f"[Tool execution skipped — {skipped_name} was not started. User sent a new message]",
                    skipped_tc.id,
                ))
            break

        if agent.tool_delay > 0 and i < len(assistant_message.tool_calls):
            time.sleep(agent.tool_delay)

    # ── Per-turn aggregate budget enforcement ─────────────────────────
    num_tools_seq = len(assistant_message.tool_calls)
    if num_tools_seq > 0:
        enforce_turn_budget(messages[-num_tools_seq:], env=get_active_env(effective_task_id))

    # ── /steer injection ──────────────────────────────────────────────
    # See _execute_tool_calls_parallel for the rationale. Same hook,
    # applied to sequential execution as well.
    if num_tools_seq > 0:
        agent._apply_pending_steer_to_tool_results(messages, num_tools_seq)




__all__ = [
    "execute_tool_calls_concurrent",
    "execute_tool_calls_sequential",
]
