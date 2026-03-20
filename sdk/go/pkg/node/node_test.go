package node_test

import (
	"strings"
	"testing"

	"github.com/kcp-protocol/kcp/sdk/go/pkg/node"
)

// ─── helpers ────────────────────────────────────────────────────────────────

func newTestNode(t *testing.T) *node.KCPNode {
	t.Helper()
	dir := t.TempDir()
	cfg := node.Config{
		UserID:   "test-user",
		TenantID: "test-tenant",
		DBPath:   dir + "/kcp.db",
		KeysDir:  dir + "/keys",
	}
	n, err := node.New(cfg)
	if err != nil {
		t.Fatalf("failed to create node: %v", err)
	}
	t.Cleanup(func() { n.Close() })
	return n
}

// ─── Node Creation ───────────────────────────────────────────────────────────

func TestNew_CreatesNode(t *testing.T) {
	n := newTestNode(t)
	if n == nil {
		t.Fatal("node should not be nil")
	}
}

func TestNew_NodeIDSet(t *testing.T) {
	n := newTestNode(t)
	if n.NodeID() == "" {
		t.Error("node ID should be set")
	}
}

func TestNew_NodeIDIsPersistent(t *testing.T) {
	dir := t.TempDir()
	cfg := node.Config{
		UserID: "u", TenantID: "t",
		DBPath: dir + "/kcp.db", KeysDir: dir + "/keys",
	}
	n1, _ := node.New(cfg)
	id1 := n1.NodeID()
	n1.Close()

	n2, _ := node.New(cfg)
	id2 := n2.NodeID()
	n2.Close()

	if id1 != id2 {
		t.Error("node ID should persist across restarts")
	}
}

func TestNew_KeysGenerated(t *testing.T) {
	n := newTestNode(t)
	if len(n.PublicKey) == 0 {
		t.Error("public key should be set")
	}
	if len(n.PrivateKey) == 0 {
		t.Error("private key should be set")
	}
}

// ─── Publish ────────────────────────────────────────────────────────────────

func TestPublish_ReturnsArtifact(t *testing.T) {
	n := newTestNode(t)
	a, err := n.Publish("Test Artifact", []byte("hello kcp"), "text")
	if err != nil {
		t.Fatalf("Publish failed: %v", err)
	}
	if a == nil {
		t.Fatal("artifact should not be nil")
	}
}

func TestPublish_ArtifactHasID(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Test", []byte("content"), "text")
	if a.ID == "" {
		t.Error("artifact should have an ID")
	}
	if len(a.ID) != 36 {
		t.Errorf("artifact ID should be UUID (36 chars), got %d", len(a.ID))
	}
}

func TestPublish_ArtifactHasContentHash(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Test", []byte("content"), "text")
	if a.ContentHash == "" {
		t.Error("artifact should have a content hash")
	}
	if len(a.ContentHash) != 64 {
		t.Errorf("SHA-256 hex should be 64 chars, got %d", len(a.ContentHash))
	}
}

func TestPublish_ArtifactIsSigned(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Test", []byte("content"), "text")
	if a.Signature == "" {
		t.Error("artifact should have a signature")
	}
}

func TestPublish_WithTags(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Tagged", []byte("content"), "text",
		node.WithTags("kcp", "test", "go"))
	if len(a.Tags) != 3 {
		t.Errorf("expected 3 tags, got %d", len(a.Tags))
	}
}

func TestPublish_WithSummary(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Summarized", []byte("content"), "text",
		node.WithSummary("This is a summary"))
	if a.Summary != "This is a summary" {
		t.Errorf("unexpected summary: %s", a.Summary)
	}
}

func TestPublish_WithVisibility(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Private", []byte("content"), "text",
		node.WithVisibility("private"))
	if a.Visibility != "private" {
		t.Errorf("expected visibility 'private', got '%s'", a.Visibility)
	}
}

