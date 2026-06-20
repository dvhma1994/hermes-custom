"""Unit tests for agent/learning_constants.py."""
import pytest

from agent import learning_constants as lc


def test_immutability_check():
    assert isinstance(lc.PROMOTION_CONSTANTS, tuple)
    assert len(lc.PROMOTION_CONSTANTS) >= 16


def test_constants_are_immutable_strings_or_tuples():
    """Critical promotion constants must be strings/tuples or numeric scalars."""
    for name in lc.PROMOTION_CONSTANTS:
        value = getattr(lc, name)
        assert isinstance(value, (str, tuple, int, float, bool)), f"{name} must be immutable scalar; got {type(value)}"


def test_opval_contract_version():
    assert isinstance(lc.OPVAL_EVIDENCE_CONTRACT_VERSION, str)
    assert lc.OPVAL_EVIDENCE_CONTRACT_VERSION == "1.0.0"


def test_promotion_constants():
    assert lc.LEARNING_PROMOTION_THRESHOLD_SCORE >= 0.80
    assert 0 <= lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT <= 1
    assert 0 <= lc.LEARNING_PROMOTION_MAX_MISALIGNMENT_PERCENT <= 1
    assert lc.LEARNING_PROMOTION_MIN_REAL_SESSIONS > 0
    assert lc.LEARNING_PROMOTION_MIN_DOMAIN_COVERAGE > 0


def test_v3_promotion_retirement_constants():
    assert 0 <= lc.PROMOTION_WIN_RATE <= 1
    assert lc.PROMOTION_SAMPLE_COUNT > 0
    assert lc.PROMOTION_AVG_SCORE >= 0
    assert lc.PROMOTION_AVG_ALIGNMENT >= 0
    assert 0 <= lc.RETIREMENT_WIN_RATE <= 1
    assert lc.RETIREMENT_SAMPLE_COUNT > 0


def test_feedback_constants():
    assert lc.FEEDBACK_OUTCOME_SUCCESS == "success"
    assert lc.FEEDBACK_OUTCOME_FAILURE == "failure"
    assert lc.FEEDBACK_OUTCOME_DEFERRED == "deferred"
    assert lc.MAX_FEEDBACK_EVENTS_PER_TURN >= 1


def test_authority_policy_values():
    assert lc.AUTHORITY_POLICY_STRICT == "strict"
    assert lc.AUTHORITY_POLICY_PERMISSIVE == "permissive"
    assert lc.AUTHORITY_POLICY_NO_SELF_DELEGATE == "no_self_delegate"


def test_no_schema_version_conflicts():
    parts = lc.OPVAL_EVIDENCE_CONTRACT_VERSION.split(".")
    assert parts[0] == "1"


def test_all_expected_names_exported():
    for name in lc.__all__:
        assert hasattr(lc, name), f"{name} missing from module"


def test_owner_constant():
    assert lc.OWNER_LEARNING_GOVERNANCE == "learning-governance-v1"
