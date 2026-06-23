"""Hybrid semantic recall — flag-gated (``HERMES_SEMANTIC_RECALL=1``), fail-open.

The session store's recall is FTS5 keyword-only, so a query phrased differently from
the stored text misses. This module adds a VECTOR index fused with the existing FTS5
ranking via Reciprocal Rank Fusion (RRF), so semantically-similar hits surface even
without lexical overlap.

Design constraints (matches the rest of the agent):
  * Dependency-light: the engine uses only ``numpy`` + stdlib ``sqlite3``. No vector
    extension, no heavy ML deps.
  * Pluggable embedder: embeddings come from an injected ``embed_fn``; the default
    builds an OpenAI-compatible client from the user's existing keys, and returns
    ``None`` (→ feature is a no-op) when nothing is configured.
  * Fail-open EVERYWHERE: every public method swallows its own errors and falls back
    to the caller's existing behaviour. When the flag is off, no embedder is available,
    or the index is empty, callers get their original FTS5 results unchanged.
  * ISOLATED storage: the vector index lives in a SIDECAR SQLite file next to the
    session DB (``state.db.semantic.db``), on its OWN connection + lock — it never
    writes through the live ``state.db`` connection, so it cannot interfere with the
    agent's transaction/locking discipline.

Nothing here mutates the system prompt or tool list, so prompt caching is unaffected.

SECURITY NOTE: indexed text (message/memory content) is sent to the configured
embedding provider. Prefer the LOCAL ``ollama_local`` provider (default-recommended)
so content never leaves the machine; a remote provider (openai/ollama-cloud) would
transmit stored text that may contain secrets from prior turns.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# An embedder maps a batch of texts to a batch of float vectors.
Embedder = Callable[[List[str]], List[List[float]]]

_TABLE = "semantic_index"
_MAX_BATCH = 64          # texts per embedding request
_MAX_TEXT_CHARS = 8000   # truncate very long texts before embedding
_RRF_K = 60              # standard RRF damping constant

# Sidecar connection cache: one connection + one lock per index file, reused for the
# process lifetime. Keeps the vector index fully isolated from the live state.db conn.
_SIDECAR_CONNS: dict = {}
_SIDECAR_LOCKS: dict = {}
_CACHE_GUARD = threading.Lock()


def is_enabled() -> bool:
    """True only when the operator opted in via the env flag."""
    return os.getenv("HERMES_SEMANTIC_RECALL") == "1"


def sidecar_path_for(db_path) -> Optional[str]:
    """Derive the sidecar index path from a session DB path (per-profile). None if
    no usable path (→ feature disables, FTS-only)."""
    try:
        p = os.fspath(db_path)
        return p + ".semantic.db" if p else None
    except Exception:
        return None


def _open_sidecar(path: str):
    """Return (conn, lock) for a sidecar index file, cached per path. WAL so readers
    never block the (rare, operator-triggered) writer. Returns (None, None) on error."""
    with _CACHE_GUARD:
        conn = _SIDECAR_CONNS.get(path)
        if conn is not None:
            return conn, _SIDECAR_LOCKS[path]
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                logger.debug("sidecar PRAGMA failed", exc_info=True)
            lock = threading.Lock()
            _SIDECAR_CONNS[path] = conn
            _SIDECAR_LOCKS[path] = lock
            return conn, lock
        except Exception:
            logger.debug("sidecar open failed for %s", path, exc_info=True)
            return None, None


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O) — the testable core.
# --------------------------------------------------------------------------- #
def content_hash(text: str) -> str:
    """Stable hash of the embedded text, so unchanged rows are not re-embedded."""
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


def rrf_fuse(
    ranked_lists: Sequence[Sequence[object]],
    k: int = _RRF_K,
    weights: Optional[Sequence[float]] = None,
) -> List[Tuple[object, float]]:
    """Reciprocal Rank Fusion of several ranked id lists.

    Each input list is ordered best-first. An id's fused score is
    ``sum(weight_l / (k + rank_in_l))`` over the lists it appears in (rank is
    0-based). Returns ``[(id, score), ...]`` sorted by score desc, ties broken by
    first appearance for determinism. Ids may be any hashable; unknown ids in some
    lists are simply absent from those terms.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    scores: dict = {}
    first_seen: dict = {}
    order = 0
    for li, lst in enumerate(ranked_lists):
        w = weights[li] if li < len(weights) else 1.0
        for rank, _id in enumerate(lst):
            scores[_id] = scores.get(_id, 0.0) + w / (k + rank)
            if _id not in first_seen:
                first_seen[_id] = order
                order += 1
    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))


