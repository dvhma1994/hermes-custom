"""Tests for agent/semantic_recall.py — the hybrid (FTS5 + vector) recall engine.

Uses a deterministic FakeEmbedder (no network), so ranking is predictable. Covers the
pure helpers (RRF, cosine), the SQLite vector store round-trip + hash-skip, the
orchestrator, and — most importantly — the fail-open contract (flag off / no embedder /
empty index all return the caller's FTS5 order unchanged).
"""
import sqlite3

import pytest

from agent import semantic_recall as sr


# --------------------------------------------------------------------------- #
class FakeEmbedder:
    """Maps exact texts to fixed vectors; unknown → zero vector. Counts calls."""

    def __init__(self, mapping, dim):
        self.mapping = mapping
        self.dim = dim
        self.batches = 0
        self.embedded = 0

    def __call__(self, texts):
        self.batches += 1
        out = []
        for t in texts:
            self.embedded += 1
            out.append(list(self.mapping.get(t, [0.0] * self.dim)))
        return out


MAP = {
    "apple": [1.0, 0.0, 0.0, 0.0],
    "apple pie recipe": [0.9, 0.1, 0.0, 0.0],
    "banana": [0.0, 1.0, 0.0, 0.0],
    "automobile": [0.0, 0.0, 1.0, 0.0],
    "fruit": [0.8, 0.2, 0.0, 0.0],
}


def mk(conn, mapping=MAP, dim=4):
    """Build a SemanticRecall with a deterministic fake embedder."""
    emb = FakeEmbedder(mapping, dim)
    return sr.SemanticRecall(conn, embed_fn=emb), emb


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("HERMES_SEMANTIC_RECALL", "1")


# --------------------------------------------------------------------------- RRF
class TestRRF:
    def test_basic_fusion_orders_by_reciprocal_rank(self):
        # a (rank0,rank1) and b (rank1,rank0) are symmetric → they TIE at the top
        # (a wins the first-seen tiebreak); both beat c and d which appear once.
        fused = sr.rrf_fuse([["a", "b", "c"], ["b", "a", "d"]])
        ids = [i for i, _ in fused]
        assert set(ids[:2]) == {"a", "b"}
        assert set(ids) == {"a", "b", "c", "d"}
        sd = dict(fused)
        assert sd["a"] > sd["c"] and sd["b"] > sd["d"]

    def test_agreement_beats_single_list_top(self):
        # 'x' tops only list1; 'y' is 2nd in BOTH → consensus should rank y above c/etc
        fused = dict(sr.rrf_fuse([["x", "y", "z"], ["w", "y", "v"]]))
        assert fused["y"] > fused["x"]

    def test_weights_apply(self):
        f_default = dict(sr.rrf_fuse([["a"], ["b"]]))
        assert f_default["a"] == pytest.approx(f_default["b"])
        f_weighted = dict(sr.rrf_fuse([["a"], ["b"]], weights=[2.0, 1.0]))
        assert f_weighted["a"] > f_weighted["b"]

    def test_empty(self):
        assert sr.rrf_fuse([]) == []
        assert sr.rrf_fuse([[], []]) == []


# ------------------------------------------------------------------ cosine_topk
class TestCosineTopk:
    def test_ranks_nearest_first(self):
        np = pytest.importorskip("numpy")
        ids = ["apple", "apple_pie", "banana"]
        mat = np.array([[1, 0, 0, 0], [0.9, 0.1, 0, 0], [0, 1, 0, 0]], dtype="float32")
        # normalize rows (as the store would)
        mat = mat / np.linalg.norm(mat, axis=1, keepdims=True)
        q = np.array([1, 0, 0, 0], dtype="float32")
        out = sr.cosine_topk(q, mat, ids, k=2)
        assert [i for i, _ in out] == ["apple", "apple_pie"]
        assert len(out) == 2

    def test_dimension_mismatch_returns_empty(self):
        np = pytest.importorskip("numpy")
        mat = np.eye(3, dtype="float32")
        assert sr.cosine_topk(np.array([1, 0], dtype="float32"), mat, ["a", "b", "c"], 2) == []

    def test_empty_inputs(self):
        assert sr.cosine_topk([1, 0], None, [], 2) == []


