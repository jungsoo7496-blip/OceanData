//go:build !windows

package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestUnixCredentialsRequire0600(t *testing.T) {
	dir := t.TempDir()
	if err := SaveCredentials(dir, testCredentials()); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, "credentials.json")
	st, err := os.Stat(p)
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatal("자격증명 파일은 0600이어야 합니다", err)
	}
	if err := os.Chmod(p, 0o644); err != nil {
		t.Fatal(err)
	}
	if cr, err := LoadCredentials(dir); err == nil || cr != nil {
		t.Fatal("다른 사용자가 읽을 수 있는 자격증명은 거부해야 합니다")
	}
}
