package rules

import (
	"testing"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

func TestMatchPath(t *testing.T) {
	r := api.WatchRule{RuleID: "bow", PathPattern: "{session}/cam01/*", Include: []string{"*.mp4"},
		Exclude: []string{"*.tmp"}}
	cases := []struct {
		rel   string
		ok    bool
		label string
	}{
		{"A-2026-11-01/cam01/0001.mp4", true, "A-2026-11-01"},
		{"A-2026-11-01/cam01/0001.MP4", true, "A-2026-11-01"},
		{"A-2026-11-01/cam02/0001.mp4", false, ""},
		{"A-2026-11-01/cam01/0001.tmp", false, ""},
		{"A-2026-11-01/cam01/.0001.mp4", false, ""},
		{"cam01/0001.mp4", false, ""},
		{"A/cam01/sub/0001.mp4", false, ""},
	}
	for _, c := range cases {
		m, ok := MatchPath(c.rel, r)
		if ok != c.ok || m.SessionLabel != c.label {
			t.Errorf("%s: got %v %q want %v %q", c.rel, ok, m.SessionLabel, c.ok, c.label)
		}
	}
}

func TestFirstUsesPriority(t *testing.T) {
	rs := []api.WatchRule{{RuleID: "general", PathPattern: "*/*", Priority: 5},
		{RuleID: "clips", PathPattern: "*/*", Include: []string{"*.mp4"}, Priority: 1}}
	m, ok := First("s/x.mp4", rs)
	if !ok || m.Rule.RuleID != "clips" {
		t.Fatalf("got %+v", m)
	}
}

func TestVariantAllowedAndPriority(t *testing.T) {
	if VariantAllowed("C2_EVENT_CLIPS", "VIDEO", "ORIGINAL") || !VariantAllowed("C2_EVENT_CLIPS", "VIDEO", "CLIP") {
		t.Fatal("C2 표가 domain.md와 다르다")
	}
	if !VariantAllowed("C1_SAMPLE_PREVIEW", "SENSOR", "ORIGINAL") || VariantAllowed("C0_METADATA_ONLY", "SENSOR", "ORIGINAL") {
		t.Fatal("비영상 규칙 오류")
	}
	if !(Priority("C0_METADATA_ONLY", "ORIGINAL") < Priority("C2_EVENT_CLIPS", "CLIP") &&
		Priority("C2_EVENT_CLIPS", "CLIP") < Priority("C3_APPROVED_FULL", "ORIGINAL")) {
		t.Fatal("전송 순위 오류")
	}
}
