# RFC KCP-003: Adaptive Sync Engine + Hybrid Storage Architecture

**RFC Number:** KCP-003  
**Title:** Adaptive Peer Sync Engine and Hybrid Storage for Scalable KCP Nodes  
**Status:** Draft  
**Date:** March 2026  
**Author:** Thiago Silva  

---

## Abstract

This RFC defines:
1. An **adaptive async sync engine** — background sync that adjusts batch size, retry policy, and concurrency based on queue depth and connection quality.
2. A **delivery confirmation protocol** — every peer explicitly ACKs each artifact; sync is not considered complete until all required replicas confirm.
3. A **replication policy model** — configurable `replication_factor` with visibility-aware routing (public goes everywhere, private stays local).
4. A **hybrid storage architecture** — SQLite as the metadata index + filesystem shard tree for content, replacing the current single-file SQLite blob storage.

---

## 1. Motivation

### 1.1 Problems with the current approach

| Problem | Current state | Impact |
|---|---|---|
| Sync is fire-and-forget | `sync_push()` has no ACK | Data silently lost on network failure |
| Single peer | `KCP_PEER` = 1 URL | No redundancy, single point of failure |
| SQLite stores everything | metadata + content blobs in one file | Doesn't scale beyond ~10GB; WAL lock contention under concurrent writes |
| No retry | If push fails, artifact never reaches peer | Data loss |
| No progress tracking | No way to know what synced and what didn't | Operational blindness |

### 1.2 Design goals

- **Correctness first** — no artifact is marked "synced" until peer ACKs it
- **Adaptive** — small queue = immediate; large queue = batched background
- **Resilient** — exponential backoff retry, circuit breaker per peer
- **Scalable** — filesystem sharding handles unlimited artifact volume
- **Simple to deploy** — no extra services (no Redis, no Kafka); pure Python + SQLite + filesystem
- **Customizable** — government / air-gapped / private scenarios via policy config

---

## 2. Adaptive Sync Engine

### 2.1 Sync Queue

A persistent sync queue is added to the database:

```sql
CREATE TABLE kcp_sync_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    peer_url    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | in_flight | done | failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT,
    next_attempt TEXT,                             -- scheduled retry time
    acked_at    TEXT,                              -- peer confirmed receipt
    error       TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_sync_queue_status ON kcp_sync_queue(status, next_attempt);
CREATE INDEX idx_sync_queue_artifact ON kcp_sync_queue(artifact_id, peer_url);
```

When `publish()` is called with `visibility=public`, entries are immediately enqueued for every configured peer. The queue is **persistent** — if the process restarts, pending items resume.

### 2.2 Adaptive Batch Sizing

The sync worker adjusts batch size based on queue depth:

```
queue_depth  →  batch_size  →  interval
──────────────────────────────────────
1–10         →  1 (immediate, on publish)
11–100       →  10 per cycle, every 30s
101–1000     →  50 per cycle, every 60s
1001+        →  100 per cycle, every 120s
```

This means:
- **Normal use** (few artifacts): sync feels instant — no perceptible delay
- **Bulk import** (hundreds of files): batched in background, no UI blocking
- **Initial peer join** (thousands of artifacts): steady background migration

### 2.3 Retry with Exponential Backoff

```
attempt  →  next_attempt_delay
────────────────────────────────
1        →  30s
2        →  2min
3        →  10min
4        →  1h
5        →  6h
6+       →  24h (max)
```

After 7 failed attempts (≈ 2 days total), the item moves to `failed` status and triggers an alert (log warning + optional webhook).

### 2.4 Circuit Breaker per Peer

Each peer has a circuit state:

```
CLOSED → OPEN (after 3 consecutive failures) → HALF-OPEN (after 5min) → CLOSED
```

While a peer's circuit is OPEN, no new requests are sent. This prevents cascade failures when a peer is down.

### 2.5 Delivery Confirmation Protocol

The sync is considered **complete** only when the peer returns a confirmed ACK:

```
Node A                                    Node B (peer)
  │                                           │
  │  POST /kcp/v1/sync/push  {artifact}       │
  │──────────────────────────────────────────►│
  │                                           │  verify signature
  │                                           │  store artifact
  │                                           │  update FTS index
  │  ← 200 {"accepted": true, "id": "..."}   │
  │◄──────────────────────────────────────────│
  │                                           │
  │  mark sync_queue entry: status=done       │
  │  acked_at = now()                         │
```

`accepted: false` (e.g. duplicate, invalid signature) also marks as `done` — no retry needed.

Any non-200 response or network error triggers the retry schedule.

### 2.6 Sync Worker (background thread)

```python
class SyncWorker:
    """
    Background daemon thread — runs inside KCPNode process.
    Spawned lazily on first publish with visibility=public.
    Stops when node closes.
    """
    def run(self):
        while not self._stop_event.is_set():
            batch_size = self._adaptive_batch_size()
            items = self.store.dequeue_pending_sync(batch_size)
            for item in items:
                if self._circuit_open(item.peer_url):
                    continue
                self._push_with_retry(item)
            sleep(self._adaptive_interval())
```

