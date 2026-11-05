package rules

import (
	"path"
	"slices"
	"strings"
	"testing"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

// 퍼즈 시험: 경로는 감시 폴더(디스크)에서, path_pattern·include·exclude는 서버 정책에서 온다 (06번 요구사항 2.1).
// 실행: go test -run='^$' -fuzz='^FuzzMatchPath$' -fuzztime=90s -parallel=2 ./internal/rules
func FuzzMatchPath(f *testing.F) {
	// TestMatchPath·TestFirstUsesPriority의 예
	for _, rel := range []string{"A-2026-11-01/cam01/0001.mp4", "A-2026-11-01/cam01/0001.MP4", "A-2026-11-01/cam02/0001.mp4",
		"A-2026-11-01/cam01/0001.tmp", "A-2026-11-01/cam01/.0001.mp4", "cam01/0001.mp4", "A/cam01/sub/0001.mp4"} {
		f.Add(rel, "{session}/cam01/*", "*.mp4", "*.tmp")
	}
	f.Add("s/x.mp4", "*/*", "*.mp4", "")
	f.Add("./a//../B-1/cam01/~$x.mp4", "/{session}/cam0[1-2]/*/", "", "[")
	f.Add("", "{session}", "", "")
	f.Fuzz(func(t *testing.T, rel, pattern, include, exclude string) {
		rule := api.WatchRule{RuleID: "fuzz", PathPattern: pattern, Priority: 1}
		if include != "" {
			rule.Include = []string{include}
		}
		if exclude != "" {
			rule.Exclude = []string{exclude}
		}
		m, ok := MatchPath(rel, rule)
		if !ok {
			if m.SessionLabel != "" || m.Rule.RuleID != "" {
				t.Fatalf("맞지 않았는데 결과가 있다: %+v", m)
			}
			return
		}
		if m.Rule.RuleID != rule.RuleID || m.Rule.PathPattern != pattern {
			t.Fatalf("다른 규칙을 돌려줬다: %+v", m.Rule)
		}
		base := path.Base(path.Clean("/" + rel)) // MatchPath처럼 정리한 경로의 파일 이름
		if strings.HasPrefix(base, ".") || strings.HasPrefix(base, "~$") {
			t.Fatalf("숨김·임시 파일 %q가 맞았다", rel)
		}
		hasSession := slices.Contains(strings.Split(strings.Trim(pattern, "/"), "/"), "{session}")
		if !hasSession {
			if m.SessionLabel != "" {
				t.Fatalf("{session}이 없는 패턴 %q에서 회차 %q를 뽑았다", pattern, m.SessionLabel)
			}
			return
		}
		// 회차 표시값은 경로의 한 조각 그대로다: 비어 있지 않고 /·상위 폴더 이동을 담지 않는다
		l := m.SessionLabel
		if l == "" || l == "." || l == ".." || strings.Contains(l, "/") || !slices.Contains(strings.Split(rel, "/"), l) {
			t.Fatalf("경로 %q 패턴 %q에서 잘못된 회차 %q", rel, pattern, l)
		}
	})
}
