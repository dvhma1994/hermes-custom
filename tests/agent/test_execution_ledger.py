"""Tests for the durable execution ledger (file-grounded state that survives
context compression).

The ledger closes a concrete long-horizon failure mode: at a context-compression
boundary the transcript is summarized away and ``TodoStore`` drops completed
items, so on a multi-file task the agent re-edits a file it already fixed or
skips verification it already ran. These tests cover the SQLite persistence
layer, the cross-rotation carry-forward (compression rotates ``session_id``), the
injection rendering, and the fail-open capture helper in ``tool_executor``.
"""

import os
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def test_record_and_get_returns_rows(db):
    assert db.record_file_mutation("s1", "agent/foo.py", "write_file") is True
    assert db.record_file_mutation("s1", "tests/test_foo.py", "patch") is True

    rows = db.get_execution_ledger("s1")
    paths = {r["path"] for r in rows}
    assert paths == {"agent/foo.py", "tests/test_foo.py"}
    assert all(r["edit_count"] == 1 for r in rows)


def test_upsert_increments_edit_count_not_duplicate(db):
    db.record_file_mutation("s1", "agent/foo.py", "write_file")
    db.record_file_mutation("s1", "agent/foo.py", "patch")
    db.record_file_mutation("s1", "agent/foo.py", "patch")

    rows = db.get_execution_ledger("s1")
    assert len(rows) == 1, "same (session, path) must upsert, not duplicate"
    assert rows[0]["edit_count"] == 3
    assert rows[0]["action"] == "patch"  # latest action wins


def test_record_rejects_empty_args(db):
    assert db.record_file_mutation("", "agent/foo.py", "write_file") is False
    assert db.record_file_mutation("s1", "", "write_file") is False
    assert db.get_execution_ledger("s1") == []


def test_summary_is_preserved_and_not_clobbered_by_none(db):
    db.record_file_mutation("s1", "agent/foo.py", "write_file", summary="added retry guard")
    # A later edit without a summary must not wipe the existing one (COALESCE).
    db.record_file_mutation("s1", "agent/foo.py", "patch")
    rows = db.get_execution_ledger("s1")
    assert rows[0]["summary"] == "added retry guard"


def test_format_injection_empty_returns_none(db):
    assert db.format_execution_ledger_for_injection("does-not-exist") is None


def test_format_injection_renders_paths_and_counts(db):
    db.record_file_mutation("s1", "agent/foo.py", "write_file")
    db.record_file_mutation("s1", "agent/foo.py", "patch")  # -> 2 edits
    db.record_file_mutation("s1", "tests/test_foo.py", "patch")

    text = db.format_execution_ledger_for_injection("s1")
    assert text is not None
    assert "already modified" in text.lower()
    assert "agent/foo.py" in text
    assert "tests/test_foo.py" in text
    assert "2× edits" in text  # multi-edit marker present
    # single-edit file shows no count marker
    assert "tests/test_foo.py (" not in text


def test_carry_forward_rekeys_old_to_new(db):
    db.record_file_mutation("old", "agent/foo.py", "write_file")
    db.record_file_mutation("old", "agent/bar.py", "patch")

    assert db.carry_forward_execution_ledger("old", "new") is True
    assert db.get_execution_ledger("old") == []
    new_paths = {r["path"] for r in db.get_execution_ledger("new")}
    assert new_paths == {"agent/foo.py", "agent/bar.py"}


def test_carry_forward_noop_on_same_or_empty_id(db):
    db.record_file_mutation("s1", "agent/foo.py", "write_file")
    assert db.carry_forward_execution_ledger("s1", "s1") is False
    assert db.carry_forward_execution_ledger("", "new") is False
    assert db.carry_forward_execution_ledger("s1", "") is False
    # rows untouched
    assert len(db.get_execution_ledger("s1")) == 1