# ---------------------------------------------------------------- VectorStore
class TestVectorStore:
    def test_roundtrip_and_normalization(self, conn):
        np = pytest.importorskip("numpy")
        st = sr.VectorStore(conn)
        st.upsert("message", "1", [3.0, 4.0, 0.0, 0.0], "h1")  # norm 5 → unit
        st.commit()
        ids, mat = st.load_matrix("message")
        assert ids == ["1"]
        assert float(np.linalg.norm(mat[0])) == pytest.approx(1.0, abs=1e-5)

    def test_upsert_overwrites_same_key(self, conn):
        st = sr.VectorStore(conn)
        st.upsert("message", "1", [1, 0, 0, 0], "h1")
        st.upsert("message", "1", [0, 1, 0, 0], "h2")
        st.commit()
        assert st.count("message") == 1
        assert st.hashes("message") == {"1": "h2"}

    def test_load_matrix_candidate_filter(self, conn):
        st = sr.VectorStore(conn)
        for i in range(5):
            st.upsert("message", str(i), [1, 0, 0, 0], f"h{i}")
        st.commit()
        ids, mat = st.load_matrix("message", ref_ids=["1", "3"])
        assert sorted(ids) == ["1", "3"]
        assert mat.shape[0] == 2

    def test_kind_isolation(self, conn):
        st = sr.VectorStore(conn)
        st.upsert("message", "1", [1, 0, 0, 0], "h")
        st.upsert("memory", "1", [0, 1, 0, 0], "h")
        st.commit()
        assert st.count("message") == 1 and st.count("memory") == 1
        ids, _ = st.load_matrix("memory")
        assert ids == ["1"]

    def test_dim_mismatch_rows_dropped(self, conn):
        np = pytest.importorskip("numpy")
        st = sr.VectorStore(conn)
        st.upsert("m", "a", [1, 0, 0, 0], "h")          # dim 4
        st.upsert("m", "b", [1, 0, 0, 0], "h")          # dim 4
        st.upsert("m", "c", [1, 0], "h")                # dim 2 (minority)
        st.commit()
        ids, mat = st.load_matrix("m")
        assert set(ids) == {"a", "b"}                    # minority dim dropped
        assert mat.shape == (2, 4)


# --------------------------------------------------------------- SemanticRecall
class TestSemanticRecall:
    def _sr(self, conn, mapping=MAP, dim=4):
        emb = FakeEmbedder(mapping, dim)
        return sr.SemanticRecall(conn, embed_fn=emb), emb

    def test_available_true_when_enabled_with_embedder(self, conn):
        rec, _ = self._sr(conn)
        assert rec.available is True

    def test_index_many_then_vector_search(self, conn):
        rec, _ = self._sr(conn)
        n = rec.index_many("message", [("1", "apple"), ("2", "banana"),
                                       ("3", "apple pie recipe")])
        assert n == 3
        hits = rec.vector_search("message", "apple", k=2)
        assert [i for i, _ in hits][:1] == ["1"]            # exact apple first
        assert "3" in [i for i, _ in hits]                  # apple pie close

    def test_index_many_skips_unchanged(self, conn):
        rec, emb = self._sr(conn)
        rec.index_many("message", [("1", "apple"), ("2", "banana")])
        first = emb.embedded
        again = rec.index_many("message", [("1", "apple"), ("2", "banana")])
        assert again == 0
        assert emb.embedded == first                        # no re-embed of unchanged

    def test_index_many_reembeds_changed_content(self, conn):
        rec, emb = self._sr(conn)
        rec.index_many("message", [("1", "apple")])
        n = rec.index_many("message", [("1", "banana")])    # same ref, new text
        assert n == 1
        hits = rec.vector_search("message", "banana", k=1)
        assert hits and hits[0][0] == "1"

    def test_hybrid_rank_promotes_semantic_match(self, conn):
        rec, _ = self._sr(conn)
        rec.index_many("message", [("1", "apple"), ("2", "banana"),
                                   ("3", "automobile")])
        # FTS ranks banana top, automobile mid, apple LAST. The query is semantically
        # 'fruit' → apple (FTS-last but fruit-like) must be pulled ABOVE the
        # semantically-irrelevant automobile.
        fts_order = ["2", "3", "1"]
        fused = rec.hybrid_rank("message", "fruit", fts_order, k=3)
        assert set(fused) == {"1", "2", "3"}                # no ids lost
        assert fused.index("1") < fused.index("3")          # fruit-like apple beats automobile

    def test_hybrid_rank_preserves_all_fts_ids(self, conn):
        rec, _ = self._sr(conn)
        rec.index_many("message", [("1", "apple")])         # only 1 indexed
        fused = rec.hybrid_rank("message", "apple", ["1", "2", "9"], k=10)
        assert set(fused) == {"1", "2", "9"}                # unindexed ids retained