The worker is a **daemon thread** — no extra processes, no Docker containers, no message broker. Zero operational overhead.

---

## 3. Replication Policy

### 3.1 Configuration

```python
# Simple (env vars)
KCP_PEERS=https://peer04.kcp-protocol.org,https://peer07.kcp-protocol.org
KCP_REPLICATION_FACTOR=2          # default: replicate to 2 peers
KCP_REPLICATION_POLICY=public     # default: only public artifacts

# Advanced (kcp_config table in SQLite)
replication.factor = 2
replication.policy = public        # public | org | all
replication.min_acks = 1           # consider synced after N acks (default: 1)
replication.max_peers = 5          # max concurrent peer pushes
```

### 3.2 Visibility-Aware Routing

```
visibility=public  → sync to ALL configured peers (up to replication_factor)
visibility=org     → sync only to peers with matching tenant_id
visibility=team    → sync only to peers in ACL.allowed_peers (future)
visibility=private → NEVER synced (stays local, encrypted)
```

### 3.3 Replication Factor Semantics

`replication_factor=N` means: "ensure N distinct peers have ACKed this artifact".

If only 2 peers are configured and `replication_factor=3`, the system syncs to all 2 available peers and logs a warning: "replication_factor=3 requested but only 2 peers available."

### 3.4 Use Case Presets

```python
# Public open-source project
KCP_PEERS=peer04,peer05,peer07
KCP_REPLICATION_FACTOR=3

# Private corporate (hub-only, no external peers)
KCP_PEERS=kcp-hub.acme.internal
KCP_REPLICATION_FACTOR=1
KCP_REPLICATION_POLICY=org

# Government / air-gapped (no external network)
KCP_PEERS=                          # empty — local only
KCP_REPLICATION_FACTOR=0
KCP_REPLICATION_POLICY=private

# High-availability (5 peers, minimum 3 acks)
KCP_PEERS=peer01,peer02,peer03,peer04,peer05
KCP_REPLICATION_FACTOR=5
KCP_REPLICATION_MIN_ACKS=3
```

---

## 4. Hybrid Storage Architecture

### 4.1 The Problem with SQLite Blobs

Current `kcp_content` table stores all artifact content as BLOBs:

```sql
CREATE TABLE kcp_content (
    content_hash TEXT PRIMARY KEY,
    content      BLOB NOT NULL,   -- ← entire file in SQLite row
    size_bytes   INTEGER
);
```

**Issues at scale:**
- SQLite WAL file grows unboundedly with large BLOBs
- Vacuum operations lock the DB (downtime)
- Memory pressure when reading large content
- No partial reads (must load entire BLOB)
- Backup = copy entire DB file (slow, large)

### 4.2 Proposed: SQLite Index + Filesystem Shard Tree

**Rule:** SQLite stores **only metadata**. Content lives on the **filesystem** in a sharded directory tree.

```
~/.kcp/
├── kcp.db                     ← SQLite: metadata, FTS, sync queue, audit (small, fast)
├── keys/
│   ├── private.key
│   └── public.key
└── content/                   ← Filesystem: all artifact content
    ├── 2026/
    │   ├── 03/
    │   │   ├── 21/
    │   │   │   ├── 2a3ecc3b0c328602b0392713aeeb685c86a6f4798aaf99010c4efe39e2b96308.bin
    │   │   │   └── b7b69e31b4a59c53ff86053ceb3cf304d31fcb4a173f4d9556ebed29df2b25a9.bin
    │   │   └── 22/
    │   │       └── ...
    │   └── 04/
    └── 2025/
        └── ...
```

**Sharding strategy:** `content/{year}/{month}/{day}/{sha256}.bin`

- **Date-based sharding** → easy archival ("move everything before 2025 to cold storage")
- **SHA-256 filename** → content-addressed (hash = filename = integrity check)
- **Flat within day** → no hotspot (all writes on same day go to same dir, but filesystems handle thousands of files per dir efficiently)

### 4.3 Content Path Calculation

```python
def content_path(self, content_hash: str, created_at: str) -> Path:
    """
    Returns the filesystem path for a content blob.
    
    content_hash: SHA-256 hex string
    created_at:   ISO 8601 timestamp
    
    Returns: ~/.kcp/content/2026/03/21/{hash}.bin
    """
    dt = datetime.fromisoformat(created_at)
    return (
        self.content_dir
        / str(dt.year)
        / f"{dt.month:02d}"
        / f"{dt.day:02d}"
        / f"{content_hash}.bin"
    )
```

### 4.4 Migration Path (backward compatible)

Existing installations (SQLite blobs) are migrated transparently on first boot:

```python
def _migrate_blobs_to_filesystem(self):
    """
    One-time migration: move BLOBs from kcp_content table to filesystem.
    Runs only if content/ directory doesn't exist yet.
    """
    if self.content_dir.exists():
        return  # already migrated
    
    rows = conn.execute("SELECT content_hash, content, size_bytes FROM kcp_content").fetchall()
    for row in rows:
        path = self.content_path(row["content_hash"], ...)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(row["content"])
    
    # Drop blob column (SQLite doesn't support DROP COLUMN natively in old versions)
    # Instead: rename table, recreate without blob, copy data
    conn.executescript("""
        ALTER TABLE kcp_content RENAME TO kcp_content_old;
        CREATE TABLE kcp_content (
            content_hash TEXT PRIMARY KEY,
            size_bytes   INTEGER NOT NULL,
            created_at   TEXT NOT NULL
        );
        INSERT INTO kcp_content SELECT content_hash, size_bytes, '' FROM kcp_content_old;
        DROP TABLE kcp_content_old;
    """)
```

### 4.5 Storage Tiers (future)

The filesystem approach enables tiered storage with zero protocol changes:

```
~/.kcp/content/2024/  →  cold storage (S3 Glacier, tape)
~/.kcp/content/2025/  →  warm storage (S3 Standard-IA)
~/.kcp/content/2026/  →  hot storage  (local SSD)
```

Retrieval transparently falls back through tiers.

### 4.6 Peer Storage (server deployment)

For peer nodes (like peer07.kcp-protocol.org), the same structure applies:

```
/dados/kcp/
├── data/
│   ├── kcp.db          ← metadata index (small, fast, backed up easily)
│   └── content/        ← sharded content tree
│       ├── 2026/
│       └── ...
└── sdk/python/
```

**Backup strategy:**
```bash
# Metadata (fast, small — do hourly)
sqlite3 /dados/kcp/data/kcp.db ".backup /backups/kcp-$(date +%Y%m%d-%H%M).db"

# Content (large — do daily, incremental with rsync)
rsync -av --checksum /dados/kcp/data/content/ /backups/content/
```

---

## 5. Sync State Visibility

### 5.1 New `kcp_stats` fields

```json
{
  "artifacts": 14,
  "sync_queue": {
    "pending": 3,
    "in_flight": 1,
    "done": 10,
    "failed": 0
  },
  "peers": {
    "peer07.kcp-protocol.org": {
      "status": "CLOSED",
      "last_sync": "2026-03-21T02:41:39Z",
      "acked": 10,
      "pending": 3
    }
  },
  "replication_factor": 2,
  "fully_replicated": 10,
  "partially_replicated": 3
}
```

### 5.2 New MCP tool: `kcp_sync_status`

```python
@mcp.tool()
def kcp_sync_status() -> dict:
    """Returns current sync queue state and peer replication status."""
    return node.sync_status()
```

---

## 6. Implementation Plan

### Phase 1 — Sync Queue + Delivery Confirmation (this sprint)
- [ ] Add `kcp_sync_queue` table to schema
- [ ] Update `publish()` to enqueue for all configured peers
- [ ] Implement `SyncWorker` daemon thread with adaptive batching
- [ ] Update `/kcp/v1/sync/push` to return explicit `accepted` + `artifact_id`
- [ ] Update `sync_push()` client to mark queue entries on ACK
- [ ] Exponential backoff retry
- [ ] Circuit breaker per peer
- [ ] Tests: queue persistence, retry, ACK handling, circuit breaker

### Phase 2 — Multi-Peer + Replication Policy
- [ ] `KCP_PEERS` env var (comma-separated list)
- [ ] `replication_factor` config
- [ ] Visibility-aware routing
- [ ] `kcp_sync_status` MCP tool
- [ ] Tests: multi-peer routing, replication factor enforcement

### Phase 3 — Hybrid Storage
- [ ] Content path calculation (`content/{year}/{month}/{day}/{hash}.bin`)
- [ ] Migration: blobs → filesystem (one-time, on boot)
- [ ] Update `publish()` to write to filesystem
- [ ] Update `get_content()` to read from filesystem
- [ ] Update sync: stream files instead of JSON BLOBs
- [ ] Peer storage config for server deployments
- [ ] Tests: filesystem read/write, migration, streaming sync

---

## 7. Non-Goals

- **No CRDT / conflict resolution** — KCP artifacts are immutable (append-only). No conflict is possible; only new versions via `derived_from`.
- **No consensus protocol** — no Raft, no Paxos. Sync is eventual consistency with delivery guarantees.
- **No message broker** — no Kafka, no Redis. Pure SQLite queue + daemon thread.
- **No blockchain** — content addressing via SHA-256 + Ed25519 signatures is sufficient.

---

## 8. Open Questions

1. **Content streaming for large files** — sync currently sends content as base64 in JSON. For files >1MB, should we use chunked transfer or multipart?
2. **Peer authentication** — currently only `X-KCP-Client` header. Should peers authenticate with Ed25519 keypairs?
3. **Delta sync** — if an artifact's content is updated (new version via `derived_from`), should we sync only the diff?
4. **Content deduplication** — same content hash across different artifacts should only be stored once on the filesystem.

---

*RFC KCP-003 — Draft. Feedback welcome: https://github.com/kcp-protocol/kcp/issues*