def _np():
    """Import numpy lazily; return None if unavailable (fail-open)."""
    try:
        import numpy as np  # noqa
        return np
    except Exception:  # pragma: no cover - numpy is a hard dep in practice
        return None


def _normalize(vec) -> "object":
    """L2-normalize a 1-D float32 numpy vector (so cosine == dot). Zero stays zero."""
    np = _np()
    v = np.asarray(vec, dtype="float32").ravel()
    n = float(np.linalg.norm(v))
    if n > 0.0:
        v = v / n
    return v


def cosine_topk(query_vec, matrix, ids: Sequence[object], k: int) -> List[Tuple[object, float]]:
    """Top-k by cosine similarity. ``query_vec`` and ``matrix`` rows are assumed
    L2-normalized, so similarity is a plain dot product. Returns ``[(id, score)]``
    best-first. Fail-open: returns ``[]`` on any error or empty input.
    """
    np = _np()
    if np is None or matrix is None or len(ids) == 0 or k <= 0:
        return []
    try:
        q = np.asarray(query_vec, dtype="float32").ravel()
        if q.shape[0] != matrix.shape[1]:
            return []  # dimension mismatch (e.g. embedder/model changed) → skip
        sims = matrix @ q
        n = sims.shape[0]
        kk = min(k, n)
        # argpartition for the top-kk, then sort just those.
        idx = np.argpartition(-sims, kk - 1)[:kk]
        idx = idx[np.argsort(-sims[idx])]
        return [(ids[i], float(sims[i])) for i in idx]
    except Exception:
        logger.debug("cosine_topk failed", exc_info=True)
        return []


# --------------------------------------------------------------------------- #
# Vector store (SQLite-backed blobs).
# --------------------------------------------------------------------------- #
class VectorStore:
    """Stores L2-normalized float32 embeddings as blobs in a SQLite table.

    Rows are keyed by (kind, ref_id) — e.g. kind="message"/ref_id=str(message_id),
    or kind="memory"/ref_id="MEMORY.md#3". ``content_hash`` lets the indexer skip
    rows whose text is unchanged.
    """

    def __init__(self, conn, lock=None):
        self._conn = conn
        # Serialize ALL access to this connection (CPython sqlite3 is not safe for
        # concurrent use of one connection). For the sidecar this is the per-file lock.
        self._lock = lock or threading.Lock()
        self._ensure()

    def _ensure(self) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                        kind         TEXT NOT NULL,
                        ref_id       TEXT NOT NULL,
                        dim          INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        embedding    BLOB NOT NULL,
                        updated_at   REAL NOT NULL,
                        PRIMARY KEY (kind, ref_id)
                    )"""
                )
                self._conn.commit()
        except Exception:
            logger.debug("VectorStore._ensure failed", exc_info=True)

    def hashes(self, kind: str) -> dict:
        """{ref_id: content_hash} for a kind, so the indexer can diff before embedding."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT ref_id, content_hash FROM {_TABLE} WHERE kind = ?", (kind,)
                ).fetchall()
            return {r[0]: r[1] for r in rows}
        except Exception:
            logger.debug("VectorStore.hashes failed", exc_info=True)
            return {}

    def upsert(self, kind: str, ref_id: str, vec, chash: str) -> bool:
        np = _np()
        if np is None:
            return False
        try:
            v = _normalize(vec).astype("float32")
            blob = v.tobytes()
            with self._lock:
                self._conn.execute(
                    f"""INSERT INTO {_TABLE} (kind, ref_id, dim, content_hash, embedding, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(kind, ref_id) DO UPDATE SET
                            dim=excluded.dim, content_hash=excluded.content_hash,
                            embedding=excluded.embedding, updated_at=excluded.updated_at""",
                    (kind, str(ref_id), int(v.shape[0]), chash, blob, time.time()),
                )
            return True
        except Exception:
            logger.debug("VectorStore.upsert failed", exc_info=True)
            return False

    def commit(self) -> None:
        try:
            with self._lock:
                self._conn.commit()
        except Exception:
            logger.debug("VectorStore.commit failed", exc_info=True)

    def load_matrix(self, kind: str, ref_ids: Optional[Sequence[str]] = None):
        """Return (ids, matrix) of stored normalized vectors for a kind.

        If ``ref_ids`` is given, only those rows load (used to restrict scoring to the
        FTS5 candidate set). Returns ([], None) when empty or on error. Rows whose
        dim disagrees with the majority are dropped (guards a mid-flight model change).
        """
        np = _np()
        if np is None:
            return [], None
        try:
            with self._lock:
                if ref_ids is not None:
                    ref_ids = [str(r) for r in ref_ids]
                    if not ref_ids:
                        return [], None
                    # chunk the IN clause to stay well under SQLite's variable limit
                    rows = []
                    for i in range(0, len(ref_ids), 400):
                        chunk = ref_ids[i:i + 400]
                        ph = ",".join("?" * len(chunk))
                        rows.extend(self._conn.execute(
                            f"SELECT ref_id, dim, embedding FROM {_TABLE} "
                            f"WHERE kind = ? AND ref_id IN ({ph})",
                            (kind, *chunk),
                        ).fetchall())
                else:
                    rows = self._conn.execute(
                        f"SELECT ref_id, dim, embedding FROM {_TABLE} WHERE kind = ?", (kind,)
                    ).fetchall()
            if not rows:
                return [], None
            # Pick the dominant dimension; drop mismatches.
            from collections import Counter
            dim = Counter(r[1] for r in rows).most_common(1)[0][0]
            ids, vecs = [], []
            for ref_id, d, blob in rows:
                if d != dim:
                    continue
                arr = np.frombuffer(blob, dtype="float32")
                if arr.shape[0] != dim:
                    continue
                ids.append(ref_id)
                vecs.append(arr)
            if not ids:
                return [], None
            return ids, np.vstack(vecs)
        except Exception:
            logger.debug("VectorStore.load_matrix failed", exc_info=True)
            return [], None

    def count(self, kind: str) -> int:
        try:
            with self._lock:
                return int(self._conn.execute(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE kind = ?", (kind,)
                ).fetchone()[0])
        except Exception:
            return 0