def test_carry_forward_survives_multiple_rotations(db):
    """Simulates several compression cycles: the ledger must accumulate, not reset."""
    db.record_file_mutation("s0", "a.py", "write_file")
    db.carry_forward_execution_ledger("s0", "s1")
    db.record_file_mutation("s1", "b.py", "write_file")
    db.carry_forward_execution_ledger("s1", "s2")
    db.record_file_mutation("s2", "c.py", "write_file")

    paths = {r["path"] for r in db.get_execution_ledger("s2")}
    assert paths == {"a.py", "b.py", "c.py"}


# ── Capture helper (tool_executor) ────────────────────────────────────────────

from agent.tool_executor import _record_execution_ledger


def _agent_with(db, session_id="s1"):
    return SimpleNamespace(_session_db=db, session_id=session_id)


def test_helper_noop_when_flag_unset(db, monkeypatch):
    # The recorder no-ops only when NONE of the recording flags is set; delete all
    # three so this test can't be flipped by env another test leaked into the process.
    for _f in ("HERMES_EXEC_LEDGER", "HERMES_DOD_GATE", "HERMES_REGRESSION_GATE"):
        monkeypatch.delenv(_f, raising=False)
    _record_execution_ledger(_agent_with(db), "write_file", {"path": "x.py"}, "ok")
    assert db.get_execution_ledger("s1") == []


