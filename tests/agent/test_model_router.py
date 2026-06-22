"""Tests for agent/model_router.py — flag-gated auto-population of the fallback chain.

No network: discovery is tested with an injected fetcher; the populate path is tested
via an explicit configured pool. The contract under test: it ONLY fills an EMPTY chain
when enabled, respects an explicit chain, and is fail-open everywhere.
"""
import json

import pytest

from agent import model_router as mr


class FakeAgent:
    def __init__(self, provider="ollama", model="glm-5.2:cloud",
                 base_url="https://ollama.com/v1", chain=None):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self._fallback_chain = chain if chain is not None else []
        self._fallback_index = 0
        self._fallback_activated = False
        self._fallback_model = None


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL_ROUTER", "1")
    monkeypatch.delenv("HERMES_ROUTER_MODELS", raising=False)


# ------------------------------------------------------------------ pure helpers
class TestRecommendChain:
    def test_excludes_primary_and_dedups(self):
        primary = {"provider": "ollama", "model": "glm-5.2:cloud", "base_url": "x"}
        pool = [
            {"provider": "ollama", "model": "glm-5.2:cloud", "base_url": "x"},  # == primary
            {"provider": "ollama", "model": "kimi-k2.7-code:cloud", "base_url": "x"},
            {"provider": "ollama", "model": "kimi-k2.7-code:cloud", "base_url": "x"},  # dup
            {"provider": "ollama", "model": "minimax-m3:cloud", "base_url": "x"},
        ]
        chain = mr.recommend_fallback_chain(primary, pool)
        models = [c["model"] for c in chain]
        assert "glm-5.2:cloud" not in models
        assert models == ["kimi-k2.7-code:cloud", "minimax-m3:cloud"]

    def test_caps_length(self):
        primary = {"provider": "p", "model": "primary", "base_url": ""}
        pool = [{"provider": "p", "model": f"m{i}", "base_url": ""} for i in range(10)]
        assert len(mr.recommend_fallback_chain(primary, pool)) == mr._MAX_CHAIN

    def test_ordering_prefers_code_then_known_families(self):
        names = ["random-model", "minimax-m3:cloud", "kimi-k2.7-code:cloud"]
        ordered = mr._order_for_fallback(names)
        assert ordered[0] == "kimi-k2.7-code:cloud"        # code tier first
        assert ordered[-1] == "random-model"               # unknown last

    def test_excludes_primary_base_under_different_tag(self):
        # primary is glm-5.2:cloud; bare glm-5.2 is the SAME model → must be dropped,
        # but glm-5.1 (different version) is a legit fallback.
        primary = {"provider": "ollama", "model": "glm-5.2:cloud", "base_url": "u"}
        pool = [
            {"provider": "ollama", "model": "glm-5.2", "base_url": "u"},        # same base → drop
            {"provider": "ollama", "model": "glm-5.1", "base_url": "u"},        # keep
            {"provider": "ollama", "model": "kimi-k2.7-code", "base_url": "u"},  # keep
        ]
        models = [c["model"] for c in mr.recommend_fallback_chain(primary, pool)]
        assert "glm-5.2" not in models
        assert models == ["glm-5.1", "kimi-k2.7-code"]


class TestConfiguredPool:
    def test_parses_valid(self, monkeypatch):
        monkeypatch.setenv("HERMES_ROUTER_MODELS", json.dumps([
            {"provider": "ollama", "model": "a", "base_url": "u"},
            {"provider": "ollama", "model": "b"},
            {"bad": "entry"},                                # dropped (no provider/model)
        ]))
        pool = mr.parse_configured_pool()
        assert [p["model"] for p in pool] == ["a", "b"]

    def test_invalid_json_is_empty(self, monkeypatch):
        monkeypatch.setenv("HERMES_ROUTER_MODELS", "{not json")
        assert mr.parse_configured_pool() == []

    def test_unset_is_empty(self):
        assert mr.parse_configured_pool() == []


class TestDiscovery:
    def _tags(self):
        return {"models": [
            {"name": "glm-5.2:cloud"}, {"name": "kimi-k2.7-code:cloud"},
            {"name": "minimax-m3:cloud"}, {"name": "nomic-embed-text:latest"},  # embed → filtered
        ]}

    def test_discovers_chat_models_filters_embed(self):
        names = mr.discover_models("ollama", "http://localhost:11500",
                                   _fetch=lambda url: self._tags())
        assert "nomic-embed-text:latest" not in names
        assert set(names) == {"glm-5.2:cloud", "kimi-k2.7-code:cloud", "minimax-m3:cloud"}

    def test_non_ollama_provider_returns_empty(self):
        assert mr.discover_models("openai", "https://api.openai.com/v1",
                                  _fetch=lambda url: self._tags()) == []

    def test_fetch_failure_is_empty(self):
        assert mr.discover_models("ollama", "http://localhost:11500",
                                  _fetch=lambda url: None) == []

    def test_build_pool_from_names_uses_primary_route(self):
        pool = mr.build_pool_from_names("ollama", ["kimi-code:cloud", "glm-5.2:cloud"],
                                        "https://ollama.com/v1")
        assert all(p["provider"] == "ollama" and p["base_url"] == "https://ollama.com/v1" for p in pool)
        assert pool[0]["model"] == "kimi-code:cloud"        # code-first ordering


class TestMaybePopulate:
    def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.delenv("HERMES_MODEL_ROUTER", raising=False)
        ag = FakeAgent()
        assert mr.maybe_populate_fallback_chain(ag) == 0
        assert ag._fallback_chain == []

    def test_respects_existing_chain(self, monkeypatch):
        monkeypatch.setenv("HERMES_ROUTER_MODELS", json.dumps(
            [{"provider": "ollama", "model": "kimi:cloud"}]))
        ag = FakeAgent(chain=[{"provider": "x", "model": "preset"}])
        assert mr.maybe_populate_fallback_chain(ag) == 0
        assert ag._fallback_chain == [{"provider": "x", "model": "preset"}]  # untouched

    def test_populates_from_configured_pool(self, monkeypatch):
        monkeypatch.setenv("HERMES_ROUTER_MODELS", json.dumps([
            {"provider": "ollama", "model": "glm-5.2:cloud"},   # == primary → excluded
            {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
            {"provider": "ollama", "model": "minimax-m3:cloud"},
        ]))
        ag = FakeAgent()  # primary glm-5.2:cloud
        n = mr.maybe_populate_fallback_chain(ag)
        assert n == 2  # glm-5.2:cloud dropped (same base as primary); kimi + minimax kept
        assert all("glm-5.2" not in c["model"] for c in ag._fallback_chain)
        assert ag._fallback_model == ag._fallback_chain[0]
        assert ag._fallback_index == 0

    def test_failopen_on_bad_agent(self):
        class Broken:
            @property
            def _fallback_chain(self):
                raise RuntimeError("boom")
        assert mr.maybe_populate_fallback_chain(Broken()) == 0


def test_is_enabled(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL_ROUTER", "1")
    assert mr.is_enabled() is True
    monkeypatch.setenv("HERMES_MODEL_ROUTER", "0")
    assert mr.is_enabled() is False
