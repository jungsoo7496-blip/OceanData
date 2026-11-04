// Package rules는 감시 규칙(Watch_Rule)으로 파일을 분류하고 collection_mode별 variant 허용을 판단한다.
package rules

import (
	"path"
	"strings"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

// Match 결과
type Match struct {
	Rule         api.WatchRule
	SessionLabel string
}

// MatchPath는 감시 루트 기준 상대 경로(슬래시 구분)를 규칙의 path_pattern과 맞춘다.
// 패턴 조각: "{session}"은 한 조각을 회차 표시값으로 뽑고, 나머지는 path.Match 글롭이다.
// include·exclude는 파일 이름에 적용한다. 숨김·임시 파일은 항상 제외한다.
func MatchPath(rel string, rule api.WatchRule) (Match, bool) {
	rel = strings.TrimPrefix(path.Clean("/"+rel), "/")
	base := path.Base(rel)
	if strings.HasPrefix(base, ".") || strings.HasPrefix(base, "~$") {
		return Match{}, false
	}
	segs := strings.Split(rel, "/")
	pats := strings.Split(strings.Trim(rule.PathPattern, "/"), "/")
	if len(segs) != len(pats) {
		return Match{}, false
	}
	label := ""
	for i, p := range pats {
		if p == "{session}" {
			if segs[i] == "" {
				return Match{}, false
			}
			label = segs[i]
			continue
		}
		ok, err := path.Match(p, segs[i])
		if err != nil || !ok {
			return Match{}, false
		}
	}
	if len(rule.Include) > 0 && !anyMatch(rule.Include, base) {
		return Match{}, false
	}
	if anyMatch(rule.Exclude, base) {
		return Match{}, false
	}
	return Match{Rule: rule, SessionLabel: label}, true
}

func anyMatch(patterns []string, name string) bool {
	lower := strings.ToLower(name)
	for _, p := range patterns {
		if ok, _ := path.Match(strings.ToLower(p), lower); ok {
			return true
		}
	}
	return false
}

// First는 규칙 목록에서 처음 맞는 규칙을 돌려준다 (priority가 낮은 값이 먼저).
func First(rel string, rules []api.WatchRule) (Match, bool) {
	var best *Match
	for _, r := range rules {
		if m, ok := MatchPath(rel, r); ok {
			if best == nil || m.Rule.Priority < best.Rule.Priority {
				mm := m
				best = &mm
			}
		}
	}
	if best == nil {
		return Match{}, false
	}
	return *best, true
}

var mediaVariants = map[string]map[string]bool{
	"C0_METADATA_ONLY":  {},
	"C1_SAMPLE_PREVIEW": {"SAMPLE_FRAME": true, "PROXY": true},
	"C2_EVENT_CLIPS":    {"SAMPLE_FRAME": true, "PROXY": true, "CLIP": true},
	"C3_APPROVED_FULL":  {"SAMPLE_FRAME": true, "PROXY": true, "CLIP": true, "ORIGINAL": true},
}

// VariantAllowed는 domain.md 3장 표와 같다. C0은 바이너리를 보내지 않고 메타데이터만 등록한다.
func VariantAllowed(mode, dataType, variant string) bool {
	if mode == "C0_METADATA_ONLY" {
		return false
	}
	if dataType == "VIDEO" || dataType == "IMAGE" {
		return mediaVariants[mode][variant]
	}
	return true
}

// Priority는 전송 순위다: C0 메타데이터 → C2 이벤트 클립 → 일반 (06번 요구사항 7.1). 작을수록 먼저.
func Priority(mode, variant string) int {
	switch {
	case mode == "C0_METADATA_ONLY":
		return 0
	case variant == "CLIP" || mode == "C2_EVENT_CLIPS":
		return 1
	default:
		return 2
	}
}

var mimeByExt = map[string]string{
	".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
	".webm": "video/webm", ".avi": "video/x-msvideo", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
	".png": "image/png", ".csv": "text/csv", ".json": "application/json", ".txt": "text/plain",
	".log": "text/plain", ".pdf": "application/pdf", ".zip": "application/zip", ".ndjson": "application/x-ndjson",
}

// MimeType은 확장자로 신고 MIME을 정한다. 서버가 내용(매직 바이트)으로 다시 판별한다.
func MimeType(name string) string {
	if m, ok := mimeByExt[strings.ToLower(path.Ext(name))]; ok {
		return m
	}
	return "application/octet-stream"
}
