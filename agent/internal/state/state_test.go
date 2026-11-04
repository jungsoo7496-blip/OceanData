package state

import (
	"context"
	"testing"
)

func TestQueueOrderAndLifecycle(t *testing.T) {
	ctx := context.Background()
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
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
