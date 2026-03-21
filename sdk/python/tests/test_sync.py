"""
Tests for KCP Sync Engine — RFC KCP-003 Phase 1

Covers:
  - kcp_sync_queue table via store methods
  - enqueue / dequeue / ack / nack
  - Exponential backoff scheduling
  - max_attempts → status=failed
  - CircuitBreaker state transitions
  - SyncWorker adaptive batch sizing
  - SyncWorker integration (mocked HTTP)
  - KCPNode sync_status()
  - KCPNode enqueues on publish when peers configured
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

from kcp.store import LocalStore
from kcp.sync_worker import CircuitBreaker, SyncWorker, _adaptive_params
from kcp.node import KCPNode


# ─── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path):
    db = str(tmp_path / "test.db")
    return LocalStore(db)


@pytest.fixture
def tmp_node(tmp_path, monkeypatch):
    """Node with no peers (default)."""
    monkeypatch.delenv("KCP_PEERS", raising=False)
    return KCPNode(
        user_id="test@example.com",
        tenant_id="test-corp",
        db_path=str(tmp_path / "kcp.db"),
        keys_dir=str(tmp_path / "keys"),
    )


@pytest.fixture
def tmp_node_with_peers(tmp_path, monkeypatch):
    """Node with fake peers configured."""
    monkeypatch.setenv("KCP_PEERS", "http://peer1:8800,http://peer2:8800")
    node = KCPNode(
        user_id="test@example.com",
        tenant_id="test-corp",
        db_path=str(tmp_path / "kcp.db"),
        keys_dir=str(tmp_path / "keys"),
    )
    # Stop the background thread immediately — we test sync via store directly
    if node._sync_worker:
        node._sync_worker.stop()
    return node


# ─── Store Queue Tests ─────────────────────────────────────────

class TestEnqueueSync:
    def test_enqueue_single_peer(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        # Manually enqueue (node has no peers configured)
        count = tmp_store.enqueue_sync(art.id, ["http://peer1:8800"])
        assert count == 1

    def test_enqueue_multiple_peers(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        peers = ["http://p1:8800", "http://p2:8800", "http://p3:8800"]
        count = tmp_store.enqueue_sync(art.id, peers)
        assert count == 3

    def test_enqueue_idempotent(self, tmp_store, tmp_node):
        """Duplicate enqueue (same artifact + peer) is silently ignored."""
        art = tmp_node.publish("T", "content")
        first = tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        second = tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        assert first == 1
        assert second == 0  # already exists

    def test_enqueue_empty_peers(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        count = tmp_store.enqueue_sync(art.id, [])
        assert count == 0


class TestDequeueSync:
    def test_dequeue_returns_pending(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(batch_size=10)
        assert len(items) == 1
        assert items[0]["artifact_id"] == art.id
        assert items[0]["peer_url"] == "http://p1:8800"

    def test_dequeue_marks_in_flight(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(batch_size=10)
        assert len(items) == 1

        # Second dequeue should return nothing (already in_flight)
        items2 = tmp_store.dequeue_pending_sync(batch_size=10)
        assert len(items2) == 0

    def test_dequeue_respects_batch_size(self, tmp_store, tmp_node):
        peers = [f"http://p{i}:8800" for i in range(10)]
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, peers)
        items = tmp_store.dequeue_pending_sync(batch_size=3)
        assert len(items) == 3

    def test_dequeue_respects_next_attempt(self, tmp_store, tmp_node):
        """Items scheduled for the future should NOT be dequeued."""
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])

        items = tmp_store.dequeue_pending_sync(batch_size=10)
        assert len(items) == 1

        # nack to schedule retry in the future
        tmp_store.nack_sync(items[0]["id"], "test error")

        # Dequeue again — should return nothing (scheduled in future)
        items2 = tmp_store.dequeue_pending_sync(batch_size=10)
        assert len(items2) == 0


class TestAckSync:
    def test_ack_marks_done(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(10)
        qid = items[0]["id"]

        tmp_store.ack_sync(qid)

        stats = tmp_store.sync_queue_stats()
        assert stats["http://p1:8800"]["done"] == 1

    def test_acked_items_not_requeued(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(10)
        tmp_store.ack_sync(items[0]["id"])

        # Nothing pending or in_flight
        more = tmp_store.dequeue_pending_sync(10)
        assert len(more) == 0


class TestNackSync:
    def test_nack_increments_attempts(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(10)
        qid = items[0]["id"]

        tmp_store.nack_sync(qid, "error msg")

        stats = tmp_store.sync_queue_stats()
        # After 1 nack with attempts=1, still pending (not yet failed)
        assert stats["http://p1:8800"]["pending"] == 1
        assert stats["http://p1:8800"]["failed"] == 0

    def test_nack_max_attempts_marks_failed(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])

        # Cycle through max_attempts nacks
        for _ in range(7):
            # Force next_attempt to be in the past by setting it directly
            tmp_store._conn.execute(
                "UPDATE kcp_sync_queue SET next_attempt = '2000-01-01T00:00:00+00:00' "
                "WHERE status IN ('pending', 'in_flight')"
            )
            tmp_store._conn.commit()
            items = tmp_store.dequeue_pending_sync(10)
            if not items:
                break
            tmp_store.nack_sync(items[0]["id"], "persistent error", max_attempts=7)

        stats = tmp_store.sync_queue_stats()
        assert stats["http://p1:8800"]["failed"] == 1

    def test_exponential_backoff_increases(self, tmp_store, tmp_node):
        """Each nack should schedule retry further in the future."""
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])

        delays = []
        for attempt in range(4):
            # Reset to pending for next dequeue
            tmp_store._conn.execute(
                "UPDATE kcp_sync_queue SET next_attempt = '2000-01-01T00:00:00+00:00', status='pending' "
                "WHERE status IN ('pending', 'in_flight', 'failed')"
            )
            tmp_store._conn.commit()
            items = tmp_store.dequeue_pending_sync(10)
            if not items:
                break
            before = datetime.now(timezone.utc)
            tmp_store.nack_sync(items[0]["id"], "error", max_attempts=10)
            row = tmp_store._conn.execute(
                "SELECT next_attempt FROM kcp_sync_queue WHERE id = ?",
                (items[0]["id"],),
            ).fetchone()
            if row and row[0]:
                next_at = datetime.fromisoformat(row[0])
                delay = (next_at - before).total_seconds()
                delays.append(delay)

        # Each retry should be longer than the previous
        for i in range(1, len(delays)):
            assert delays[i] > delays[i - 1], (
                f"Expected delay[{i}]={delays[i]:.1f}s > delay[{i-1}]={delays[i-1]:.1f}s"
            )


class TestSyncQueueStats:
    def test_stats_empty(self, tmp_store):
        stats = tmp_store.sync_queue_stats()
        assert stats == {}

    def test_stats_multiple_peers(self, tmp_store, tmp_node):
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800", "http://p2:8800"])
        stats = tmp_store.sync_queue_stats()
        assert "http://p1:8800" in stats
        assert "http://p2:8800" in stats
        assert stats["http://p1:8800"]["pending"] == 1
        assert stats["http://p2:8800"]["pending"] == 1

    def test_stats_mixed_statuses(self, tmp_store, tmp_node):
        art1 = tmp_node.publish("A1", "content")
        art2 = tmp_node.publish("A2", "content")
        tmp_store.enqueue_sync(art1.id, ["http://p1:8800"])
        tmp_store.enqueue_sync(art2.id, ["http://p1:8800"])

        # Dequeue and ACK one
        items = tmp_store.dequeue_pending_sync(1)
        tmp_store.ack_sync(items[0]["id"])

        stats = tmp_store.sync_queue_stats()
        peer = stats["http://p1:8800"]
        assert peer["done"] == 1
        assert peer["pending"] + peer["in_flight"] == 1


# ─── CircuitBreaker Tests ──────────────────────────────────────

class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker("http://peer:8800")
        assert cb.state == "CLOSED"
        assert not cb.is_open

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("http://peer:8800")
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.is_open

    def test_does_not_open_before_threshold(self):
        cb = CircuitBreaker("http://peer:8800")
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "CLOSED"

    def test_success_resets_failures(self):
        cb = CircuitBreaker("http://peer:8800")
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "CLOSED"
        assert cb._failures == 0

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker("http://peer:8800")
        cb.recovery_timeout = 0  # Immediate recovery for testing
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "OPEN"

        # After recovery timeout, is_open should return False and state = HALF_OPEN
        time.sleep(0.01)
        assert not cb.is_open
        assert cb.state == "HALF_OPEN"

    def test_success_from_half_open_closes(self):
        cb = CircuitBreaker("http://peer:8800")
        cb.recovery_timeout = 0
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.01)
        _ = cb.is_open  # triggers HALF_OPEN
        cb.record_success()
        assert cb.state == "CLOSED"


# ─── Adaptive Batch Sizing Tests ──────────────────────────────

class TestAdaptiveBatchParams:
    def test_small_queue(self):
        batch, interval = _adaptive_params(5)
        assert batch == 1
        assert interval == 5

    def test_medium_queue(self):
        batch, interval = _adaptive_params(50)
        assert batch == 10
        assert interval == 30

    def test_large_queue(self):
        batch, interval = _adaptive_params(500)
        assert batch == 50
        assert interval == 60

    def test_very_large_queue(self):
        batch, interval = _adaptive_params(5000)
        assert batch == 100
        assert interval == 120

    def test_exact_boundary(self):
        batch, _ = _adaptive_params(10)
        assert batch == 1
        batch2, _ = _adaptive_params(11)
        assert batch2 == 10


# ─── SyncWorker Tests ──────────────────────────────────────────

class TestSyncWorker:
    def test_start_stop(self, tmp_store):
        worker = SyncWorker(tmp_store, ["http://p1:8800"])
        worker.start()
        assert worker._thread.is_alive()
        worker.stop(timeout=2)

    def test_no_peers_can_still_start(self, tmp_store):
        worker = SyncWorker(tmp_store, [])
        worker.start()
        assert worker._thread.is_alive()
        worker.stop(timeout=2)

    def test_status_returns_dict(self, tmp_store):
        worker = SyncWorker(tmp_store, ["http://p1:8800"])
        status = worker.status()
        assert "running" in status
        assert "peers" in status

    def test_add_peer(self, tmp_store):
        worker = SyncWorker(tmp_store, ["http://p1:8800"])
        worker.add_peer("http://p2:8800")
        assert "http://p2:8800" in worker.peer_urls
        assert "http://p2:8800" in worker._circuits

    def test_push_with_ack_on_success(self, tmp_store, tmp_node):
        """Successful HTTP push → ack_sync called."""
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(10)

        worker = SyncWorker(tmp_store, ["http://p1:8800"])

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"accepted": True, "id": art.id}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(worker._session, "post", return_value=mock_resp):
            worker._push_one(items[0])

        stats = tmp_store.sync_queue_stats()
        assert stats["http://p1:8800"]["done"] == 1

    def test_push_records_failure_on_connection_error(self, tmp_path, monkeypatch):
        """Connection error → nack_sync called, circuit records failure."""
        import requests as req
        monkeypatch.delenv("KCP_PEERS", raising=False)
        node = KCPNode(
            user_id="t@t.com", tenant_id="t",
            db_path=str(tmp_path / "n.db"),
            keys_dir=str(tmp_path / "k"),
        )
        art = node.publish("T", "content")
        node.store.enqueue_sync(art.id, ["http://p1:8800"])
        items = node.store.dequeue_pending_sync(10)

        worker = SyncWorker(node.store, ["http://p1:8800"])

        with patch.object(
            worker._session, "post",
            side_effect=req.exceptions.ConnectionError("refused"),
        ):
            worker._push_one(items[0])

        # Item was nacked → scheduled for future retry
        row = node.store._conn.execute(
            "SELECT status, attempts, error FROM kcp_sync_queue WHERE id = ?",
            (items[0]["id"],),
        ).fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == 1
        assert "refused" in (row[2] or "")
        assert worker._circuits["http://p1:8800"]._failures == 1

    def test_circuit_open_skips_push(self, tmp_store, tmp_node):
        """Open circuit → item put back without making HTTP request."""
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(10)

        worker = SyncWorker(tmp_store, ["http://p1:8800"])
        # Force circuit open
        for _ in range(3):
            worker._circuits["http://p1:8800"].record_failure()
        worker._circuits["http://p1:8800"].recovery_timeout = 9999

        # The tick should skip the item because circuit is open
        with patch.object(worker._session, "post") as mock_post:
            worker._tick()
            mock_post.assert_not_called()

    def test_409_conflict_treated_as_ack(self, tmp_store, tmp_node):
        """HTTP 409 Conflict → already exists → treated as ACK (no retry)."""
        import requests as req
        art = tmp_node.publish("T", "content")
        tmp_store.enqueue_sync(art.id, ["http://p1:8800"])
        items = tmp_store.dequeue_pending_sync(10)

        worker = SyncWorker(tmp_store, ["http://p1:8800"])

        mock_resp = MagicMock()
        mock_resp.status_code = 409
        http_error = req.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error

        with patch.object(worker._session, "post", return_value=mock_resp):
            worker._push_one(items[0])

        stats = tmp_store.sync_queue_stats()
        assert stats["http://p1:8800"]["done"] == 1


# ─── KCPNode Integration Tests ────────────────────────────────

class TestKCPNodeSyncIntegration:
    def test_no_peers_sync_worker_is_none(self, tmp_node):
        assert tmp_node._sync_worker is None
        assert tmp_node.peers == []

    def test_peers_parsed_from_env(self, tmp_node_with_peers):
        node = tmp_node_with_peers
        assert "http://peer1:8800" in node.peers
        assert "http://peer2:8800" in node.peers

    def test_sync_worker_started_when_peers_configured(self, tmp_node_with_peers):
        node = tmp_node_with_peers
        # Worker was stopped in fixture, but it was created
        assert node._sync_worker is not None

    def test_publish_enqueues_public_artifact(self, tmp_node_with_peers):
        node = tmp_node_with_peers
        art = node.publish("Public Doc", "public content", visibility="public")
        stats = node.store.sync_queue_stats()
        # Should have entries for both peers
        total_pending = sum(v.get("pending", 0) for v in stats.values())
        assert total_pending == 2  # 2 peers

    def test_publish_does_not_enqueue_private_artifact(self, tmp_node_with_peers):
        node = tmp_node_with_peers
        art = node.publish("Private Doc", "secret", visibility="private")
        stats = node.store.sync_queue_stats()
        total = sum(
            v.get("pending", 0) + v.get("done", 0)
            for v in stats.values()
        )
        assert total == 0

    def test_sync_status_no_workers(self, tmp_node):
        status = tmp_node.sync_status()
        assert status["running"] is False
        assert status["peers"] == {}

    def test_sync_status_with_worker(self, tmp_node_with_peers):
        node = tmp_node_with_peers
        status = node.sync_status()
        # Worker was stopped, so running=False
        assert "peers" in status
        assert isinstance(status["peers"], dict)

    def test_close_stops_worker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KCP_PEERS", "http://peer1:8800")
        node = KCPNode(
            user_id="test@example.com",
            tenant_id="test-corp",
            db_path=str(tmp_path / "kcp2.db"),
            keys_dir=str(tmp_path / "keys2"),
        )
        assert node._sync_worker is not None
        assert node._sync_worker._thread.is_alive()
        node.close()
        node._sync_worker._thread.join(timeout=2)
        assert not node._sync_worker._thread.is_alive()
