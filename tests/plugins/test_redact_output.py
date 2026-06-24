"""Tests for the redact_output plugin (outgoing secret redaction)."""
from __future__ import annotations

import pytest

from plugins.redact_output import redact, redact_output_hook, register

# Realistic-shaped but FAKE secrets (not real credentials).
_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"          # github token shape
_SK = "sk-" + "abcdEFGH1234abcdEFGH1234"        # openai key shape
_FISH = "fish_" + "0123456789abcdef0123456789abcdef0123"  # sakana key shape
_AWS = "AKIA" + "ABCDEFGHIJ123456"               # aws access key shape
_JWT = "eyJhbGciOiJIUzI1Niained.eyJzdWIiOiIxMjM0NTY.abcDEF123456"


def test_no_op_when_flag_unset(monkeypatch):
    monkeypatch.delenv("HERMES_REDACT_OUTPUT", raising=False)
    assert redact_output_hook(response_text=f"token {_GH}") is None


def test_redacts_github_token(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_OUTPUT", "1")
    out = redact_output_hook(response_text=f"here is the token {_GH} ok")
    assert isinstance(out, str)
    assert _GH not in out
    assert "‹redacted:github-token›" in out


def test_clean_text_returns_none(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_OUTPUT", "1")
    assert redact_output_hook(response_text="just a normal message, nothing secret") is None


def test_redacts_multiple_types(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_OUTPUT", "1")
    text = f"gh={_GH} sk={_SK} fish={_FISH} aws={_AWS}"
    out = redact_output_hook(response_text=text)
    for secret in (_GH, _SK, _FISH, _AWS):
        assert secret not in out


def test_redacts_private_key_block(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_OUTPUT", "1")
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEA_fakekeymaterial_\n"
           "-----END RSA PRIVATE KEY-----")
    out = redact_output_hook(response_text=f"key:\n{pem}\n")
    assert "PRIVATE KEY" not in out
    assert "‹redacted:private-key›" in out


def test_redacts_jwt(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_OUTPUT", "1")
    out = redact_output_hook(response_text=f"auth: {_JWT}")
    assert _JWT not in out
    assert "‹redacted:jwt›" in out


def test_does_not_redact_placeholders(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_OUTPUT", "1")
    # Short/placeholder shapes must pass through untouched.
    safe = "use gho_example or sk-xxxx as placeholders; task-list is fine"
    assert redact_output_hook(response_text=safe) is None


def test_empty_and_nonstring(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_OUTPUT", "1")
    assert redact_output_hook(response_text="") is None
    assert redact_output_hook(response_text=None) is None
    assert redact_output_hook() is None


def test_redact_counts():
    text = f"{_GH} and {_GH}"
    out, n = redact(text)
    assert n == 2
    assert _GH not in out


def test_register_wires_transform_llm_output():
    captured = {}

    class _Ctx:
        def register_hook(self, name, cb):
            captured["name"] = name
            captured["cb"] = cb

    register(_Ctx())
    assert captured["name"] == "transform_llm_output"
    assert captured["cb"] is redact_output_hook
