# RFC KCP-002: MCP Bridge — KCP ↔ Model Context Protocol Integration

**RFC Number:** KCP-002  
**Title:** KCP-to-MCP Bridge — Bridging Persistent Knowledge with Ephemeral AI Context  
**Status:** Draft  
**Date:** March 2026  
**Author:** Thiago Silva  
**Inspired by:** Gemini 2.0 suggestion during LLM onboarding tests (2026-03-20)

---

## Abstract

This RFC proposes a **Bridge Mode** for KCP that allows bidirectional integration with the Model Context Protocol (MCP). KCP artifacts (persistent, signed, lineage-tracked) can be injected as ephemeral MCP context into AI tools (Claude, Cursor, Windsurf), and outputs produced during MCP sessions can be automatically persisted back as KCP artifacts.

---

## 1. Motivation

### 1.1 The Gap Today

MCP (Anthropic, 2024) and KCP (2026) are complementary but disconnected:

| Dimension | MCP | KCP |
|-----------|-----|-----|
| Lifespan | Session-bound (ephemeral) | Persistent (forever) |
| Direction | Tool → LLM (inject context) | LLM → Store (persist output) |
| Identity | No signature | Ed25519 signed |
| Lineage | None | Full DAG |
| Discovery | None | Full-text + semantic search |
| Governance | None | Multi-tenant ACL |

**Result:** An AI assistant using MCP can access tools and data in real-time, but the knowledge it produces disappears when the session ends — and it has no access to previously produced knowledge.

### 1.2 The Opportunity

By bridging the two protocols:

1. **KCP → MCP**: Past artifacts become available as MCP context. The LLM can "remember" what was learned before.
2. **MCP → KCP**: New outputs produced in MCP sessions are automatically published as KCP artifacts with full lineage.

```
┌────────────────────────────────────────────────┐
│  AI Assistant (Claude / Cursor / Windsurf)     │
│  ↕ MCP protocol (standard Anthropic)           │
├────────────────────────────────────────────────┤
│  KCP-MCP Bridge (this RFC)                     │
│  • Injects KCP artifacts as MCP context        │
│  • Captures MCP outputs → KCP artifacts        │
├────────────────────────────────────────────────┤
│  KCPNode (Python / Go SDK)                     │
│  • SQLite (local) / Hub / Federation           │
└────────────────────────────────────────────────┘
```

---

## 2. Protocol Changes

### 2.1 New `lineage` fields

Two new optional fields are added to the `lineage` object to track MCP session provenance:

```json
{
  "artifact_id": "uuid-v4",
  "lineage": {
    "query": "string",
    "data_sources": ["uri"],
    "agent": "string",
    "parent_id": "uuid",
    "mcp_session_id": "string (optional)",
    "mcp_tool_call":  "string (optional)"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `mcp_session_id` | `string` | Identifier of the MCP session that produced this artifact |
| `mcp_tool_call` | `string` | Name of the MCP tool call that triggered publication (e.g. `kcp_publish`) |

These fields are **optional** and backward-compatible. Existing artifacts without them remain valid.

### 2.2 New `format` values

```
"format": "html | json | markdown | pdf | png | mcp_context | mcp_tool_result"
```

| New Value | Meaning |
|-----------|---------|
| `mcp_context` | Artifact was originally an MCP context block injected into an LLM |
| `mcp_tool_result` | Artifact was produced as the result of an MCP tool call |

### 2.3 New operating mode: `bridge`

KCP currently defines 3 modes:
- **Local** — SQLite, no server
- **Hub** — PostgreSQL + S3, corporate
- **Federation** — Hub-to-hub P2P

This RFC adds a 4th:
- **Bridge** — KCPNode acts as an MCP server, exposing KCP tools to AI assistants

In Bridge mode, the KCPNode:
1. Listens for MCP tool calls (`kcp_publish`, `kcp_search`, `kcp_get`, `kcp_lineage`)
2. Executes them against the local SQLite (or Hub)
3. Returns MCP-compliant responses
4. Auto-publishes tool results as KCP artifacts when configured

---

## 3. Architecture

### 3.1 Flow: MCP Tool Call → KCP Artifact

```
User asks Claude: "Analyze rate limiting strategies and save your analysis"
     ↓
Claude calls MCP tool: kcp_publish(title=..., content=..., tags=[...])
     ↓
KCP-MCP Bridge receives the tool call
     ↓
Bridge calls KCPNode.publish(
    title=...,
    content=...,
    tags=[...],
    lineage={
        mcp_session_id: "session-abc123",
        mcp_tool_call:  "kcp_publish"
    }
)
     ↓
Returns artifact_id to Claude via MCP response
     ↓
Claude: "I've saved the analysis as artifact abc-123."
```

### 3.2 Flow: KCP Artifact → MCP Context

```
User asks Claude: "What do we know about rate limiting?"
     ↓
Claude calls MCP tool: kcp_search(query="rate limiting", limit=5)
     ↓
