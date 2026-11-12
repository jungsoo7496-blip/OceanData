package update

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestRollbackHashMismatchLeavesCurrentBinaryUntouched(t *testing.T) {
	dir := t.TempDir()
	current := filepath.Join(dir, "marine-agent")
	backup := filepath.Join(dir, ".marine-agent.previous-synthetic")
	currentData, previousData := []byte("synthetic-current-binary"), []byte("synthetic-previous-binary")
	if err := os.WriteFile(current, currentData, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(backup, []byte("synthetic-corrupt-backup"), 0o700); err != nil {
		t.Fatal(err)
	}
	pointer := previousPointer(current)
	if err := os.WriteFile(pointer, []byte(filepath.Base(backup)+"\n"+digest(previousData)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Rollback(current); err == nil {
		t.Fatal("기록한 해시와 다른 백업을 복원하면 안 됩니다")
	}
	got, err := os.ReadFile(current)
	if err != nil || !bytes.Equal(got, currentData) {
		t.Fatal("복원 거부 시 현재 실행 파일이 바뀌었습니다", err)
	}
	if _, err := os.Stat(pointer); err != nil {
		t.Fatal("복원 실패 시 복구 정보를 보존해야 합니다", err)
	}
}
