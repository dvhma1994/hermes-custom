"""Tests for agent/skill_induction.py — read-only mining of recurring tool workflows."""
import sqlite3

from agent import skill_induction as si


class TestCollapse:
    def test_collapses_consecutive_repeats(self):
        assert si.collapse_repeats(["a", "a", "b", "b", "b", "a"]) == ["a", "b", "a"]

    def test_empty(self):
        assert si.collapse_repeats([]) == []


class TestExtract:
    def test_groups_by_session_and_collapses(self):
        rows = [
            ("s1", "read_file"), ("s1", "read_file"), ("s1", "patch"),
            ("s2", "terminal"),
        ]
        seqs = si.extract_session_sequences(rows)
        assert seqs["s1"] == ["read_file", "patch"]
        assert seqs["s2"] == ["terminal"]


class TestMine:
    def _seqs(self):
        # workflow search→read→patch recurs in 3 sessions; terminal-only in 1
        return {
            "s1": ["search_files", "read_file", "patch", "terminal"],
            "s2": ["search_files", "read_file", "patch"],
            "s3": ["search_files", "read_file", "patch", "write_file"],
            "s4": ["todo"],
        }

    def test_finds_cross_session_workflow(self):
        cands = si.mine_frequent_ngrams(self._seqs(), ngram=(2, 4), min_sessions=3)
        seqs = [tuple(c["sequence"]) for c in cands]
        assert ("search_files", "read_file", "patch") in seqs
        # support is distinct sessions
        c = next(c for c in cands if c["sequence"] == ["search_files", "read_file", "patch"])
        assert c["session_support"] == 3

    def test_min_sessions_threshold(self):
        # nothing recurs across >=4 sessions here
        assert si.mine_frequent_ngrams(self._seqs(), min_sessions=4) == []

    def test_ignores_lone_tool_repetition(self):
        seqs = {f"s{i}": ["read_file", "read_file", "read_file"] for i in range(5)}
        # collapse already happens in extract, but mine also guards _all_same
        raw = {f"s{i}": ["read_file", "x", "read_file"] for i in range(5)}
        cands = si.mine_frequent_ngrams(raw, ngram=(2, 2), min_sessions=3)
        assert all(not si._all_same(tuple(c["sequence"])) for c in cands)


class TestRank:
    def test_orders_by_support_then_length(self):
        cands = [
            {"sequence": ["a", "b"], "length": 2, "session_support": 2, "total_count": 9},
            {"sequence": ["c", "d", "e"], "length": 3, "session_support": 5, "total_count": 5},
        ]
        ranked = si.rank_candidates(cands)
        assert ranked[0]["sequence"] == ["c", "d", "e"]  # higher support wins

    def test_drops_subsumed_shorter_with_le_support(self):
        cands = [
            {"sequence": ["a", "b", "c"], "length": 3, "session_support": 5, "total_count": 5},
            {"sequence": ["a", "b"], "length": 2, "session_support": 5, "total_count": 6},  # subset, == support
        ]
        ranked = si.rank_candidates(cands)
        seqs = [c["sequence"] for c in ranked]
        assert ["a", "b", "c"] in seqs
        assert ["a", "b"] not in seqs  # dropped: subsumed by longer with >= support

    def test_keeps_shorter_if_more_support(self):
        cands = [
            {"sequence": ["a", "b", "c"], "length": 3, "session_support": 3, "total_count": 3},
            {"sequence": ["a", "b"], "length": 2, "session_support": 9, "total_count": 9},  # more support
        ]
        seqs = [c["sequence"] for c in si.rank_candidates(cands)]
        assert ["a", "b"] in seqs  # retained — it's more broadly useful

    def test_top_k_cap(self):
        cands = [{"sequence": [f"t{i}", "x"], "length": 2, "session_support": 5, "total_count": 5}
                 for i in range(30)]
        assert len(si.rank_candidates(cands, top_k=5)) == 5


class TestFormatAndDb:
    def test_format_empty(self):
        assert "No recurring" in si.format_report([])

    def test_format_has_flow_arrows(self):
        rep = si.format_report([{"sequence": ["a", "b"], "length": 2,
                                 "session_support": 3, "total_count": 4}])
        assert "a → b" in rep and "3 sessions" in rep

    def test_propose_from_db_failopen_no_conn(self):
        class NoConn:
            _conn = None
        assert si.propose_skills_from_db(NoConn()) == []

    def test_propose_from_db_end_to_end(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, tool_name TEXT)")
        mid = 0
        for s in range(4):  # 4 sessions sharing search→read→patch
            for tool in ["search_files", "read_file", "patch"]:
                mid += 1
                conn.execute("INSERT INTO messages (id, session_id, tool_name) VALUES (?,?,?)",
                             (mid, f"s{s}", tool))
        conn.commit()

        class DB:
            _conn = conn
        out = si.propose_skills_from_db(DB(), min_sessions=4)
        assert out and out[0]["sequence"] == ["search_files", "read_file", "patch"]
        assert out[0]["session_support"] == 4