# --------------------------------------------------------------------------- #
# Default embedder (OpenAI-compatible; optional).
# --------------------------------------------------------------------------- #
def _ollama_native_embedder(base: str, model: str) -> Embedder:
    """Embedder for a LOCAL Ollama daemon's native API (no openai SDK / no key).

    Prefers the batch ``/api/embed`` endpoint, falls back to per-text
    ``/api/embeddings``. This is the zero-cost fully-local path (e.g. nomic-embed-text
    on http://localhost:11500). Raises on connection errors — the caller is fail-open.
    """
    import json
    import urllib.request

    root = base.rstrip("/")

    def _embed(texts: List[str]) -> List[List[float]]:
        texts = list(texts)
        # Batch endpoint (newer Ollama).
        try:
            req = urllib.request.Request(
                root + "/api/embed",
                data=json.dumps({"model": model, "input": texts}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                embs = json.loads(r.read().decode("utf-8")).get("embeddings")
            if embs and len(embs) == len(texts):
                return embs
        except Exception:
            logger.debug("ollama /api/embed batch failed; falling back", exc_info=True)
        # Per-text fallback (older Ollama).
        out: List[List[float]] = []
        for t in texts:
            req = urllib.request.Request(
                root + "/api/embeddings",
                data=json.dumps({"model": model, "prompt": t}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                out.append(json.loads(r.read().decode("utf-8"))["embedding"])
        return out

    return _embed


def get_default_embedder() -> Optional[Embedder]:
    """Build an embedder from env, or return None (→ feature no-op).

    Provider (``HERMES_EMBED_PROVIDER`` = ollama_local|openai|ollama):
      * ollama_local — LOCAL Ollama native API at ``HERMES_EMBED_BASE`` (default
        http://localhost:11500), model ``HERMES_EMBED_MODEL`` or nomic-embed-text.
        Zero-cost, fully local, no key. RECOMMENDED for this install.
      * openai  — OPENAI_API_KEY, model ``HERMES_EMBED_MODEL`` or text-embedding-3-small
      * ollama  — OLLAMA_API_KEY + OLLAMA_BASE_URL (OpenAI-compat /v1/embeddings),
        model ``HERMES_EMBED_MODEL`` or nomic-embed-text
    Default when unset: openai if OPENAI_API_KEY is present, else None.
    Returns None on any missing config / import error — never raises.
    """
    if not is_enabled():
        return None

    provider = (os.getenv("HERMES_EMBED_PROVIDER") or "").strip().lower()
    model = (os.getenv("HERMES_EMBED_MODEL") or "").strip()

    # Local Ollama native path — no SDK, no key, fully local.
    if provider == "ollama_local":
        base = (os.getenv("HERMES_EMBED_BASE") or "http://localhost:11500").strip()
        return _ollama_native_embedder(base, model or "nomic-embed-text")

    try:
        from openai import OpenAI
    except Exception:
        logger.debug("openai SDK unavailable; semantic recall embedder disabled")
        return None

    provider = (os.getenv("HERMES_EMBED_PROVIDER") or "").strip().lower()
    model = (os.getenv("HERMES_EMBED_MODEL") or "").strip()
    api_key = base_url = None

    if provider == "ollama" or (not provider and not os.getenv("OPENAI_API_KEY") and os.getenv("OLLAMA_API_KEY")):
        api_key = os.getenv("OLLAMA_API_KEY")
        base_url = os.getenv("OLLAMA_BASE_URL") or None
        model = model or "nomic-embed-text"
        if not api_key or not base_url:
            return None
    else:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = (os.getenv("OPENAI_BASE_URL") or "").strip() or None
        model = model or "text-embedding-3-small"
        if not api_key:
            return None

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception:
        logger.debug("OpenAI client build failed for embedder", exc_info=True)
        return None

    def _embed(texts: List[str]) -> List[List[float]]:
        resp = client.embeddings.create(model=model, input=list(texts))
        # Preserve request order (OpenAI returns .index but data is already ordered).
        return [d.embedding for d in resp.data]

    return _embed


# --------------------------------------------------------------------------- #
# Orchestrator.
# --------------------------------------------------------------------------- #
class SemanticRecall:
    """Index + recall facade. All public methods are fail-open.

    Construct with an explicit ``conn`` (tests) or, preferably, via
    :meth:`for_db` which routes storage to an isolated sidecar file so the live
    ``state.db`` connection is never touched.
    """

    def __init__(self, conn, embed_fn: Optional[Embedder] = None, lock=None):
        self._conn = conn
        self._store = VectorStore(conn, lock=lock) if conn is not None else None
        # Resolve the embedder once; None disables vector ops (FTS5-only fallback).
        self._embed = embed_fn if embed_fn is not None else get_default_embedder()

    @classmethod
    def for_db(cls, db, embed_fn: Optional[Embedder] = None) -> "SemanticRecall":
        """Build a recall facade whose index lives in a sidecar file next to ``db``.

        Derives the sidecar path from ``db.db_path`` (so it is per-profile and never
        shares the live state.db connection). Falls back to a disabled instance
        (FTS-only) when the path can't be resolved or the sidecar can't open.
        """
        try:
            path = sidecar_path_for(getattr(db, "db_path", None))
            if not path:
                return cls(conn=None, embed_fn=embed_fn)
            conn, lock = _open_sidecar(path)
            if conn is None:
                return cls(conn=None, embed_fn=embed_fn)
            return cls(conn=conn, embed_fn=embed_fn, lock=lock)
        except Exception:
            logger.debug("SemanticRecall.for_db failed", exc_info=True)
            return cls(conn=None, embed_fn=embed_fn)

    @classmethod
    def for_default(cls, embed_fn: Optional[Embedder] = None) -> "SemanticRecall":
        """Build a recall facade over the DEFAULT session DB's sidecar — for callers
        (e.g. tool handlers) that aren't handed a db object. Falls back to a disabled
        instance if the default path can't be resolved/opened."""
        try:
            from hermes_state import DEFAULT_DB_PATH
            path = sidecar_path_for(DEFAULT_DB_PATH)
            if not path:
                return cls(conn=None, embed_fn=embed_fn)
            conn, lock = _open_sidecar(path)
            if conn is None:
                return cls(conn=None, embed_fn=embed_fn)
            return cls(conn=conn, embed_fn=embed_fn, lock=lock)
        except Exception:
            logger.debug("SemanticRecall.for_default failed", exc_info=True)
            return cls(conn=None, embed_fn=embed_fn)

    @property
    def available(self) -> bool:
        """True when the flag is on, an embedder exists, and a store is open."""
        return is_enabled() and self._embed is not None and self._store is not None

    def _embed_batch(self, texts: List[str]) -> Optional[List[list]]:
        if not self._embed or not texts:
            return None
        out: List[list] = []
        try:
            for i in range(0, len(texts), _MAX_BATCH):
                chunk = [(t or "")[:_MAX_TEXT_CHARS] for t in texts[i:i + _MAX_BATCH]]
                vecs = self._embed(chunk)
                # Per-chunk count check: a provider returning the wrong number for ANY
                # chunk would mis-pair vectors↔texts in index_many's zip → bail safely.
                if vecs is None or len(vecs) != len(chunk):
                    return None
                out.extend(vecs)
            if len(out) != len(texts):
                return None
            return out
        except Exception:
            logger.debug("embed batch failed", exc_info=True)
            return None

    def index_many(self, kind: str, items: Sequence[Tuple[str, str]]) -> int:
        """Embed + upsert ``[(ref_id, text), ...]``, skipping unchanged content.

        Returns the number of rows (re)indexed. Fail-open → 0 on any error.
        """
        if not self.available or not items:
            return 0
        try:
            existing = self._store.hashes(kind)
            todo: List[Tuple[str, str, str]] = []  # (ref_id, text, hash)
            for ref_id, text in items:
                h = content_hash(text)
                if existing.get(str(ref_id)) != h:
                    todo.append((str(ref_id), text, h))
            if not todo:
                return 0
            vecs = self._embed_batch([t for _, t, _ in todo])
            if vecs is None:
                return 0
            n = 0
            for (ref_id, _text, h), vec in zip(todo, vecs):
                if self._store.upsert(kind, ref_id, vec, h):
                    n += 1
            self._store.commit()
            return n
        except Exception:
            logger.debug("index_many failed", exc_info=True)
            return 0

    def vector_search(self, kind: str, query: str, k: int = 20,
                      candidate_ids: Optional[Sequence[str]] = None) -> List[Tuple[str, float]]:
        """Embed ``query`` and return top-k (ref_id, score). ``candidate_ids`` limits
        scoring to that set (e.g. the FTS5 hits). Fail-open → []."""
        if not self.available or not query or not query.strip():
            return []
        try:
            qv = self._embed_batch([query])
            if not qv:
                return []
            ids, mat = self._store.load_matrix(kind, ref_ids=candidate_ids)
            if mat is None:
                return []
            return cosine_topk(_normalize(qv[0]), mat, ids, k)
        except Exception:
            logger.debug("vector_search failed", exc_info=True)
            return []

    def hybrid_rank(self, kind: str, query: str, fts_ranked_ids: Sequence[str],
                    k: int = 50) -> List[str]:
        """Fuse the caller's FTS5 ranking with a vector ranking via RRF.

        Returns a reordered list of ref_ids. When unavailable, the vector side is
        empty, or anything fails, returns ``list(fts_ranked_ids)`` UNCHANGED — so the
        caller always gets at least its original FTS5 order.
        """
        fts_ids = [str(x) for x in fts_ranked_ids]
        if not self.available or not fts_ids:
            return fts_ids
        try:
            # Restrict vector scoring to the FTS5 candidates so this is a re-ranking,
            # not a recall-expanding scan (cheap, and keeps results grounded in FTS5).
            vec_hits = self.vector_search(kind, query, k=len(fts_ids), candidate_ids=fts_ids)
            if not vec_hits:
                return fts_ids
            vec_ids = [vid for vid, _ in vec_hits]
            fused = rrf_fuse([fts_ids, vec_ids])
            ranked = [rid for rid, _ in fused]
            # Safety: keep any FTS id that somehow dropped out, appended in FTS order.
            seen = set(ranked)
            ranked.extend([i for i in fts_ids if i not in seen])
            return ranked[:k] if k and k > 0 else ranked
        except Exception:
            logger.debug("hybrid_rank failed", exc_info=True)
            return fts_ids


def backfill_messages(conn, recall: "SemanticRecall", limit: int = 2000,
                      roles: Sequence[str] = ("user", "assistant")) -> int:
    """Index the most recent ``limit`` messages into the vector store.

    A one-shot/operator utility (the live agent does not auto-backfill the whole
    history — too expensive). Reads the ``messages`` table directly. Fail-open → 0.
    Re-running is cheap: ``index_many`` skips rows whose content hash is unchanged.
    """
    if recall is None or conn is None or not getattr(recall, "available", False):
        return 0
    try:
        roles = tuple(roles) or ("user", "assistant")
        ph = ",".join("?" * len(roles))
        rows = conn.execute(
            f"SELECT id, content FROM messages "
            f"WHERE role IN ({ph}) AND content IS NOT NULL AND content != '' "
            f"ORDER BY id DESC LIMIT ?",
            (*roles, int(limit)),
        ).fetchall()
        items = [(str(r[0]), r[1]) for r in rows if r[1]]
        return recall.index_many("message", items)
    except Exception:
        logger.debug("backfill_messages failed", exc_info=True)
        return 0


def backfill_for_db(db, limit: int = 2000, embed_fn: Optional[Embedder] = None) -> int:
    """Operator convenience: index recent messages from ``db`` into its sidecar index.

    Reads messages from the live ``state.db`` (via ``db._conn``) and writes vectors to
    the isolated sidecar (via ``SemanticRecall.for_db``). Returns rows indexed (0 if the
    feature is disabled / no embedder). Safe to re-run (skips unchanged content).
    """
    rec = SemanticRecall.for_db(db, embed_fn=embed_fn)
    return backfill_messages(getattr(db, "_conn", None), rec, limit=limit)
