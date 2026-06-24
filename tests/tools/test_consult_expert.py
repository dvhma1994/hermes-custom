"""Tests for tools/consult_expert_tool.py."""
import json

from tools import consult_expert_tool as cet


class _FakeMessage:
    content = "canned expert answer"


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeResponse()


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


def test_success_returns_answer(monkeypatch):
    from agent import auxiliary_client

    fake_client = _FakeClient()
    calls = []

    def fake_resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return fake_client, "fugu-ultra"

    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", fake_resolve)
    monkeypatch.delenv("HERMES_EXPERT_MODEL", raising=False)  # exercise the default ("fugu")

    out = json.loads(cet.consult_expert("How should I debug this?", context="Stack trace here"))

    assert out == {"success": True, "answer": "canned expert answer", "model": "fugu"}
    assert calls == [(("sakana",), {"model": "fugu"})]
    assert fake_client.chat.completions.kwargs == {
        "model": "fugu",
        "messages": [
            {
                "role": "user",
                "content": "How should I debug this?\n\nContext:\nStack trace here",
            }
        ],
        "max_tokens": 2000,
    }


def test_provider_error_returns_fail_open_json_error(monkeypatch):
    from agent import auxiliary_client

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", boom)

    out = json.loads(cet.consult_expert("Hard question"))

    assert out["success"] is False
    assert "provider down" in out["error"]


def test_check_fn_true_only_when_flag_is_one(monkeypatch):
    monkeypatch.delenv("HERMES_EXPERT_CONSULT", raising=False)
    assert cet.check_consult_expert_requirements() is False

    monkeypatch.setenv("HERMES_EXPERT_CONSULT", "0")
    assert cet.check_consult_expert_requirements() is False

    monkeypatch.setenv("HERMES_EXPERT_CONSULT", "1")
    assert cet.check_consult_expert_requirements() is True


def test_empty_question_errors():
    out = json.loads(cet.consult_expert(""))

    assert out == {"success": False, "error": "question required"}


def test_module_is_discoverable_by_builtin_tool_loader():
    from pathlib import Path

    from tools.registry import _module_registers_tools

    tool_path = Path(cet.__file__)

    assert _module_registers_tools(tool_path) is True


def test_registered_handler_maps_args_and_db(monkeypatch):
    from tools.registry import registry

    captured = {}

    def fake_consult(question, context="", db=None):
        captured.update({"question": question, "context": context, "db": db})
        return json.dumps({"success": True})

    monkeypatch.setattr(cet, "consult_expert", fake_consult)
    entry = registry.get_entry("consult_expert")

    assert entry is not None
    assert json.loads(entry.handler({"question": "Q", "context": "C"}, db="db-obj")) == {"success": True}
    assert captured == {"question": "Q", "context": "C", "db": "db-obj"}
