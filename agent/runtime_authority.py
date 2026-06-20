"""Runtime Authority for Learning Governance V1."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, fields
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from agent.learning_constants import (
    ALLOWED_AUTHORITY_KNOBS,
    AUTHORITY_POLICY_NO_SELF_DELEGATE,
    AUTHORITY_POLICY_PERMISSIVE,
    AUTHORITY_POLICY_STRICT,
    DEFAULT_MAX_TOOL_ITERATIONS,
    FORBIDDEN_AUTHORITY_KNOBS,
)

__all__ = [
    "AuthorityDecision",
    "RuntimeAuthority",
    "apply_decision",
    "allowed_authority_knobs",
    "ensure_runtime_authority",
    "hash_decision",
]

logger = logging.getLogger(__name__)


_KNOB_TO_AGENT_ATTR = {
    "policy": "_authority_policy",
    "tool_scope": "_authority_tool_scope",
    "context_size_override": "_authority_context_size_override",
    "allow_self_delegate": "_authority_delegation_policy",
    "max_tool_iterations": "_authority_max_tool_iterations",
}


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """Immutable runtime-authority decision. Only allowed knobs may be set."""

    policy: Optional[str] = None
    tool_scope: Optional[Tuple[str, ...]] = None
    context_size_override: Optional[int] = None
    allow_self_delegate: Optional[bool] = None
    max_tool_iterations: Optional[int] = None

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None:
                continue
            if field.name not in ALLOWED_AUTHORITY_KNOBS:
                raise ValueError(f"Forbidden authority knob: {field.name}")
            if field.name == "policy" and value not in {
                AUTHORITY_POLICY_STRICT,
                AUTHORITY_POLICY_PERMISSIVE,
                AUTHORITY_POLICY_NO_SELF_DELEGATE,
            }:
                raise ValueError(f"Invalid authority policy: {value}")
            if field.name == "tool_scope" and not isinstance(value, (list, tuple, set, frozenset)):
                raise ValueError("tool_scope must be a sequence or set")
            if field.name == "context_size_override" and (not isinstance(value, int) or value < 1):
                raise ValueError("context_size_override must be a positive int")
            if field.name == "max_tool_iterations" and (not isinstance(value, int) or value < 1):
                raise ValueError("max_tool_iterations must be a positive int")


@dataclass
class RuntimeAuthority:
    """Per-session runtime authority state with allowed knobs only."""

    policy: str = AUTHORITY_POLICY_PERMISSIVE
    tool_scope: Optional[frozenset] = None
    context_size_override: Optional[int] = None
    allow_self_delegate: bool = True
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS

    def set_policy(self, value: str) -> None:
        if value not in {
            AUTHORITY_POLICY_STRICT,
            AUTHORITY_POLICY_PERMISSIVE,
            AUTHORITY_POLICY_NO_SELF_DELEGATE,
        }:
            raise ValueError(f"Invalid authority policy: {value}")
        self.policy = value

    def set_tool_scope(self, value: Iterable[str]) -> None:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("tool_scope must be a sequence or set")
        self.tool_scope = frozenset(value)

    def set_context_size_override(self, value: int) -> None:
        if not isinstance(value, int) or value < 1:
            raise ValueError("context_size_override must be a positive int")
        self.context_size_override = value

    def set_allow_self_delegate(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise ValueError("allow_self_delegate must be a bool")
        self.allow_self_delegate = value

    def set_max_tool_iterations(self, value: int) -> None:
        if not isinstance(value, int) or value < 1:
            raise ValueError("max_tool_iterations must be a positive int")
        self.max_tool_iterations = value

    def to_decision(self) -> AuthorityDecision:
        return AuthorityDecision(
            policy=self.policy,
            tool_scope=tuple(self.tool_scope) if self.tool_scope else None,
            context_size_override=self.context_size_override,
            allow_self_delegate=self.allow_self_delegate,
            max_tool_iterations=self.max_tool_iterations,
        )

    def get_policy(self) -> str:
        return self.policy

    def get_context_size_override(self) -> Optional[int]:
        return self.context_size_override

    def get_max_tool_iterations(self) -> int:
        return self.max_tool_iterations

    def get_allow_self_delegate(self) -> bool:
        return self.allow_self_delegate

    def get_tool_scope(self) -> Optional[frozenset]:
        return self.tool_scope


def allowed_authority_knobs() -> Tuple[str, ...]:
    return tuple(ALLOWED_AUTHORITY_KNOBS)


def _check_forbidden(knob: str) -> None:
    if knob in FORBIDDEN_AUTHORITY_KNOBS:
        raise ValueError(f"Forbidden authority knob cannot be applied: {knob}")


def apply_decision(agent: Any, decision: AuthorityDecision) -> None:
    """Apply only allowed authority knobs to the agent."""
    if not isinstance(decision, AuthorityDecision):
        raise ValueError("decision must be an AuthorityDecision")

    for field_obj in fields(decision):
        knob = field_obj.name
        if knob.startswith("_"):
            continue
        _check_forbidden(knob)
        if knob not in ALLOWED_AUTHORITY_KNOBS:
            raise ValueError(f"Authority decision contains unknown knob: {knob}")

    # Policy
    value = decision.policy
    if value is not None:
        agent._authority_policy = value
        if value == AUTHORITY_POLICY_NO_SELF_DELEGATE:
            agent._authority_delegation_policy = AUTHORITY_POLICY_NO_SELF_DELEGATE
        else:
            agent._authority_delegation_policy = (
                AUTHORITY_POLICY_PERMISSIVE if value != AUTHORITY_POLICY_STRICT else AUTHORITY_POLICY_STRICT
            )

    # Tool scope
    value = decision.tool_scope
    if value is not None:
        agent._authority_tool_scope = frozenset(value)

    # Context size override
    value = decision.context_size_override
    if value is not None:
        agent._authority_context_size_override = value

    # Self-delegation
    value = decision.allow_self_delegate
    if value is not None:
        agent._authority_delegation_policy = (
            AUTHORITY_POLICY_NO_SELF_DELEGATE if not value else AUTHORITY_POLICY_PERMISSIVE
        )
        agent._authority_allow_self_delegate = value

    # Max tool iterations
    value = decision.max_tool_iterations
    if value is not None:
        agent._authority_max_tool_iterations = value


def ensure_runtime_authority(agent: Any) -> "RuntimeAuthority":
    """Initialize per-turn runtime-authority state on the agent.

    Milestone 1 semantics: authority is *per-turn* reactive control, not a
    persisted setting. Each turn starts from permissive defaults; the feedback
    collector (see :mod:`agent.runtime_feedback`) may tighten the delegation
    policy for the remainder of the turn if tools fail heavily, then the next
    turn starts clean again.

    Cache safety (sacred Hermes invariant): this deliberately does NOT touch
    the cache-affecting knobs — ``_authority_tool_scope`` (would filter the
    cached tool list), ``_authority_context_size_override``,
    ``_authority_tool_guidance`` / ``_authority_system_prompt_suffix`` (would
    mutate the byte-stable system prompt). Leaving them unset keeps the system
    prompt and tool list byte-stable so upstream prompt caches stay warm. M1
    adaptation never sets any of them.

    Idempotent and exception-safe by construction: it only assigns attributes.
    """
    authority = RuntimeAuthority()
    agent._runtime_authority = authority
    # Reset the knobs M1 may adjust back to the *session baseline* so a
    # within-turn tightening cannot leak into the next turn — but a
    # session-level learned policy (set once at session start by
    # ``learning_cycle.apply_learned_authority`` via the ``*_baseline`` attrs)
    # IS preserved across turns. With no baseline set, this is the permissive
    # default, identical to the pre-learning behavior.
    agent._authority_policy = getattr(
        agent, "_authority_policy_baseline", AUTHORITY_POLICY_PERMISSIVE
    )
    agent._authority_delegation_policy = getattr(
        agent, "_authority_delegation_baseline", AUTHORITY_POLICY_PERMISSIVE
    )
    agent._authority_allow_self_delegate = getattr(
        agent, "_authority_allow_self_delegate_baseline", True
    )
    return authority


def hash_decision(decision: AuthorityDecision) -> str:
    """Return a deterministic SHA-256 hash of a decision."""
    payload = repr(decision).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def filter_tool_scope(tools_for_api: list, scope: frozenset) -> list:
    """Return only tool definitions whose names are within the authority scope."""
    if not scope:
        return list(tools_for_api)

    def _tool_name(tool: Any) -> Optional[str]:
        if isinstance(tool, dict):
            return (
                tool.get("function", {}).get("name")
                or tool.get("name")
            )
        fn = getattr(tool, "function", None)
        if fn is not None:
            return getattr(fn, "name", None)
        return getattr(tool, "name", None)

    return [tool for tool in tools_for_api if _tool_name(tool) in scope]
