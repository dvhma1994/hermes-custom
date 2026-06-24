"""Unit tests for agent/learning_demotion_recommendations.py."""
import sqlite3
import time
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_demotion_recommendations import LearningDemotionRecommendations
from agent.opval.store import OpvalStore


def _make_store(tmp_path):
    db = tmp_path / "ldr.db"
    return OpvalStore(str(db))


def test_recommendation_queue_insert(tmp_path):
    store = _make_store(tmp_path)
    queue = LearningDemotionRecommendations(store._conn)
    rec_id = queue.enqueue("authority_alignment", "strategy:coding", "low win rate")
    assert rec_id
    all_recs = queue.get_all()
    assert len(all_recs) == 1
    assert all_recs[0].strategy_id == "strategy:coding"


def test_recommendation_queue_poll_unprocessed(tmp_path):
    store = _make_store(tmp_path)
    queue = LearningDemotionRecommendations(store._conn)
    rec_id = queue.enqueue("authority_alignment", "strategy:coding", "low win rate")
    queue.mark_processed(rec_id)
    unprocessed = queue.get_unprocessed("strategy:coding")
    assert len(unprocessed) == 0


def test_recommendation_queue_mark_processed(tmp_path):
    store = _make_store(tmp_path)
    queue = LearningDemotionRecommendations(store._conn)
    rec_id = queue.enqueue("authority_alignment", "strategy:coding", "low win rate")
    assert queue.mark_processed(rec_id)
    assert queue.get_unprocessed_count() == 0
    assert not queue.mark_processed(rec_id)  # already processed


def test_recommendation_queue_expiry_after_max_age(tmp_path):
    store = _make_store(tmp_path)
    queue = LearningDemotionRecommendations(store._conn)
    old_time = time.time() - (lc.DEMOTION_RECOMMENDATION_MAX_AGE_DAYS * 24 * 3600 + 1)
    queue.enqueue("authority_alignment", "strategy:coding", "old", created_at=old_time)
    unprocessed = queue.get_unprocessed("strategy:coding")
    assert len(unprocessed) == 0


def test_recommendation_queue_no_double_insert(tmp_path):
    store = _make_store(tmp_path)
    queue = LearningDemotionRecommendations(store._conn)
    rec_id = queue.enqueue("authority_alignment", "strategy:coding", "reason", rec_id="fixed-id")
    rec_id2 = queue.enqueue("authority_alignment", "strategy:coding", "reason", rec_id="fixed-id")
    assert rec_id == rec_id2
    assert queue.get_all_count() == 1


def test_recommendation_queue_by_strategy_id(tmp_path):
    store = _make_store(tmp_path)
    queue = LearningDemotionRecommendations(store._conn)
    queue.enqueue("authority_alignment", "strategy:coding", "low win rate")
    queue.enqueue("authority_alignment", "strategy:research", "high drift")
    assert queue.get_unprocessed_count("strategy:coding") == 1
    assert queue.get_unprocessed_count("strategy:research") == 1


def test_recommendation_queue_created_by_migration(tmp_path):
    store = _make_store(tmp_path)
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='learning_demotion_recommendations'"
    )
    assert cur.fetchone() is not None


def test_recommendation_queue_expire_removals(tmp_path):
    store = _make_store(tmp_path)
    queue = LearningDemotionRecommendations(store._conn)
    old_time = time.time() - (lc.DEMOTION_RECOMMENDATION_MAX_AGE_DAYS * 24 * 3600 + 10)
    queue.enqueue("authority_alignment", "strategy:coding", "old", created_at=old_time)
    removed = queue.expire_recommendations()
    assert removed == 1
    assert queue.get_all_count() == 0
