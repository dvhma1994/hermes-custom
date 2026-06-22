"""Tests for environment-setup failure diagnosis (HERMES_ENV_DIAG).

_detect_env_setup_failure distinguishes a SETUP failure (missing dep, binary not
on PATH) from a code bug, so the model provisions instead of "fixing" good code.
"""

from tools.terminal_tool import _detect_env_setup_failure


def test_python_missing_module_names_both_dep_and_local_import():
    h = _detect_env_setup_failure("pytest", "ModuleNotFoundError: No module named 'requests'", 1)
    assert h and "pip install" in h.lower()
    assert "import" in h.lower()  # review Issue 1: don't steer away from a local-import bug


def test_pip_resolver_and_externally_managed():
    assert _detect_env_setup_failure("pip install x", "ERROR: Could not find a version that satisfies requirement x", 1)
    assert _detect_env_setup_failure("pip install x", "error: externally-managed-environment", 1)


def test_rust_and_interpreter_missing():
    assert _detect_env_setup_failure("cargo build", "error: could not find `Cargo.toml`", 101)
    h = _detect_env_setup_failure("./run.sh", "/usr/bin/env: 'python': No such file or directory", 127)
    assert h and "path" in h.lower()


def test_sh_form_not_found():
    h = _detect_env_setup_failure("ruff", "sh: 1: ruff: not found", 127)
    assert h and "path" in h.lower()


def test_node_cannot_find_module():
    h = _detect_env_setup_failure("node app.js", "Error: Cannot find module 'express'", 1)
    assert h and "npm install" in h.lower()


def test_npm_enoent_package_json():
    h = _detect_env_setup_failure("npm test", "npm ERR! enoent ENOENT: no such file, open '.../package.json'", 1)
    assert h and "npm install" in h.lower()


def test_go_missing_package():
    h = _detect_env_setup_failure("go build ./...", 'build x: cannot find package "y"', 1)
    assert h and "go mod" in h.lower()


def test_command_not_found():
    h = _detect_env_setup_failure("ruff check", "bash: ruff: command not found", 127)
    assert h and "path" in h.lower()


def test_windows_not_recognized():
    h = _detect_env_setup_failure("ruff", "'ruff' is not recognized as an internal or external command", 1)
    assert h and "path" in h.lower()


def test_real_code_errors_not_flagged():
    assert _detect_env_setup_failure("pytest", "AssertionError: assert 1 == 2\nFAILED tests/test_x.py", 1) is None
    assert _detect_env_setup_failure("python x.py", "SyntaxError: invalid syntax", 1) is None
    assert _detect_env_setup_failure("go test", "./main.go:5: undefined: Foo", 1) is None


def test_not_found_in_prose_is_not_flagged():
    """Review Issue 2: ': not found' in app output/data must NOT read as PATH error."""
    assert _detect_env_setup_failure("pytest", "KeyError: not found\nFAILED tests/test_x.py", 1) is None
    assert _detect_env_setup_failure("curl ...", "HTTP 404: not found", 1) is None
    assert _detect_env_setup_failure("app", "user lookup -> user: not found in db", 1) is None


def test_exit_zero_and_empty_are_none():
    assert _detect_env_setup_failure("pytest", "No module named foo", 0) is None  # success -> not a failure
    assert _detect_env_setup_failure("pytest", "", 1) is None
