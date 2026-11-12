//go:build !windows

package update

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestRollbackInstallsVerifiedBytesInsteadOfBackupLink(t *testing.T) {
	dir := t.TempDir()
	current := filepath.Join(dir, "marine-agent")
	backup := filepath.Join(dir, ".marine-agent.previous-synthetic")
	target := filepath.Join(dir, "mutable-backup-target")
	previous := []byte("synthetic-previous-verified-binary")
	if err := os.WriteFile(current, []byte("synthetic-current-binary"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, previous, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, backup); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(previousPointer(current), []byte(filepath.Base(backup)+"\n"+digest(previous)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Rollback(current); err != nil {
		t.Fatal(err)
	}
	if st, err := os.Lstat(current); err != nil || !st.Mode().IsRegular() {
		t.Fatal("복원된 실행 파일이 변경 가능한 링크여서는 안 됩니다", err)
	}
	if err := os.WriteFile(target, []byte("synthetic-unverified-changed-binary"), 0o700); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(current)
	if err != nil || !bytes.Equal(got, previous) {
		t.Fatal("백업 원본이 바뀌어도 복원한 검증 바이트는 변하지 않아야 합니다", err)
	}
}
