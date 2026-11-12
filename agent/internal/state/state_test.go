package state

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"
)

func TestQueueOrderAndLifecycle(t *testing.T) {
	ctx := context.Background()
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close() }()
	for _, f := range []File{
		{RelPath: "a/general.mp4", RuleID: "r", SourceDeviceID: "dev_a", Size: 10, MtimeNs: 1, Priority: 2},
		{RelPath: "a/clip.mp4", RuleID: "r", SourceDeviceID: "dev_a", Size: 20, MtimeNs: 1, Priority: 1},
		{RelPath: "a/meta.mp4", RuleID: "r", SourceDeviceID: "dev_b", Size: 30, MtimeNs: 1, Priority: 0},
	} {
		if ok, err := s.Enqueue(ctx, f); err != nil || !ok {
			t.Fatal(ok, err)
		}
	}
	// 같은 크기·수정시각 재발견은 무시
	if ok, _ := s.Enqueue(ctx, File{RelPath: "a/clip.mp4", RuleID: "r", SourceDeviceID: "dev_a", Size: 20, MtimeNs: 1}); ok {
		t.Fatal("중복 등록")
	}
	next, _ := s.Next(ctx, 10)
	if len(next) != 3 || next[0].RelPath != "a/meta.mp4" || next[1].RelPath != "a/clip.mp4" {
		t.Fatalf("순서 오류: %v", []string{next[0].RelPath, next[1].RelPath})
	}
	_ = s.MarkUploading(ctx, next[2].ID, "upl_1", "ast_1")
	next, _ = s.Next(ctx, 10)
	if next[0].Status != Uploading {
		t.Fatal("진행 중 업로드가 먼저여야 한다")
	}
	_ = s.MarkDone(ctx, next[0].ID, "ast_1")
	st, _ := s.Stats(ctx)
	if st.QueueItems != 2 || st.Done != 1 || st.QueueBytes != 50 || len(st.Sources) != 2 {
		t.Fatalf("통계 오류 %+v", st)
	}
	if v, _ := s.KV(ctx, "last_success_at"); v == "" {
		t.Fatal("마지막 성공 시각이 기록되어야 한다")
	}
	n, _ := s.ExcludeDisallowed(ctx, func(f *File) (bool, string) { return f.SourceDeviceID != "dev_b", "POLICY" })
	if n != 1 {
		t.Fatalf("정책 제외 %d", n)
	}
	// 끝난 파일이 바뀌면 다시 대기열로
	if ok, _ := s.Enqueue(ctx, File{RelPath: "a/general.mp4", RuleID: "r", SourceDeviceID: "dev_a", Size: 11, MtimeNs: 2}); !ok {
		t.Fatal("바뀐 파일은 다시 넣어야 한다")
	}
}

func TestReceiptAndDonePersistTogetherAndChangedFileClearsReceipt(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Enqueue(ctx, File{RelPath: "sensor/a.csv", RuleID: "r", SourceDeviceID: "dev_a", Size: 10, MtimeNs: 1}); err != nil {
		t.Fatal(err)
	}
	f, err := s.Get(ctx, "sensor/a.csv")
	if err != nil || f == nil {
		t.Fatal(err)
	}
	if err := s.MarkUploading(ctx, f.ID, "upl_1", "ast_1"); err != nil {
		t.Fatal(err)
	}
	f, err = s.Get(ctx, f.RelPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.AwaitReceipt(ctx, f, "ast_1"); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close() }()
	if pending, err := s.ReceiptPending(ctx, f.ID); err != nil || !pending {
		t.Fatal("영수증 대기 표시가 재시작 뒤 사라졌다", err)
	}
	clock := time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC)
	s.Now = func() time.Time { return clock }
	const receipt = `{"asset_id":"ast_1","synthetic":true}`
	if err := s.MarkDoneWithReceipt(ctx, f, receipt); err != nil {
		t.Fatal(err)
	}
	f, err = s.Get(ctx, f.RelPath)
	stored, receiptErr := s.KV(ctx, fmt.Sprintf("receipt:%d", f.ID))
	success, successErr := s.KV(ctx, "last_success_at")
	pending, pendingErr := s.ReceiptPending(ctx, f.ID)
	if err != nil || receiptErr != nil || successErr != nil || pendingErr != nil || f.Status != Done ||
		stored != receipt || pending || success != clock.Format(time.RFC3339) {
		t.Fatal("실영수증·완료·성공 시각이 함께 저장되지 않았다")
	}
	if ok, err := s.Enqueue(ctx, File{RelPath: f.RelPath, RuleID: "r", SourceDeviceID: "dev_a", Size: 11, MtimeNs: 2}); !ok || err != nil {
		t.Fatal("바뀐 파일을 새 전송으로 다시 넣지 못했다", err)
	}
	if raw, _ := s.KV(ctx, fmt.Sprintf("receipt:%d", f.ID)); raw != "" {
		t.Fatal("이전 파일의 영수증이 새 전송에 남았다")
	}
	if pending, _ := s.ReceiptPending(ctx, f.ID); pending {
		t.Fatal("이전 영수증 대기가 새 전송에 남았다")
	}
}

