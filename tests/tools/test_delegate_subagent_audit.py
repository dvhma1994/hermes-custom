"""Tests for the subagent file-write claim detector (HERMES_SUBAGENT_AUDIT).

The detector flags a subagent summary that CLAIMS a file change while the harness
recorded no write for it — a likely fabricated success. It must require BOTH a
write-verb AND a path-ish token so analysis/conclusion summaries don't trip it.
"""

from tools.delegate_tool import _summary_claims_file_change


def test_claims_with_verb_and_path():
    assert _summary_claims_file_change("Implemented the fix in agent/auth.py")
    assert _summary_claims_file_change("Created tests/test_foo.py with 5 cases")
    assert _summary_claims_file_change("wrote the handler to /opt/app/handler.go")
    assert _summary_claims_file_change("Modified config.yaml and added a key")
    assert _summary_claims_file_change("updated the README.md with usage notes")


def test_no_claim_without_path():
    assert not _summary_claims_file_change("I analyzed the code and it looks correct")
    assert not _summary_claims_file_change("All tests pass")
    assert not _summary_claims_file_change("Implemented the algorithm in my head")


def test_no_claim_without_write_verb():
    assert not _summary_claims_file_change("The file agent/auth.py contains the bug")
    assert not _summary_claims_file_change("See config.yaml for details")


def test_no_claim_on_version_or_number_tokens():
    """Review Issue 3: version/number tokens must not read as filenames."""
    assert not _summary_claims_file_change("Refactored the approach; ratio improved to 3.5x")
    assert not _summary_claims_file_change("updated to v2.0 of the protocol")
    assert not _summary_claims_file_change("Implemented Python3.11 pattern matching")


def test_empty_or_none():
    assert not _summary_claims_file_change("")
    assert not _summary_claims_file_change(None)
