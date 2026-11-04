package update

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

func TestVerifyApplyRollback(t *testing.T) {
	dir := t.TempDir()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	newBin := filepath.Join(dir, "new")
	_ = os.WriteFile(newBin, []byte("v2"), 0o755)
	sig := filepath.Join(dir, "new.sig")
	_ = os.WriteFile(sig, []byte(base64.StdEncoding.EncodeToString(ed25519.Sign(priv, []byte("v2")))+"\n"), 0o644)
	key := base64.StdEncoding.EncodeToString(pub)
	if err := Verify(newBin, sig, key); err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(newBin, []byte("v2-tampered"), 0o755)
	if Verify(newBin, sig, key) == nil {
		t.Fatal("변조된 바이너리를 통과시켰다")
	}
	_ = os.WriteFile(newBin, []byte("v2"), 0o755)
	cur := filepath.Join(dir, "marine-agent")
	_ = os.WriteFile(cur, []byte("v1"), 0o755)
	if err := Apply(cur, newBin); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(cur); string(b) != "v2" {
		t.Fatal("적용 실패")
	}
	if err := Rollback(cur); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(cur); string(b) != "v1" {
		t.Fatal("되돌리기 실패")
	}
}
