package crypto_test

import (
	"strings"
	"testing"

	"github.com/kcp-protocol/kcp/sdk/go/pkg/crypto"
)

// ─── GenerateKeypair ────────────────────────────────────────────────────────

func TestGenerateKeypair_ReturnsKeys(t *testing.T) {
	priv, pub, err := crypto.GenerateKeypair()
	if err != nil {
		t.Fatalf("GenerateKeypair failed: %v", err)
	}
	if len(priv) == 0 {
		t.Error("private key is empty")
	}
	if len(pub) == 0 {
		t.Error("public key is empty")
	}
}

func TestGenerateKeypair_Unique(t *testing.T) {
	_, pub1, _ := crypto.GenerateKeypair()
	_, pub2, _ := crypto.GenerateKeypair()
	if string(pub1) == string(pub2) {
		t.Error("two keypairs should be unique")
	}
}

func TestGenerateKeypair_CorrectLengths(t *testing.T) {
	priv, pub, err := crypto.GenerateKeypair()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Ed25519: private key = 64 bytes, public key = 32 bytes
	if len(priv) != 64 {
		t.Errorf("expected private key len 64, got %d", len(priv))
	}
	if len(pub) != 32 {
		t.Errorf("expected public key len 32, got %d", len(pub))
	}
}

// ─── Sign / Verify ──────────────────────────────────────────────────────────

func TestSignAndVerify_Valid(t *testing.T) {
	priv, pub, _ := crypto.GenerateKeypair()
	data := []byte(`{"artifact_id":"test","content":"hello"}`)

	sig := crypto.Sign(data, priv)
	if sig == "" {
		t.Fatal("signature is empty")
	}
	if !crypto.Verify(data, sig, pub) {
		t.Error("valid signature should verify")
	}
}

func TestVerify_TamperedData(t *testing.T) {
	priv, pub, _ := crypto.GenerateKeypair()
	original := []byte(`{"artifact_id":"abc","content":"original"}`)
	tampered := []byte(`{"artifact_id":"abc","content":"TAMPERED"}`)

	sig := crypto.Sign(original, priv)
	if crypto.Verify(tampered, sig, pub) {
		t.Error("tampered data should NOT verify")
	}
}

func TestVerify_WrongKey(t *testing.T) {
	priv, _, _ := crypto.GenerateKeypair()
	_, pub2, _ := crypto.GenerateKeypair()
	data := []byte(`{"artifact_id":"test"}`)

	sig := crypto.Sign(data, priv)
	if crypto.Verify(data, sig, pub2) {
		t.Error("signature from different key should NOT verify")
	}
}

func TestVerify_InvalidHex(t *testing.T) {
	_, pub, _ := crypto.GenerateKeypair()
	if crypto.Verify([]byte("data"), "not-valid-hex!!", pub) {
		t.Error("invalid hex signature should return false")
	}
}

func TestSign_IsDeterministicForSameInput(t *testing.T) {
	priv, _, _ := crypto.GenerateKeypair()
	data := []byte(`{"artifact_id":"same"}`)
	sig1 := crypto.Sign(data, priv)
	sig2 := crypto.Sign(data, priv)
	if sig1 != sig2 {
		t.Error("Ed25519 signatures for same data+key should be identical")
	}
}

func TestSign_IsHexEncoded(t *testing.T) {
	priv, _, _ := crypto.GenerateKeypair()
	sig := crypto.Sign([]byte("test"), priv)
	validHex := "0123456789abcdef"
	for _, c := range strings.ToLower(sig) {
		if !strings.ContainsRune(validHex, c) {
			t.Errorf("signature contains non-hex character: %c", c)
			break
		}
	}
}

// ─── HashContent ────────────────────────────────────────────────────────────

func TestHashContent_NonEmpty(t *testing.T) {
	h := crypto.HashContent([]byte("hello kcp"))
	if h == "" {
		t.Error("hash should not be empty")
	}
}

func TestHashContent_Deterministic(t *testing.T) {
	data := []byte("same content")
	if crypto.HashContent(data) != crypto.HashContent(data) {
		t.Error("hash should be deterministic")
	}
}

func TestHashContent_DifferentInputs(t *testing.T) {
	h1 := crypto.HashContent([]byte("content A"))
	h2 := crypto.HashContent([]byte("content B"))
	if h1 == h2 {
		t.Error("different content should produce different hashes")
	}
}

func TestHashContent_KnownValue(t *testing.T) {
	// SHA-256 of empty string
	h := crypto.HashContent([]byte(""))
	expected := "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	if h != expected {
		t.Errorf("expected %s, got %s", expected, h)
	}
}

func TestHashContent_IsHex64Chars(t *testing.T) {
	h := crypto.HashContent([]byte("kcp protocol"))
	if len(h) != 64 {
		t.Errorf("SHA-256 hex should be 64 chars, got %d", len(h))
	}
}

// ─── SaveKeys / LoadKeys ────────────────────────────────────────────────────

func TestSaveAndLoadKeys(t *testing.T) {
	dir := t.TempDir()
	priv, pub, _ := crypto.GenerateKeypair()

	if err := crypto.SaveKeys(dir, priv, pub); err != nil {
		t.Fatalf("SaveKeys failed: %v", err)
	}

	loadedPriv, loadedPub, err := crypto.LoadKeys(dir)
	if err != nil {
		t.Fatalf("LoadKeys failed: %v", err)
	}

	if string(priv) != string(loadedPriv) {
		t.Error("loaded private key does not match saved")
	}
	if string(pub) != string(loadedPub) {
		t.Error("loaded public key does not match saved")
	}
}

func TestLoadOrGenerateKeys_CreatesIfMissing(t *testing.T) {
	dir := t.TempDir()
	priv, pub, err := crypto.LoadOrGenerateKeys(dir)
	if err != nil {
		t.Fatalf("LoadOrGenerateKeys failed: %v", err)
	}
	if len(priv) == 0 || len(pub) == 0 {
		t.Error("should have generated keys")
	}
}

func TestLoadOrGenerateKeys_Idempotent(t *testing.T) {
	dir := t.TempDir()
	_, pub1, _ := crypto.LoadOrGenerateKeys(dir)
	_, pub2, _ := crypto.LoadOrGenerateKeys(dir)
	if string(pub1) != string(pub2) {
		t.Error("LoadOrGenerateKeys should return same key on second call")
	}
}