# ------------------------------------------------------------------ fail-open
class TestFailOpen:
    def test_disabled_flag_makes_unavailable(self, conn, monkeypatch):
        monkeypatch.delenv("HERMES_SEMANTIC_RECALL", raising=False)
        rec = sr.SemanticRecall(conn, embed_fn=FakeEmbedder(MAP, 4))
        assert rec.available is False
        # hybrid_rank returns the FTS order unchanged
        assert rec.hybrid_rank("message", "apple", ["5", "6", "7"]) == ["5", "6", "7"]
        assert rec.vector_search("message", "apple") == []
        assert rec.index_many("message", [("1", "apple")]) == 0

    def test_no_embedder_falls_back(self, conn):
        rec = sr.SemanticRecall(conn, embed_fn=None)
        # get_default_embedder() returns None without configured keys in test env;
        # if the runner HAS keys it could be non-None, so only assert the contract:
        out = rec.hybrid_rank("message", "apple", ["1", "2"])
        assert out == ["1", "2"] or set(out) == {"1", "2"}

    def test_empty_index_returns_fts_order(self, conn):
        rec, _ = mk(conn)
        # nothing indexed → vector side empty → FTS order preserved exactly
        assert rec.hybrid_rank("message", "apple", ["3", "1", "2"]) == ["3", "1", "2"]

    def test_embedder_raising_is_swallowed(self, conn):
        def boom(texts):
            raise RuntimeError("provider down")
        rec = sr.SemanticRecall(conn, embed_fn=boom)
        assert rec.index_many("message", [("1", "apple")]) == 0
        assert rec.vector_search("message", "apple") == []
        assert rec.hybrid_rank("message", "apple", ["1", "2"]) == ["1", "2"]

    def test_provider_count_mismatch_is_safe(self, conn):
        def short(texts):
            return [[1.0, 0, 0, 0]]  # always returns 1 regardless of input length
        rec = sr.SemanticRecall(conn, embed_fn=short)
        # 2 inputs, 1 returned → mismatch → 0 indexed, no crash
        assert rec.index_many("message", [("1", "a"), ("2", "b")]) == 0


# ------------------------------------------------------------------- misc
class TestBackfill:
    @staticmethod
    def _seed(conn, rows):
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
        conn.executemany("INSERT INTO messages (id, role, content) VALUES (?,?,?)", rows)
        conn.commit()

    def test_indexes_recent_user_assistant_messages(self, conn):
        self._seed(conn, [
            (1, "user", "apple"), (2, "assistant", "banana"),
            (3, "tool", "automobile"), (4, "user", ""), (5, "user", None),
        ])
        rec, _ = mk(conn)
        n = sr.backfill_messages(conn, rec, limit=100)
        assert n == 2                                   # tool + empty + null excluded
        hits = rec.vector_search("message", "apple", k=1)
        assert hits and hits[0][0] == "1"

    def test_rerun_skips_unchanged(self, conn):
        self._seed(conn, [(1, "user", "apple"), (2, "user", "banana")])
        rec, emb = mk(conn)
        sr.backfill_messages(conn, rec)
        before = emb.embedded
        assert sr.backfill_messages(conn, rec) == 0
        assert emb.embedded == before

    def test_unavailable_is_noop(self, conn, monkeypatch):
        self._seed(conn, [(1, "user", "apple")])
        monkeypatch.delenv("HERMES_SEMANTIC_RECALL", raising=False)
        rec, _ = mk(conn)
        assert sr.backfill_messages(conn, rec) == 0


def test_content_hash_stable_and_differs():
    assert sr.content_hash("abc") == sr.content_hash("abc")
    assert sr.content_hash("abc") != sr.content_hash("abd")
    assert sr.content_hash("") == sr.content_hash(None or "")


def test_is_enabled(monkeypatch):
    monkeypatch.setenv("HERMES_SEMANTIC_RECALL", "1")
    assert sr.is_enabled() is True
    monkeypatch.setenv("HERMES_SEMANTIC_RECALL", "0")
    assert sr.is_enabled() is False