func TestStaleOrMissingFileCannotReceiveOldReceipt(t *testing.T) {
	for _, change := range []string{"sha256", "size", "mtime", "asset", "upload", "status", "missing"} {
		t.Run(change, func(t *testing.T) {
			ctx := context.Background()
			s, err := Open(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = s.Close() }()
			if _, err := s.Enqueue(ctx, File{RelPath: "sensor/a.csv", RuleID: "r", SourceDeviceID: "dev_a", Size: 10, MtimeNs: 1}); err != nil {
				t.Fatal(err)
			}
			f, _ := s.Get(ctx, "sensor/a.csv")
			if err := s.SetHash(ctx, f.ID, "old-sha"); err != nil {
				t.Fatal(err)
			}
			if err := s.MarkUploading(ctx, f.ID, "upl_old", "ast_old"); err != nil {
				t.Fatal(err)
			}
			f, _ = s.Get(ctx, f.RelPath)
			// 파일 ID가 같아도 영수증 조회 중 바뀐 파일 버전·전송에는 이전 결과를 적용하지 않는다.
			statements := map[string]string{
				"sha256":  `UPDATE files SET sha256='new-sha' WHERE id=?`,
				"size":    `UPDATE files SET size=11 WHERE id=?`,
				"mtime":   `UPDATE files SET mtime_ns=2 WHERE id=?`,
				"asset":   `UPDATE files SET asset_id='ast_new' WHERE id=?`,
				"upload":  `UPDATE files SET upload_id='upl_new' WHERE id=?`,
				"status":  `UPDATE files SET status='QUEUED' WHERE id=?`,
				"missing": `DELETE FROM files WHERE id=?`,
			}
			if _, err := s.db.ExecContext(ctx, statements[change], f.ID); err != nil {
				t.Fatal(err)
			}
			if err := s.MarkDoneWithReceipt(ctx, f, `{"old_receipt":true}`); !errors.Is(err, ErrStaleFile) {
				t.Fatalf("이전 파일 영수증이 새 상태를 덮었다: %v", err)
			}
			if err := s.AwaitReceipt(ctx, f, f.AssetID); !errors.Is(err, ErrStaleFile) {
				t.Fatalf("사라졌거나 바뀐 전송을 영수증 대기로 바꿨다: %v", err)
			}
			if receipt, _ := s.KV(ctx, fmt.Sprintf("receipt:%d", f.ID)); receipt != "" {
				t.Fatal("현재 파일 행과 무관한 영수증이 남았다")
			}
			if success, _ := s.KV(ctx, "last_success_at"); success != "" {
				t.Fatal("이전 파일 결과로 성공 시각을 기록했다")
			}
			current, _ := s.Get(ctx, f.RelPath)
			if current != nil && current.Status == Done {
				t.Fatal("현재 파일을 이전 전송으로 완료했다")
			}
		})
	}
}

func TestScanCannotReplaceFileWhileAwaitingReceipt(t *testing.T) {
	ctx := context.Background()
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close() }()
	first := File{RelPath: "sensor/a.csv", RuleID: "r", SourceDeviceID: "dev_a", Size: 10, MtimeNs: 1}
	if _, err := s.Enqueue(ctx, first); err != nil {
		t.Fatal(err)
	}
	f, _ := s.Get(ctx, first.RelPath)
	if err := s.MarkUploading(ctx, f.ID, "upl_1", "ast_1"); err != nil {
		t.Fatal(err)
	}
	f, _ = s.Get(ctx, f.RelPath)
	if err := s.AwaitReceipt(ctx, f, f.AssetID); err != nil {
		t.Fatal(err)
	}
	next := first
	next.Size, next.MtimeNs = 11, 2
	if ok, err := s.Enqueue(ctx, next); ok || err != nil {
		t.Fatal("스캔이 진행 중 전송을 교체했다", err)
	}
	current, _ := s.Get(ctx, f.RelPath)
	if current.Status != Uploading || current.Size != 10 || current.UploadID != "upl_1" {
		t.Fatal("영수증 대기 파일의 전송 상태가 바뀌었다")
	}
}
