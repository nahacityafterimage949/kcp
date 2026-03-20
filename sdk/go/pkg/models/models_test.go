package models_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/kcp-protocol/kcp/sdk/go/pkg/models"
)

// ─── NewArtifact ────────────────────────────────────────────────────────────

func TestNewArtifact_FieldsSet(t *testing.T) {
	a := models.NewArtifact("Test Title", "alice@example.com", "acme", "text")
	if a.Title != "Test Title" {
		t.Errorf("expected title 'Test Title', got '%s'", a.Title)
	}
	if a.UserID != "alice@example.com" {
		t.Errorf("expected user_id 'alice@example.com', got '%s'", a.UserID)
	}
	if a.TenantID != "acme" {
		t.Errorf("expected tenant_id 'acme', got '%s'", a.TenantID)
	}
	if a.Format != "text" {
		t.Errorf("expected format 'text', got '%s'", a.Format)
	}
}

func TestNewArtifact_IDIsUUID(t *testing.T) {
	a := models.NewArtifact("T", "u", "t", "text")
	if len(a.ID) != 36 {
		t.Errorf("expected UUID length 36, got %d", len(a.ID))
	}
	if !strings.Contains(a.ID, "-") {
		t.Error("UUID should contain hyphens")
	}
}

func TestNewArtifact_UniqueIDs(t *testing.T) {
	a1 := models.NewArtifact("T", "u", "t", "text")
	a2 := models.NewArtifact("T", "u", "t", "text")
	if a1.ID == a2.ID {
		t.Error("each artifact should have a unique ID")
	}
}

func TestNewArtifact_DefaultVisibility(t *testing.T) {
	a := models.NewArtifact("T", "u", "t", "text")
	if a.Visibility != "public" {
		t.Errorf("expected default visibility 'public', got '%s'", a.Visibility)
	}
}

func TestNewArtifact_DefaultVersion(t *testing.T) {
	a := models.NewArtifact("T", "u", "t", "text")
	if a.Version != "1" {
		t.Errorf("expected version '1', got '%s'", a.Version)
	}
}

func TestNewArtifact_TimestampSet(t *testing.T) {
	a := models.NewArtifact("T", "u", "t", "text")
	if a.Timestamp == "" {
		t.Error("timestamp should be set")
	}
	if !strings.Contains(a.Timestamp, "T") {
		t.Error("timestamp should be RFC3339 format")
	}
}

// ─── Lineage ────────────────────────────────────────────────────────────────

func TestLineage_Fields(t *testing.T) {
	l := &models.Lineage{
		Query:       "analyze churn",
		DataSources: []string{"postgres://db/customers"},
		Agent:       "analyst-v1",
	}
	if l.Query != "analyze churn" {
		t.Errorf("unexpected query: %s", l.Query)
	}
	if len(l.DataSources) != 1 {
		t.Errorf("expected 1 data source, got %d", len(l.DataSources))
	}
}

func TestLineage_JSONRoundtrip(t *testing.T) {
	l := &models.Lineage{
		Query:       "predict revenue",
		DataSources: []string{"s3://bucket/data"},
		Agent:       "ml-agent-v2",
	}
	data, err := json.Marshal(l)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}
	var l2 models.Lineage
	if err := json.Unmarshal(data, &l2); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}
	if l.Query != l2.Query {
		t.Errorf("query mismatch after roundtrip")
	}
}

// ─── ACL ────────────────────────────────────────────────────────────────────

func TestACL_Fields(t *testing.T) {
	acl := &models.ACL{
		AllowedTenants: []string{"acme"},
		AllowedUsers:   []string{"alice@example.com"},
		AllowedTeams:   []string{"data-science"},
	}
	if len(acl.AllowedTenants) != 1 {
		t.Error("expected 1 allowed tenant")
	}
	if len(acl.AllowedUsers) != 1 {
		t.Error("expected 1 allowed user")
	}
}

// ─── CanonicalJSON ───────────────────────────────────────────────────────────

func TestCanonicalJSON_NoSignature(t *testing.T) {
	a := models.NewArtifact("Test", "u", "t", "text")
	a.Signature = "ed25519:abc123"
	a.ContentHash = "sha256:def456"

	data, err := a.CanonicalJSON()
	if err != nil {
		t.Fatalf("CanonicalJSON failed: %v", err)
	}
	if strings.Contains(string(data), "signature") {
		t.Error("canonical JSON should NOT contain signature field")
	}
}

func TestCanonicalJSON_IsDeterministic(t *testing.T) {
	a := models.NewArtifact("Test", "alice", "acme", "text")
	a.ContentHash = "sha256:abc"
	a.Tags = []string{"tag1", "tag2"}

	d1, _ := a.CanonicalJSON()
	d2, _ := a.CanonicalJSON()
	if string(d1) != string(d2) {
		t.Error("CanonicalJSON should be deterministic")
	}
}

func TestCanonicalJSON_IsValidJSON(t *testing.T) {
	a := models.NewArtifact("Test", "u", "t", "text")
	a.ContentHash = "sha256:abc"
	data, err := a.CanonicalJSON()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var m map[string]interface{}
	if err := json.Unmarshal(data, &m); err != nil {
		t.Errorf("canonical JSON is not valid JSON: %v", err)
	}
}

func TestCanonicalJSON_KeysAreSorted(t *testing.T) {
	a := models.NewArtifact("Test", "alice", "acme", "text")
	a.ContentHash = "sha256:xyz"
	data, _ := a.CanonicalJSON()

	var m map[string]interface{}
	json.Unmarshal(data, &m)

	// title should come after tenant_id alphabetically
	raw := string(data)
	titleIdx := strings.Index(raw, `"title"`)
	tenantIdx := strings.Index(raw, `"tenant_id"`)
	if titleIdx < tenantIdx {
		t.Error("keys should be sorted alphabetically: tenant_id before title")
	}
}

// ─── JSON Serialization ──────────────────────────────────────────────────────

func TestArtifact_JSONFieldNames(t *testing.T) {
	a := models.NewArtifact("Test", "user", "tenant", "text")
	a.ContentHash = "sha256:abc"
	a.Tags = []string{"kcp", "test"}

	data, err := json.Marshal(a)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}
	raw := string(data)

	// Protocol fields must use snake_case
	for _, field := range []string{"user_id", "tenant_id", "content_hash"} {
		if !strings.Contains(raw, `"`+field+`"`) {
			t.Errorf("expected JSON field '%s' not found", field)
		}
	}
}

func TestSearchResult_Fields(t *testing.T) {
	sr := models.SearchResult{
		ID:        "uuid-1",
		Title:     "Result 1",
		Relevance: 0.95,
	}
	if sr.ID != "uuid-1" {
		t.Error("ID mismatch")
	}
	if sr.Relevance != 0.95 {
		t.Error("Relevance mismatch")
	}
}

func TestSearchResponse_Results(t *testing.T) {
	resp := models.SearchResponse{
		Results: []models.SearchResult{
			{ID: "1", Title: "A"},
			{ID: "2", Title: "B"},
		},
		Total:       2,
		QueryTimeMs: 3,
	}
	if resp.Total != 2 {
		t.Errorf("expected total 2, got %d", resp.Total)
	}
	if len(resp.Results) != 2 {
		t.Error("expected 2 results")
	}
}
