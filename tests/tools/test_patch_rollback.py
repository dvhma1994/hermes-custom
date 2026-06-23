"""Tests for V4A multi-file apply-phase rollback (HERMES_V4A_ROLLBACK).

A partial multi-file apply (file A written, file B then fails) must restore A
rather than leave a half-applied tree.
"""

from types import SimpleNamespace

from tools.patch_parser import (
    OperationType,
    PatchOperation,
    _capture_v4a_snapshots,
    _rollback_v4a,
    apply_v4a_operations,
    parse_v4a_patch,
)


class FakeFS:
    def __init__(self, files, fail_write_on=None):
        self.files = dict(files)
        self.fail_write_on = fail_write_on

    def read_file_raw(self, path):
        if path in self.files:
            return SimpleNamespace(content=self.files[path], error=None)
        return SimpleNamespace(content=None, error="not found")

    def write_file(self, path, content):
        if path == self.fail_write_on:
            return SimpleNamespace(error="EACCES: permission denied")
        self.files[path] = content
        return SimpleNamespace(error=None)

    def delete_file(self, path):
        self.files.pop(path, None)
        return SimpleNamespace(error=None)

    def move_file(self, src, dst):
        if src in self.files:
            self.files[dst] = self.files.pop(src)
        return SimpleNamespace(error=None)


def test_rollback_restores_update_add_delete_move():
    fs = FakeFS({"a.py": "orig_a", "d.py": "orig_d", "m.py": "orig_m"})
    ops = [
        PatchOperation(OperationType.UPDATE, "a.py"),
        PatchOperation(OperationType.ADD, "new.py"),
        PatchOperation(OperationType.DELETE, "d.py"),
        PatchOperation(OperationType.MOVE, "m.py", new_path="m2.py"),
    ]
    snaps = _capture_v4a_snapshots(ops, fs)  # BEFORE any write
    # simulate a successful apply of all four
    fs.write_file("a.py", "new_a")
    fs.write_file("new.py", "added")
    fs.delete_file("d.py")
    fs.move_file("m.py", "m2.py")

    ok, detail = _rollback_v4a([0, 1, 2, 3], snaps, fs)
    assert ok, detail
    assert fs.files["a.py"] == "orig_a"            # update reverted
    assert "new.py" not in fs.files                 # add removed
    assert fs.files["d.py"] == "orig_d"            # delete restored
    assert fs.files.get("m.py") == "orig_m" and "m2.py" not in fs.files  # move reversed


def test_rollback_reports_failure_when_restore_blocked():
    # write_file fails for the file we'd need to restore -> not a clean rollback
    fs = FakeFS({"a.py": "orig_a"}, fail_write_on="a.py")
    ops = [PatchOperation(OperationType.UPDATE, "a.py")]
    snaps = _capture_v4a_snapshots(ops, fs)
    ok, detail = _rollback_v4a([0], snaps, fs)
    assert ok is False and "a.py" in detail


_TWO_FILE_PATCH = """\
*** Begin Patch
*** Update File: a.py
@@ x @@
 x
-old
+new
 y
*** Update File: b.py
@@ x @@
 x
-old
+new
 y
*** End Patch"""


def _two_file_fs():
    return FakeFS({"a.py": "x\nold\ny", "b.py": "x\nold\ny"}, fail_write_on="b.py")


def test_partial_apply_rolls_back_when_flag_on(monkeypatch):
    monkeypatch.setenv("HERMES_V4A_ROLLBACK", "1")
    ops, err = parse_v4a_patch(_TWO_FILE_PATCH)
    assert err is None and len(ops) == 2
    fs = _two_file_fs()
    result = apply_v4a_operations(ops, fs)
    assert result.success is False
    assert "rolled back" in (result.error or "").lower()
    assert fs.files["a.py"] == "x\nold\ny"  # A restored despite B's write failing


def test_partial_apply_leaves_half_state_when_flag_off(monkeypatch):
    monkeypatch.delenv("HERMES_V4A_ROLLBACK", raising=False)
    ops, err = parse_v4a_patch(_TWO_FILE_PATCH)
    assert err is None
    fs = _two_file_fs()
    result = apply_v4a_operations(ops, fs)
    assert result.success is False
    assert fs.files["a.py"] == "x\nnew\ny"  # A stays modified (original behavior)


_DUP_PATH_PATCH = """\
*** Begin Patch
*** Update File: a.py
@@ x @@
 x
-old
+new1
 y
*** Update File: a.py
@@ x @@
 x
-new1
+new2
 y
*** End Patch"""


def test_duplicate_target_path_rejected_in_validation():
    """Review Issue 1: two ops on the same path are rejected up front (Phase 1, no
    writes) — the index-keyed rollback snapshot is unsound for same-path multi-ops."""
    ops, err = parse_v4a_patch(_DUP_PATH_PATCH)
    assert err is None and len(ops) == 2
    fs = FakeFS({"a.py": "x\nold\ny"})
    result = apply_v4a_operations(ops, fs)
    assert result.success is False
    assert "more than one operation" in (result.error or "")
    assert fs.files["a.py"] == "x\nold\ny"  # unchanged — rejected before any write


def test_rollback_reports_failure_when_delete_of_created_fails():
    """Review Issue 3: a failed delete during ADD-rollback must NOT report success."""
    class FS(FakeFS):
        def delete_file(self, path):
            return SimpleNamespace(error="EBUSY")

    fs = FS({})
    ops = [PatchOperation(OperationType.ADD, "new.py")]
    snaps = _capture_v4a_snapshots(ops, fs)
    fs.files["new.py"] = "added"  # simulate the ADD landed
    ok, detail = _rollback_v4a([0], snaps, fs)
    assert ok is False and "new.py" in detail
