"""Tests for tools/skill_search_tool.py — hybrid (keyword + semantic) skill lookup."""
import json

import pytest

from tools import skill_search_tool as sst


SKILLS = [
    {"name": "deep-verify", "description": "reproduce then check before done", "category": "quality"},
    {"name": "scout", "description": "explore the codebase via delegate, return citations", "category": "research"},
    {"name": "orchestrate", "description": "coordinate multiple subagents on a plan", "category": "workflow"},
]


class TestKeywordRank:
    def test_name_match_weighted_above_description(self):
        ranked = sst._keyword_rank(SKILLS, "scout")
        assert ranked[0] == "scout"

    def test_description_overlap_ranks(self):
        ranked = sst._keyword_rank(SKILLS, "explore codebase")
        assert ranked[0] == "scout"

    def test_returns_all_skills(self):
        assert set(sst._keyword_rank(SKILLS, "anything")) == {s["name"] for s in SKILLS}


class TestSkillSearch:
    def test_empty_query_errors(self):
        out = json.loads(sst.skill_search(query=""))
        assert out["success"] is False

    def test_no_skills(self, monkeypatch):
        monkeypatch.setattr(sst, "_load_skills", lambda: [])
        out = json.loads(sst.skill_search(query="x"))
        assert out["success"] is True and out["results"] == []

    def test_keyword_mode_when_semantic_off(self, monkeypatch):
        monkeypatch.setattr(sst, "_load_skills", lambda: SKILLS)
        monkeypatch.delenv("HERMES_SEMANTIC_RECALL", raising=False)
        out = json.loads(sst.skill_search(query="explore codebase", limit=2))
        assert out["mode"] == "keyword"
        assert out["results"][0]["name"] == "scout"
        assert out["count"] == 2
        assert out["results"][0]["category"] == "research"

    def test_limit_clamped(self, monkeypatch):
        monkeypatch.setattr(sst, "_load_skills", lambda: SKILLS)
        out = json.loads(sst.skill_search(query="x", limit=999))
        assert out["count"] <= len(SKILLS)


class TestHybridMode:
    def test_semantic_fuses_and_marks_hybrid(self, monkeypatch):
        monkeypatch.setattr(sst, "_load_skills", lambda: SKILLS)
        monkeypatch.setenv("HERMES_SEMANTIC_RECALL", "1")

        from agent import semantic_recall as sr

        class FakeRec:
            available = True
            def index_many(self, kind, items):
                return len(items)
            def vector_search(self, kind, query, k=20, candidate_ids=None):
                # semantic says scout best, then orchestrate, then deep-verify
                return [("scout", 0.9), ("orchestrate", 0.5), ("deep-verify", 0.1)]

        monkeypatch.setattr(sr.SemanticRecall, "for_db", classmethod(lambda cls, db, embed_fn=None: FakeRec()))
        monkeypatch.setattr(sr, "is_enabled", lambda: True)

        out = json.loads(sst.skill_search(query="find things in the repo", limit=3, db=object()))
        assert out["mode"] == "hybrid"
        assert out["results"][0]["name"] == "scout"
        assert {r["name"] for r in out["results"]} == {s["name"] for s in SKILLS}

    def test_semantic_failure_falls_back_to_keyword(self, monkeypatch):
        monkeypatch.setattr(sst, "_load_skills", lambda: SKILLS)
        monkeypatch.setenv("HERMES_SEMANTIC_RECALL", "1")
        from agent import semantic_recall as sr

        def boom(cls, db, embed_fn=None):
            raise RuntimeError("embedder down")
        monkeypatch.setattr(sr.SemanticRecall, "for_db", classmethod(boom))
        monkeypatch.setattr(sr, "is_enabled", lambda: True)

        out = json.loads(sst.skill_search(query="scout", limit=1, db=object()))
        assert out["mode"] == "keyword"          # degraded safely
        assert out["results"][0]["name"] == "scout"


def test_tokens():
    assert sst._tokens("Hello, World-2!") == ["hello", "world", "2"]