func TestPublish_DefaultVisibilityIsPublic(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Public", []byte("content"), "text")
	if a.Visibility != "public" {
		t.Errorf("expected default visibility 'public', got '%s'", a.Visibility)
	}
}

func TestPublish_UserAndTenantSet(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Test", []byte("content"), "text")
	if a.UserID != "test-user" {
		t.Errorf("expected user_id 'test-user', got '%s'", a.UserID)
	}
	if a.TenantID != "test-tenant" {
		t.Errorf("expected tenant_id 'test-tenant', got '%s'", a.TenantID)
	}
}

// ─── Get ────────────────────────────────────────────────────────────────────

func TestGet_ReturnsPublished(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Findable", []byte("data"), "text")

	found, err := n.Get(a.ID)
	if err != nil {
		t.Fatalf("Get failed: %v", err)
	}
	if found == nil {
		t.Fatal("artifact not found after publish")
	}
	if found.ID != a.ID {
		t.Errorf("ID mismatch: %s vs %s", found.ID, a.ID)
	}
}

func TestGet_NotFound(t *testing.T) {
	n := newTestNode(t)
	found, err := n.Get("non-existent-id")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if found != nil {
		t.Error("should return nil for missing artifact")
	}
}

func TestGetContent_ReturnsOriginal(t *testing.T) {
	n := newTestNode(t)
	content := []byte("original content bytes")
	a, _ := n.Publish("Content Test", content, "text")

	got, err := n.GetContent(a.ID)
	if err != nil {
		t.Fatalf("GetContent failed: %v", err)
	}
	if string(got) != string(content) {
		t.Errorf("content mismatch: got %q, want %q", got, content)
	}
}

// ─── Search ─────────────────────────────────────────────────────────────────

func TestSearch_FindsPublished(t *testing.T) {
	n := newTestNode(t)
	n.Publish("Customer Churn Analysis", []byte("churn data"), "text",
		node.WithTags("churn", "analytics"))

	resp, err := n.Search("churn", 10)
	if err != nil {
		t.Fatalf("Search failed: %v", err)
	}
	if resp.Total == 0 {
		t.Error("search should find the published artifact")
	}
}

func TestSearch_EmptyQuery(t *testing.T) {
	n := newTestNode(t)
	n.Publish("Some Artifact", []byte("data"), "text")

	resp, err := n.Search("", 10)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp == nil {
		t.Error("should return response even for empty query")
	}
}

func TestSearch_LimitRespected(t *testing.T) {
	n := newTestNode(t)
	for i := 0; i < 5; i++ {
		n.Publish("KCP Artifact", []byte("kcp content"), "text")
	}
	resp, _ := n.Search("KCP", 3)
	if len(resp.Results) > 3 {
		t.Errorf("expected at most 3 results, got %d", len(resp.Results))
	}
}

// ─── List ────────────────────────────────────────────────────────────────────

func TestList_ReturnsArtifacts(t *testing.T) {
	n := newTestNode(t)
	n.Publish("A1", []byte("c1"), "text")
	n.Publish("A2", []byte("c2"), "text")

	list, err := n.List(10, nil)
	if err != nil {
		t.Fatalf("List failed: %v", err)
	}
	if len(list) < 2 {
		t.Errorf("expected at least 2 artifacts, got %d", len(list))
	}
}

func TestList_FilterByTags(t *testing.T) {
	n := newTestNode(t)
	n.Publish("Tagged", []byte("c"), "text", node.WithTags("ml"))
	n.Publish("Untagged", []byte("c"), "text")

	list, _ := n.List(10, []string{"ml"})
	for _, a := range list {
		hasTag := false
		for _, tag := range a.Tags {
			if tag == "ml" {
				hasTag = true
				break
			}
		}
		if !hasTag {
			t.Errorf("artifact %s should have tag 'ml'", a.ID)
		}
	}
}

// ─── Lineage ─────────────────────────────────────────────────────────────────

