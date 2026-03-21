"""
Tests for KCP Filesystem Content Store — RFC KCP-003 Phase 3

Covers:
  - ContentStore write / read / exists / delete
  - Shard path structure: content/{year}/{month}/{day}/{hash}.bin
  - Atomic write (no corrupt files on crash)
  - Idempotent write (same hash → no overwrite)
  - Hash index: fast lookup after first scan
  - stats() returns correct file count and sizes
  - LocalStore integration: publish writes to filesystem
  - LocalStore.get_content reads from filesystem
  - _migrate_blobs_to_filesystem: copies legacy SQLite blobs on boot
  - Fallback: get_content reads SQLite blob if fs file missing
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kcp.content_store import ContentStore, _human_size
from kcp.store import LocalStore
from kcp.node import KCPNode


# ─── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def cs(tmp_path):
    return ContentStore(tmp_path)


@pytest.fixture
def store(tmp_path):
    return LocalStore(str(tmp_path / "kcp.db"))


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.delenv("KCP_PEERS", raising=False)
    return KCPNode(
        user_id="test@kcp.io",
        tenant_id="test",
        db_path=str(tmp_path / "kcp.db"),
        keys_dir=str(tmp_path / "keys"),
    )


# ─── ContentStore: basic ops ──────────────────────────────────

class TestContentStoreWrite:
    def test_write_creates_file(self, cs, tmp_path):
        data = b"hello KCP"
        content_hash = "abc123"
        path = cs.write(content_hash, data)
        assert path.exists()
        assert path.read_bytes() == data

    def test_write_returns_path(self, cs):
        path = cs.write("h1", b"data")
        assert isinstance(path, Path)

    def test_write_shard_structure(self, cs):
        ts = "2026-03-21T10:00:00+00:00"
        path = cs.write("deadbeef", b"x", timestamp=ts)
        parts = path.parts
        # Should contain .../content/2026/03/21/deadbeef.bin
        assert "content" in parts
        assert "2026" in parts
        assert "03" in parts
        assert "21" in parts
        assert path.name == "deadbeef.bin"

    def test_write_idempotent(self, cs):
        """Writing same hash twice must not overwrite (immutable content)."""
        cs.write("h1", b"original")
        cs.write("h1", b"different")  # should be silently skipped
        assert cs.read("h1") == b"original"

    def test_write_atomic_no_tmp_left_on_success(self, cs, tmp_path):
        cs.write("h2", b"data")
        tmp_files = list((tmp_path / "content").rglob("*.tmp"))
        assert len(tmp_files) == 0

    def test_write_default_timestamp_uses_today(self, cs):
        today = datetime.now(timezone.utc)
        path = cs.write("htoday", b"data")
        assert f"{today.year:04d}" in str(path)
        assert f"{today.month:02d}" in str(path)


class TestContentStoreRead:
    def test_read_existing(self, cs):
        cs.write("rh1", b"content bytes")
        assert cs.read("rh1") == b"content bytes"

    def test_read_missing_returns_none(self, cs):
        assert cs.read("nonexistent_hash") is None

    def test_read_uses_index_after_first_scan(self, cs, tmp_path):
        """After a scan, subsequent reads should use the index (no re-scan)."""
        cs.write("ih1", b"indexed")
        # Delete index to force scan
        index = tmp_path / "content" / ".index"
        if index.exists():
            index.unlink()
        # First read → scan (writes index)
        assert cs.read("ih1") == b"indexed"
        # Index should now exist
        assert index.exists()
        # Second read → uses index
        assert cs.read("ih1") == b"indexed"

    def test_read_large_content(self, cs):
        big = b"X" * 1_000_000  # 1 MB
        cs.write("big_hash", big)
        assert cs.read("big_hash") == big


class TestContentStoreExists:
    def test_exists_true(self, cs):
        cs.write("eh1", b"data")
        assert cs.exists("eh1") is True

    def test_exists_false(self, cs):
        assert cs.exists("nothere") is False


class TestContentStoreDelete:
    def test_delete_removes_file(self, cs, tmp_path):
        cs.write("dh1", b"to delete")
        assert cs.exists("dh1")
        result = cs.delete("dh1")
        assert result is True
        assert not cs.exists("dh1")

    def test_delete_missing_returns_false(self, cs):
        assert cs.delete("doesnotexist") is False

    def test_delete_removes_index_entry(self, cs):
        cs.write("dh2", b"data")
        cs.read("dh2")  # populate index
        cs.delete("dh2")
        assert cs._find_by_index("dh2") is None


class TestContentStoreStats:
    def test_stats_empty(self, cs):
        s = cs.stats()
        assert s["total_files"] == 0
        assert s["total_bytes"] == 0
        assert s["shard_count"] == 0

    def test_stats_after_writes(self, cs):
        cs.write("s1", b"hello")
        cs.write("s2", b"world!!")
        s = cs.stats()
        assert s["total_files"] == 2
        assert s["total_bytes"] == len(b"hello") + len(b"world!!")

    def test_stats_shard_count(self, cs):
        cs.write("ts1", b"a", timestamp="2026-01-01T00:00:00+00:00")
        cs.write("ts2", b"b", timestamp="2026-02-15T00:00:00+00:00")
        s = cs.stats()
        assert s["shard_count"] == 2  # different months = different dirs


class TestHumanSize:
    def test_bytes(self):
        assert _human_size(500) == "500.0 B"

    def test_kb(self):
        assert _human_size(2048) == "2.0 KB"

    def test_mb(self):
        assert _human_size(1024 * 1024) == "1.0 MB"


# ─── LocalStore Integration ────────────────────────────────────

class TestLocalStoreFilesystem:
    def test_publish_writes_to_filesystem(self, node, tmp_path):
        art = node.publish("Hello", "content bytes here")
        # ContentStore should have the file
        assert node.store.content_store.exists(art.content_hash)

    def test_get_content_reads_from_filesystem(self, node):
        art = node.publish("Doc", "readable content")
        content = node.get_content(art.id)
        assert content == b"readable content"

    def test_sqlite_kcp_content_has_no_blob(self, node, tmp_path):
        """After Phase 3, kcp_content rows should have empty blob."""
        art = node.publish("NoBlobDoc", "some text")
        row = node.store._conn.execute(
            "SELECT content FROM kcp_content WHERE content_hash = ?",
            (art.content_hash,),
        ).fetchone()
        assert row is not None
        # Blob should be empty (b'' or zero-length)
        blob = bytes(row["content"]) if row["content"] else b""
        assert blob == b""

    def test_content_persists_across_store_instances(self, tmp_path, monkeypatch):
        """Content written by one store is readable by a fresh instance."""
        monkeypatch.delenv("KCP_PEERS", raising=False)
        db = str(tmp_path / "kcp.db")
        keys = str(tmp_path / "keys")

        node1 = KCPNode(user_id="u", tenant_id="t", db_path=db, keys_dir=keys)
        art = node1.publish("Persistent", "stays here")
        content_hash = art.content_hash

        # New store instance pointing to same directory
        node2 = KCPNode(user_id="u", tenant_id="t", db_path=db, keys_dir=keys)
        content = node2.get_content(art.id)
        assert content == b"stays here"

    def test_stats_includes_filesystem_info(self, node):
        node.publish("A", "content A")
        node.publish("B", "content B")
        s = node.store.stats()
        assert "filesystem" in s
        assert s["filesystem"]["files"] >= 2
        assert s["filesystem"]["size_bytes"] > 0

    def test_fallback_to_sqlite_blob_if_fs_missing(self, node, tmp_path):
        """If the filesystem file is somehow deleted, fall back to SQLite blob."""
        art = node.publish("Fallback", "fallback content")

        # Manually put the blob back in SQLite (simulate pre-migration state)
        node.store._conn.execute(
            "UPDATE kcp_content SET content = ? WHERE content_hash = ?",
            (b"fallback content", art.content_hash),
        )
        node.store._conn.commit()

        # Remove the filesystem file
        node.store.content_store.delete(art.content_hash)

        # get_content should still work via SQLite fallback
        content = node.get_content(art.id)
        assert content == b"fallback content"


# ─── Migration ─────────────────────────────────────────────────

class TestBlobMigration:
    def test_migration_moves_blob_to_filesystem(self, tmp_path, monkeypatch):
        """Pre-Phase3 SQLite blob should be migrated to filesystem on init."""
        monkeypatch.delenv("KCP_PEERS", raising=False)
        db_path = tmp_path / "kcp.db"
        keys = tmp_path / "keys"

        # Step 1: create a store and publish (this writes to filesystem)
        node1 = KCPNode(user_id="u", tenant_id="t",
                        db_path=str(db_path), keys_dir=str(keys))
        art = node1.publish("MigTest", "migrate this")

        # Step 2: simulate pre-migration state — put blob back in SQLite,
        # delete the filesystem file
        node1.store._conn.execute(
            "UPDATE kcp_content SET content = ? WHERE content_hash = ?",
            (b"migrate this", art.content_hash),
        )
        node1.store._conn.commit()
        node1.store.content_store.delete(art.content_hash)
        assert not node1.store.content_store.exists(art.content_hash)

        # Step 3: create a NEW store instance — migration should fire
        node2 = KCPNode(user_id="u", tenant_id="t",
                        db_path=str(db_path), keys_dir=str(keys))

        # File should now be in filesystem
        assert node2.store.content_store.exists(art.content_hash)

        # SQLite blob should be cleared
        row = node2.store._conn.execute(
            "SELECT content FROM kcp_content WHERE content_hash = ?",
            (art.content_hash,),
        ).fetchone()
        blob = bytes(row["content"]) if row["content"] else b""
        assert blob == b""

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        """Running migration multiple times must not corrupt data."""
        monkeypatch.delenv("KCP_PEERS", raising=False)
        db = str(tmp_path / "kcp.db")
        keys = str(tmp_path / "keys")

        node = KCPNode(user_id="u", tenant_id="t", db_path=db, keys_dir=keys)
        art = node.publish("Idem", "idempotent")

        # Call migration twice
        conn = node.store._conn
        node.store._migrate_blobs_to_filesystem(conn)
        node.store._migrate_blobs_to_filesystem(conn)

        # Content still readable
        assert node.get_content(art.id) == b"idempotent"

    def test_migration_skips_empty_blobs(self, tmp_path, monkeypatch):
        """Rows with empty blob (already migrated) must be skipped silently."""
        monkeypatch.delenv("KCP_PEERS", raising=False)
        db = str(tmp_path / "kcp.db")
        keys = str(tmp_path / "keys")

        node = KCPNode(user_id="u", tenant_id="t", db_path=db, keys_dir=keys)
        art = node.publish("Skip", "skipped")

        # Blob already empty (as set by publish)
        conn = node.store._conn
        row = conn.execute(
            "SELECT content FROM kcp_content WHERE content_hash = ?",
            (art.content_hash,),
        ).fetchone()
        blob = bytes(row["content"]) if row["content"] else b""
        assert blob == b""

        # Migration should run without error
        node.store._migrate_blobs_to_filesystem(conn)
        assert node.get_content(art.id) == b"skipped"