def test_helper_records_write_file_when_enabled(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    # Non-JSON result -> falls back to args; path is normalized before storing.
    _record_execution_ledger(_agent_with(db), "write_file", {"path": "agent/x.py"}, "wrote 10 lines")
    rows = db.get_execution_ledger("s1")
    assert [r["path"] for r in rows] == [os.path.normpath("agent/x.py")]


def test_helper_prefers_resolved_path_from_result(db, monkeypatch):
    """Result-driven (D2): use the tool's resolved absolute path, not the raw arg."""
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    result = '{"bytes_written": 10, "resolved_path": "C:\\\\proj\\\\agent\\\\x.py", "files_modified": ["C:\\\\proj\\\\agent\\\\x.py"]}'
    _record_execution_ledger(_agent_with(db), "write_file", {"path": "agent/x.py"}, result)
    rows = db.get_execution_ledger("s1")
    # resolved_path + files_modified are the same file -> deduped to ONE row.
    assert len(rows) == 1
    assert rows[0]["path"] == os.path.normpath("C:\\proj\\agent\\x.py")


def test_helper_skips_writeresult_error_shape(db, monkeypatch):
    """D1: a failed write_file emits {"bytes_written":0,...,"error":...} with NO
    "success" field and not starting with {"error" — must still be skipped."""
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    result = '{"bytes_written": 0, "resolved_path": "C:\\\\proj\\\\x.py", "error": "permission denied"}'
    _record_execution_ledger(_agent_with(db), "write_file", {"path": "x.py"}, result)
    assert db.get_execution_ledger("s1") == []


def test_helper_dedups_path_spellings(db, monkeypatch):
    """D2: same physical file via different arg spellings -> one row, edit_count grows."""
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    a = _agent_with(db)
    _record_execution_ledger(a, "write_file", {"path": "agent/x.py"}, "ok-text")
    _record_execution_ledger(a, "write_file", {"path": "./agent/x.py"}, "ok-text")
    _record_execution_ledger(a, "write_file", {"path": "agent/sub/../x.py"}, "ok-text")
    rows = db.get_execution_ledger("s1")
    assert len(rows) == 1
    assert rows[0]["edit_count"] == 3


def test_helper_ignores_non_file_tools(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(_agent_with(db), "terminal", {"command": "ls"}, "ok")
    _record_execution_ledger(_agent_with(db), "search_files", {"query": "foo"}, "ok")
    assert db.get_execution_ledger("s1") == []


def test_helper_skips_errored_write(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(_agent_with(db), "patch", {"path": "x.py"}, '{"error": "no such file"}')
    _record_execution_ledger(_agent_with(db), "write_file", {"path": "y.py"}, "Error executing tool 'write_file': boom")
    _record_execution_ledger(_agent_with(db), "write_file", {"path": "z.py"}, '{"success": false, "error": "denied"}')
    assert db.get_execution_ledger("s1") == []


def test_helper_records_patch_replace_mode(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(
        _agent_with(db), "patch",
        {"mode": "replace", "path": "agent/x.py", "old_string": "a", "new_string": "b"},
        "diff applied",
    )
    assert [r["path"] for r in db.get_execution_ledger("s1")] == [os.path.normpath("agent/x.py")]


def test_helper_records_v4a_bulk_patch_multifile(db, monkeypatch):
    """mode='patch' has no `path` arg — files live in the patch body. This is the
    multi-file case the ledger exists for, so every file marker must be captured."""
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    patch_body = (
        "*** Begin Patch\n"
        "*** Update File: agent/a.py\n"
        "@@ ctx @@\n-old\n+new\n"
        "*** Add File: tests/test_a.py\n"
        "+def test_a(): pass\n"
        "*** Delete File: agent/old.py\n"
        "*** End Patch\n"
    )
    _record_execution_ledger(_agent_with(db), "patch", {"mode": "patch", "patch": patch_body}, "ok")
    paths = {r["path"] for r in db.get_execution_ledger("s1")}
    assert paths == {os.path.normpath(p) for p in ("agent/a.py", "tests/test_a.py", "agent/old.py")}


def test_helper_never_raises_on_bad_agent(monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    # No _session_db, weird args — must fail open, not raise.
    _record_execution_ledger(SimpleNamespace(), "write_file", {"path": "x.py"}, "ok")
    _record_execution_ledger(SimpleNamespace(_session_db=None), "write_file", None, "ok")
    _record_execution_ledger(SimpleNamespace(_session_db=None), "patch", "not-a-dict", "ok")


# ── Lineage fallback (D3) · janitor (D4) · dead-shim guard (D5) ────────────────

def test_injection_lineage_fallback_when_carry_forward_missed(db):
    """D3: if rows stayed under an ancestor (carry_forward failed, or the gate was
    enabled mid-task), injection walks sessions.parent_session_id and still finds
    them — instead of silently showing nothing for the rest of the task."""
    db.create_session("root", "cli")
    db.create_session("child", "cli", parent_session_id="root")
    db.record_file_mutation("root", "agent/a.py", "write_file")
    assert db.get_execution_ledger("child") == []  # current id has none of its own
    text = db.format_execution_ledger_for_injection("child")
    assert text is not None and "a.py" in text


def test_injection_returns_none_when_no_ledger_in_lineage(db):
    """The parent walk terminates and returns None when nothing up-chain has rows."""
    db.create_session("root", "cli")
    db.create_session("child", "cli", parent_session_id="root")
    assert db.format_execution_ledger_for_injection("child") is None


def test_prune_removes_old_keeps_fresh(db):
    """D4: age-based janitor drops stale rows, never touches fresh ones."""
    import time
    db.record_file_mutation("s1", "old.py", "write_file")
    db.record_file_mutation("s1", "fresh.py", "write_file")
    old_ts = time.time() - 30 * 86400
    db._conn.execute("UPDATE execution_ledger SET updated_at = ? WHERE path = ?", (old_ts, "old.py"))
    db._conn.commit()
    assert db.prune_execution_ledger(max_age_days=14) == 1
    assert {r["path"] for r in db.get_execution_ledger("s1")} == {"fresh.py"}


def test_dead_rt02_shim_is_not_the_active_compressor():
    """D5: the rt02 shim is dead by design. If someone re-binds it as the active
    compress_context without porting the ledger inject + carry_forward, this fails
    loudly instead of silently dropping file history across compression."""
    from agent import conversation_compression, compression_rt02_shim
    assert (
        conversation_compression.compress_context
        is not compression_rt02_shim.compress_context_rt02
    )


# ── Verification ledger (the second half — what was PROVED) ───────────────────

from agent.tool_executor import _is_verification_command


def test_record_verification_and_get(db):
    assert db.record_verification("s1", "pytest tests/", 0) is True
    assert db.record_verification("s1", "ruff check", 1) is True
    rows = {r["command"]: r for r in db.get_verification_ledger("s1")}
    assert rows["pytest tests/"]["passed"] == 1
    assert rows["ruff check"]["passed"] == 0


def test_record_verification_unknown_exit_code(db):
    db.record_verification("s1", "make build", None)
    row = db.get_verification_ledger("s1")[0]
    assert row["passed"] is None and row["exit_code"] is None


def test_verification_upsert_increments_run_count(db):
    db.record_verification("s1", "pytest", 1)
    db.record_verification("s1", "pytest", 0)  # re-ran, now green
    rows = db.get_verification_ledger("s1")
    assert len(rows) == 1
    assert rows[0]["run_count"] == 2
    assert rows[0]["passed"] == 1  # latest verdict wins


def test_format_verification_empty_none(db):
    assert db.format_verification_for_injection("nope") is None


def test_format_verification_renders_verdicts(db):
    db.record_verification("s1", "pytest tests/", 0)
    db.record_verification("s1", "mypy .", 1)
    text = db.format_verification_for_injection("s1")
    assert "re-run" in text.lower()  # mandates re-verification, never "you're done"
    assert "`pytest tests/` PASS" in text
    assert "`mypy .` FAIL" in text


def test_format_verification_marks_stale_when_file_changed_after(db):
    """Safety: a check that ran BEFORE the latest file change is flagged STALE."""
    db.record_verification("s1", "pytest", 0)
    db.record_file_mutation("s1", "agent/x.py", "write_file")
    db._conn.execute("UPDATE verification_ledger SET updated_at = 1000 WHERE command='pytest'")
    db._conn.execute("UPDATE execution_ledger SET updated_at = 2000 WHERE session_id='s1'")
    db._conn.commit()
    # The per-row marker (not just the header word) must be present.
    assert "STALE (a file changed" in db.format_verification_for_injection("s1")


def test_format_verification_not_stale_when_check_ran_after_change(db):
    db.record_file_mutation("s1", "agent/x.py", "write_file")
    db.record_verification("s1", "pytest", 0)
    db._conn.execute("UPDATE execution_ledger SET updated_at = 1000 WHERE session_id='s1'")
    db._conn.execute("UPDATE verification_ledger SET updated_at = 2000 WHERE command='pytest'")
    db._conn.commit()
    # No per-row STALE marker (the header always mentions the word).
    assert "STALE (a file changed" not in db.format_verification_for_injection("s1")


def test_carry_forward_verification_rekeys(db):
    db.record_verification("old", "pytest", 0)
    assert db.carry_forward_verification_ledger("old", "new") is True
    assert db.get_verification_ledger("old") == []
    assert [r["command"] for r in db.get_verification_ledger("new")] == ["pytest"]


def test_prune_clears_old_verification_rows(db):
    import time
    db.record_verification("s1", "old-check", 0)
    db.record_verification("s1", "fresh-check", 0)
    db._conn.execute("UPDATE verification_ledger SET updated_at = ? WHERE command = ?",
                     (time.time() - 30 * 86400, "old-check"))
    db._conn.commit()
    db.prune_execution_ledger(max_age_days=14)
    assert [r["command"] for r in db.get_verification_ledger("s1")] == ["fresh-check"]


def test_verification_lineage_fallback(db):
    db.create_session("root", "cli")
    db.create_session("child", "cli", parent_session_id="root")
    db.record_verification("root", "pytest", 0)
    text = db.format_verification_for_injection("child")
    assert text is not None and "pytest" in text


def test_verification_staleness_uses_file_session_when_split(db):
    """If file + verification ledgers landed under DIFFERENT ids (one carry_forward
    succeeded, the other failed), staleness still compares against the file
    ledger's real session — no false 'current' that would trust a stale pass."""
    db.create_session("A", "cli")
    db.create_session("B", "cli", parent_session_id="A")
    db.record_verification("A", "pytest", 0)        # verification stayed under A
    db.record_file_mutation("B", "agent/x.py", "write_file")  # files moved to B
    db._conn.execute("UPDATE verification_ledger SET updated_at = 1000 WHERE command='pytest'")
    db._conn.execute("UPDATE execution_ledger SET updated_at = 2000 WHERE session_id='B'")
    db._conn.commit()
    # Querying under B: verification resolves via lineage to A, files under B.
    assert "STALE (a file changed" in db.format_verification_for_injection("B")


def test_is_verification_command_heuristic():
    assert _is_verification_command("python -m pytest tests/ -q")
    assert _is_verification_command("npm run test")
    assert _is_verification_command("ruff check .")
    assert _is_verification_command("go build ./...")
    assert not _is_verification_command("ls -la")
    assert not _is_verification_command("git status")
    assert not _is_verification_command("echo hello")


def test_helper_records_terminal_verification(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(
        _agent_with(db), "terminal",
        {"command": "pytest tests/ -q"},
        '{"output": "5 passed", "exit_code": 0}',
    )
    rows = db.get_verification_ledger("s1")
    assert rows[0]["command"] == "pytest tests/ -q"
    assert rows[0]["passed"] == 1


def test_helper_skips_non_verification_terminal(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(_agent_with(db), "terminal", {"command": "ls -la"}, '{"output":"x","exit_code":0}')
    assert db.get_verification_ledger("s1") == []


def test_helper_terminal_unknown_exit_code_still_records(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    # Non-JSON result -> exit_code unknown, but the command is still recorded.
    _record_execution_ledger(_agent_with(db), "terminal", {"command": "pytest -q"}, "plain text output")
    rows = db.get_verification_ledger("s1")
    assert rows[0]["command"] == "pytest -q" and rows[0]["passed"] is None


# ── Hermes-review fixes: F1 (COALESCE) · F2 (-1 sentinel) · F3 (head match) ────

def test_is_verification_command_rejects_mere_mentions(db=None):
    """F3: a marker that only APPEARS (commit message, filename, grep arg) is not
    a verification run — only the command HEAD counts. Real runs still register."""
    assert not _is_verification_command('git commit -m "fix ruff lint"')
    assert not _is_verification_command("cat pytest.ini")
    assert not _is_verification_command("ls | grep tsc")
    assert not _is_verification_command("cargo build")  # markers are cargo test/check/clippy
    assert _is_verification_command("python -m pytest tests/")
    assert _is_verification_command("npx tsc --noEmit")
    assert _is_verification_command("FOO=bar pytest -q")
    assert _is_verification_command("cd repo && go test ./...")


def test_verification_unknown_rerun_preserves_known_verdict(db):
    """F1: a re-run whose exit code is unknown must NOT wipe a prior known verdict
    to NULL — losing a known PASS/FAIL across compression defeats the ledger."""
    db.record_verification("s1", "pytest", 0)       # known PASS
    db.record_verification("s1", "pytest", None)    # re-ran, exit unknown
    row = db.get_verification_ledger("s1")[0]
    assert row["passed"] == 1 and row["exit_code"] == 0  # preserved, not nulled
    assert row["run_count"] == 2
    db.record_verification("s1", "pytest", 1)        # a real failure still wins
    assert db.get_verification_ledger("s1")[0]["passed"] == 0


def test_helper_terminal_negative_exit_is_unknown_not_fail(db, monkeypatch):
    """F2: terminal exit_code=-1 (didn't run: disabled/pending/blocked) must record
    as unknown, never as a spurious FAIL on a command that never executed."""
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(
        _agent_with(db), "terminal", {"command": "pytest -q"},
        '{"output": "pending approval", "exit_code": -1}',
    )
    row = db.get_verification_ledger("s1")[0]
    assert row["passed"] is None and row["exit_code"] is None


# ── Cycle 1: output snippet (recover WHY a check failed across compression) ────

def test_record_verification_stores_output_snippet(db):
    db.record_verification("s1", "pytest", 1, output_snippet="E   assert 1 == 2")
    assert db.get_verification_ledger("s1")[0]["output_snippet"] == "E   assert 1 == 2"


def test_format_shows_snippet_for_fail_not_pass(db):
    db.record_verification("s1", "pytest fail", 1, output_snippet="Traceback: boom")
    db.record_verification("s1", "pytest ok", 0, output_snippet=None)
    text = db.format_verification_for_injection("s1")
    assert "last error: Traceback: boom" in text
    assert text.count("last error:") == 1  # only the FAIL row carries one


def test_pass_clears_prior_fail_snippet(db):
    db.record_verification("s1", "pytest", 1, output_snippet="old traceback")
    db.record_verification("s1", "pytest", 0, output_snippet=None)  # now green
    row = db.get_verification_ledger("s1")[0]
    assert row["passed"] == 1 and row["output_snippet"] is None  # stale traceback cleared


def test_unknown_rerun_preserves_fail_snippet(db):
    db.record_verification("s1", "pytest", 1, output_snippet="the traceback")
    db.record_verification("s1", "pytest", None, output_snippet=None)  # parse-miss re-run
    row = db.get_verification_ledger("s1")[0]
    assert row["passed"] == 0 and row["output_snippet"] == "the traceback"  # both preserved


def test_helper_captures_snippet_for_failing_check(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(
        _agent_with(db), "terminal", {"command": "pytest -q"},
        '{"output": "E   assert False\\n1 failed", "exit_code": 1}',
    )
    row = db.get_verification_ledger("s1")[0]
    assert row["passed"] == 0 and "assert False" in (row["output_snippet"] or "")


def test_helper_no_snippet_for_passing_check(db, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(
        _agent_with(db), "terminal", {"command": "pytest -q"},
        '{"output": "5 passed", "exit_code": 0}',
    )
    row = db.get_verification_ledger("s1")[0]
    assert row["passed"] == 1 and row["output_snippet"] is None


# ── Cycle-1 review fixes: (d) secret redaction · (c) injection size budget ─────

from agent.tool_executor import _redact_secrets


def test_redact_secrets_unit():
    assert "sk-abcdefghijklmnop12" not in _redact_secrets("token=sk-abcdefghijklmnop12")
    assert "<redacted" in _redact_secrets("password: hunter2hunter2")
    assert "secretvalue123" not in _redact_secrets("Authorization: Bearer secretvalue123")
    assert "ghp_" + "a" * 21 not in _redact_secrets("leaked ghp_" + "a" * 21 + " here")
    assert _redact_secrets("5 passed in 0.3s") == "5 passed in 0.3s"  # non-secret untouched


def test_redact_secrets_env_var_compounds():
    """Cycle 11: inline env-prefixed secrets in a command must be redacted, without
    false-matching benign env vars."""
    assert "hunter2" not in _redact_secrets("PGPASSWORD=hunter2 pytest")
    assert "ghp_" + "a" * 21 not in _redact_secrets("GITHUB_TOKEN=ghp_" + "a" * 21 + " python -m pytest")
    assert "s3cr3t" not in _redact_secrets("DB_PASSWORD=s3cr3t make test")
    # multi-segment names (review fix): the canonical AWS/cloud secrets
    assert "wJalrXUtn" not in _redact_secrets("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI pytest")
    assert "abcd1234" not in _redact_secrets("GOOGLE_API_KEY=abcd1234 python -m pytest")
    assert "xoxb-9" not in _redact_secrets("SLACK_APP_TOKEN=xoxb-9-secret make test")
    # benign env vars / lookalikes are NOT redacted (incl. multi-segment)
    assert _redact_secrets("MONKEY=banana pytest") == "MONKEY=banana pytest"
    assert _redact_secrets("AUTHOR=jane go test") == "AUTHOR=jane go test"
    assert _redact_secrets("LD_LIBRARY_PATH=/x pytest") == "LD_LIBRARY_PATH=/x pytest"
    assert _redact_secrets("MONKEY_BUSINESS=x pytest") == "MONKEY_BUSINESS=x pytest"


def test_verification_command_is_redacted_before_storage(db, monkeypatch):
    """Cycle 11: the command itself (persisted + re-injected) must be redacted, not
    just the output — env-prefixed checks can carry a credential."""
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    _record_execution_ledger(
        _agent_with(db), "terminal",
        {"command": "PGPASSWORD=hunter2 pytest tests/ -q"},
        '{"output": "1 failed", "exit_code": 1}',
    )
    cmd = db.get_verification_ledger("s1")[0]["command"]
    assert "hunter2" not in cmd and "<redacted>" in cmd


def test_output_snippet_redacts_secrets_before_db(db, monkeypatch):
    """(d): credentials in a failing check's output must be redacted before they're
    persisted to state.db (and thus re-injected every future turn)."""
    monkeypatch.setenv("HERMES_EXEC_LEDGER", "1")
    import json as _json
    out = "FAILED test_env API_KEY=sk-abc123def456ghi789xyz and Authorization: Bearer ghp_" + "a" * 25
    _record_execution_ledger(
        _agent_with(db), "terminal", {"command": "pytest -q"},
        _json.dumps({"output": out, "exit_code": 1}),
    )
    snip = db.get_verification_ledger("s1")[0]["output_snippet"]
    assert "sk-abc123def456ghi789xyz" not in snip
    assert "ghp_" + "a" * 25 not in snip
    assert "<redacted" in snip


def test_injection_snippet_budget_caps_total(db):
    """(c): a many-FAIL session can't bloat the injected error-tail block unboundedly."""
    big = "x" * 400
    for i in range(40):
        db.record_verification("s1", f"pytest test_{i}", 1, output_snippet=big)
    text = db.format_verification_for_injection("s1")
    assert text.count("` FAIL") == 40  # every FAIL still listed
    error_chars = sum(
        len(line) for line in text.splitlines() if line.strip().startswith("last error:")
    )
    assert error_chars <= 5000  # budget-capped (~4000 + one overshoot line)


# ── Cycle 3: Definition-of-Done gate query (get_open_verification_failures) ────

def test_open_failures_empty_when_all_green_and_current(db):
    db.record_verification("s1", "pytest", 0)  # exit 0 -> pass, no file changes after
    assert db.get_open_verification_failures("s1") == []


def test_open_failures_includes_fail(db):
    db.record_verification("s1", "pytest", 1)  # FAIL
    db.record_verification("s1", "ruff", 0)    # pass, current
    open_ = db.get_open_verification_failures("s1")
    assert [o["command"] for o in open_] == ["pytest"]
    assert open_[0]["passed"] == 0 and open_[0]["stale"] is False


def test_open_failures_includes_stale_pass(db):
    db.record_verification("s1", "pytest", 0)  # passed...
    db.record_file_mutation("s1", "agent/x.py", "write_file")  # ...then a file changed
    db._conn.execute("UPDATE verification_ledger SET updated_at=1000 WHERE command='pytest'")
    db._conn.execute("UPDATE execution_ledger SET updated_at=2000 WHERE session_id='s1'")
    db._conn.commit()
    open_ = db.get_open_verification_failures("s1")
    assert len(open_) == 1 and open_[0]["stale"] is True and open_[0]["passed"] == 1


def test_open_failures_via_lineage(db):
    db.create_session("root", "cli")
    db.create_session("child", "cli", parent_session_id="root")
    db.record_verification("root", "pytest", 1)
    assert [o["command"] for o in db.get_open_verification_failures("child")] == ["pytest"]


def test_open_failures_excludes_abandoned_old_check(db):
    """Review Issue 2: a check that failed early and was abandoned (never re-run)
    must not block every later, unrelated final in a long session."""
    db.record_verification("s1", "old-pytest", 1)    # failed early
    db.record_verification("s1", "recent-ruff", 0)   # later, passing
    db._conn.execute("UPDATE verification_ledger SET updated_at=1000 WHERE command='old-pytest'")
    db._conn.execute("UPDATE verification_ledger SET updated_at=100000 WHERE command='recent-ruff'")
    db._conn.commit()
    # old-pytest is ~99000s before the latest activity -> outside the recency window
    assert db.get_open_verification_failures("s1") == []


# ── Cycle 9: regression-test coverage gate ────────────────────────────────────

from hermes_state import _is_test_path, _is_source_code


def test_is_test_path_classification():
    assert _is_test_path("tests/test_foo.py")
    assert _is_test_path("lib/foo_test.go")
    assert _is_test_path("src/foo.spec.ts")
    assert _is_test_path("conftest.py")
    assert _is_test_path("pkg/__tests__/x.js")
    assert not _is_test_path("agent/foo.py")
    assert not _is_test_path("README.md")


def test_is_source_code_classification():
    assert _is_source_code("agent/foo.py")
    assert _is_source_code("src/app.ts")
    assert not _is_source_code("tests/test_foo.py")  # a test, not source-to-cover
    assert not _is_source_code("README.md")
    assert not _is_source_code("config.yaml")


def test_coverage_gap_present_when_source_changed_no_test_with_runner(db):
    db.record_file_mutation("s1", "agent/foo.py", "write_file")  # source change
    db.record_verification("s1", "pytest", 0)                    # a runner is in use
    assert db.get_open_test_coverage_gap("s1") == ["agent/foo.py"]


def test_no_gap_when_a_test_was_also_changed(db):
    db.record_file_mutation("s1", "agent/foo.py", "write_file")
    db.record_file_mutation("s1", "tests/test_foo.py", "write_file")
    db.record_verification("s1", "pytest", 0)
    assert db.get_open_test_coverage_gap("s1") == []


def test_no_gap_when_no_runner_in_use(db):
    db.record_file_mutation("s1", "agent/foo.py", "write_file")  # no verification recorded
    assert db.get_open_test_coverage_gap("s1") == []


def test_no_gap_for_docs_or_config_only(db):
    db.record_file_mutation("s1", "README.md", "write_file")
    db.record_file_mutation("s1", "config.yaml", "patch")
    db.record_verification("s1", "pytest", 0)
    assert db.get_open_test_coverage_gap("s1") == []


def test_no_gap_when_runner_is_stale(db):
    """Review Issue 2: a verification carried from an EARLIER task must not satisfy
    the runner guard for the current task's edits."""
    db.record_verification("s1", "pytest", 0)
    db.record_file_mutation("s1", "agent/foo.py", "write_file")
    db._conn.execute("UPDATE verification_ledger SET updated_at = 1000 WHERE command='pytest'")
    db._conn.execute("UPDATE execution_ledger SET updated_at = 100000 WHERE session_id='s1'")
    db._conn.commit()
    assert db.get_open_test_coverage_gap("s1") == []  # stale runner -> not this task


def test_recording_fires_under_regression_gate_flag_alone(db, monkeypatch):
    """Review Issue 1: the ledger must record (so the regression gate has data) when
    ONLY HERMES_REGRESSION_GATE is set — not silently depend on HERMES_EXEC_LEDGER."""
    monkeypatch.delenv("HERMES_EXEC_LEDGER", raising=False)
    monkeypatch.delenv("HERMES_DOD_GATE", raising=False)
    monkeypatch.setenv("HERMES_REGRESSION_GATE", "1")
    _record_execution_ledger(_agent_with(db), "write_file", {"path": "agent/foo.py"}, "wrote 10 lines")
    assert [r["path"] for r in db.get_execution_ledger("s1")] == [os.path.normpath("agent/foo.py")]


def test_helper_records_under_dod_gate_flag_alone(db, monkeypatch):
    """Review Issue 1: verification recording must work when ONLY HERMES_DOD_GATE
    is set (the gate consumes it), not silently depend on HERMES_EXEC_LEDGER."""
    monkeypatch.delenv("HERMES_EXEC_LEDGER", raising=False)
    monkeypatch.setenv("HERMES_DOD_GATE", "1")
    _record_execution_ledger(
        _agent_with(db), "terminal", {"command": "pytest -q"},
        '{"output": "1 failed", "exit_code": 1}',
    )
    assert [o["command"] for o in db.get_open_verification_failures("s1")] == ["pytest -q"]