func TestLineage_RootHasOneEntry(t *testing.T) {
	n := newTestNode(t)
	root, _ := n.Publish("Root", []byte("root"), "text")

	chain, err := n.Lineage(root.ID)
	if err != nil {
		t.Fatalf("Lineage failed: %v", err)
	}
	if len(chain) != 1 {
		t.Errorf("root artifact should have lineage of 1, got %d", len(chain))
	}
}

func TestLineage_DerivedChain(t *testing.T) {
	n := newTestNode(t)
	root, _ := n.Publish("Root", []byte("root content"), "text")
	derived, _ := n.Publish("Derived", []byte("derived content"), "text",
		node.WithDerivedFrom(root.ID))

	chain, err := n.Lineage(derived.ID)
	if err != nil {
		t.Fatalf("Lineage failed: %v", err)
	}
	if len(chain) != 2 {
		t.Errorf("derived artifact should have lineage of 2, got %d", len(chain))
	}
}

func TestLineage_ChainOrder(t *testing.T) {
	n := newTestNode(t)
	root, _ := n.Publish("Root", []byte("root"), "text")
	mid, _ := n.Publish("Middle", []byte("mid"), "text",
		node.WithDerivedFrom(root.ID))
	_, _ = n.Publish("Leaf", []byte("leaf"), "text",
		node.WithDerivedFrom(mid.ID))

	chain, _ := n.Lineage(mid.ID)
	if len(chain) < 2 {
		t.Errorf("expected chain length >= 2, got %d", len(chain))
	}
}

// ─── Delete ──────────────────────────────────────────────────────────────────

func TestDelete_RemovesArtifact(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("To Delete", []byte("bye"), "text")

	ok, err := n.Delete(a.ID)
	if err != nil {
		t.Fatalf("Delete failed: %v", err)
	}
	if !ok {
		t.Error("Delete should return true for existing artifact")
	}
}

func TestDelete_NotFoundReturnsFalse(t *testing.T) {
	n := newTestNode(t)
	ok, err := n.Delete("non-existent-id")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if ok {
		t.Error("Delete should return false for non-existent artifact")
	}
}

// ─── Stats ───────────────────────────────────────────────────────────────────

func TestStats_NodeIDSet(t *testing.T) {
	n := newTestNode(t)
	s := n.Stats()
	if s.NodeID == "" {
		t.Error("stats should include node_id")
	}
}

func TestStats_CountsArtifacts(t *testing.T) {
	n := newTestNode(t)
	n.Publish("A1", []byte("c"), "text")
	n.Publish("A2", []byte("c"), "text")

	s := n.Stats()
	if s.Artifacts < 2 {
		t.Errorf("expected at least 2 artifacts in stats, got %d", s.Artifacts)
	}
}

func TestStats_UserAndTenant(t *testing.T) {
	n := newTestNode(t)
	s := n.Stats()
	if s.UserID != "test-user" {
		t.Errorf("expected user_id 'test-user', got '%s'", s.UserID)
	}
	if s.TenantID != "test-tenant" {
		t.Errorf("expected tenant_id 'test-tenant', got '%s'", s.TenantID)
	}
}

// ─── Signature Verification ──────────────────────────────────────────────────

func TestPublish_SignatureIsHex(t *testing.T) {
	n := newTestNode(t)
	a, _ := n.Publish("Signed", []byte("content"), "text")
	validHex := "0123456789abcdef"
	for _, c := range strings.ToLower(a.Signature) {
		if !strings.ContainsRune(validHex, c) {
			t.Errorf("signature contains non-hex character: %c", c)
			break
		}
	}
}

func TestPublish_DifferentContentDifferentHash(t *testing.T) {
	n := newTestNode(t)
	a1, _ := n.Publish("T", []byte("content A"), "text")
	a2, _ := n.Publish("T", []byte("content B"), "text")
	if a1.ContentHash == a2.ContentHash {
		t.Error("different content should produce different hashes")
	}
}