KCP-MCP Bridge calls KCPNode.search("rate limiting")
     ↓
Returns list of artifacts as MCP context blocks:
[
  {
    "type": "resource",
    "resource": {
      "uri": "kcp://artifact/abc-123",
      "mimeType": "text/markdown",
      "text": "## Rate Limiting Strategies\n\nToken Bucket..."
    }
  }
]
     ↓
Claude uses this as context to answer with full knowledge of past analyses
```

### 3.3 MCP Tools Exposed

| Tool | MCP Input | KCP Operation | MCP Output |
|------|-----------|---------------|------------|
| `kcp_publish` | `title`, `content`, `tags`, `derived_from?` | `KCPNode.publish()` | `artifact_id`, `content_hash` |
| `kcp_search` | `query`, `limit?`, `tags?` | `KCPNode.search()` | List of `SearchResult` |
| `kcp_get` | `artifact_id` | `KCPNode.get()` | Full artifact + content |
| `kcp_lineage` | `artifact_id` | `KCPNode.lineage()` | Lineage chain (DAG) |

### 3.4 MCP Resource: `kcp://` URI Scheme

This RFC also introduces a URI scheme for KCP artifacts:

```
kcp://<node_id>/artifact/<artifact_id>
kcp://<node_id>/search?q=<query>
kcp://local/artifact/abc-123          (local node)
kcp://hub.acme.com/artifact/abc-123   (hub node)
```

This enables direct referencing of KCP artifacts in MCP contexts and in lineage chains across federated nodes.

---

## 4. Implementation Plan

### Phase 1: MCP Server (Python)

```
mcp-server/
├── __init__.py
├── server.py          # FastMCP server
├── tools/
│   ├── publish.py     # kcp_publish tool
│   ├── search.py      # kcp_search tool
│   ├── get.py         # kcp_get tool
│   └── lineage.py     # kcp_lineage tool
├── bridge.py          # KCPNode ↔ MCP adapter
├── config.py          # Configuration
└── README.md
```

### Phase 2: KCP SDK Changes

- Add `mcp_session_id` and `mcp_tool_call` to `Lineage` model (Python + Go + TypeScript)
- Add `mcp_context` and `mcp_tool_result` to `format` enum
- Add `bridge` mode to `KCPNode` configuration

### Phase 3: Claude Desktop Integration

```json
// claude_desktop_config.json
{
  "mcpServers": {
    "kcp": {
      "command": "python",
      "args": ["-m", "kcp_mcp_server"],
      "env": {
        "KCP_USER_ID": "alice@example.com",
        "KCP_DB_PATH": "~/.kcp/kcp.db"
      }
    }
  }
}
```

---

## 5. Security Considerations

### 5.1 Signature on MCP-originated artifacts

All artifacts published via the Bridge MUST be signed with the node's Ed25519 key, identical to artifacts published directly via SDK. The `mcp_session_id` field does not affect signature validity.

### 5.2 MCP session isolation

Each MCP session SHOULD be treated as a separate `user_id` or tagged with the session ID in `lineage.mcp_session_id` to maintain auditability.

### 5.3 ACL enforcement

When `kcp_search` or `kcp_get` is called via MCP:
- The authenticated MCP client identity maps to a KCP `user_id`
- ACL rules are enforced identically to direct SDK calls
- Private artifacts are never returned unless the MCP client has explicit ACL access

---

## 6. Backward Compatibility

All changes are **additive and backward-compatible**:

- `mcp_session_id` and `mcp_tool_call` are optional in `lineage`
- `mcp_context` and `mcp_tool_result` are new values in an open enum — existing values remain valid
- `bridge` is a new mode — existing Local / Hub / Federation modes are unchanged
- The `kcp://` URI scheme is new — existing `https://` and `file://` URIs in `content_url` remain valid

---

## 7. Open Questions

1. **Auto-publish**: Should the Bridge automatically publish ALL MCP tool results as KCP artifacts, or only explicit `kcp_publish` calls?
2. **Session identity**: Should `mcp_session_id` be user-controlled or auto-generated by the Bridge?
3. **Bidirectional lineage**: When a KCP artifact is used as MCP context AND the LLM produces a derived artifact, should the lineage `parent_id` point to the original KCP artifact automatically?
4. **URI resolution**: How should `kcp://` URIs be resolved across federated nodes without a central registry? (See RFC KCP-003 for federation)

---

## 8. Related RFCs

- **RFC KCP-001** — Core protocol specification
- **RFC KCP-003** (planned) — Federation, CRDTs, Merkle DAG lineage proofs

---

## References

- [Model Context Protocol](https://modelcontextprotocol.io/) — Anthropic, 2024
- [KCP SPEC.md](../SPEC.md) — Knowledge Context Protocol v0.2
- [KCP ARCHITECTURE.md](../ARCHITECTURE.md) — Operating modes
- Gemini 2.0 suggestion: "KCP-to-MCP Bridge" — LLM onboarding test, 2026-03-20
