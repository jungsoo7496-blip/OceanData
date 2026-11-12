package core

// Idempotency-Key는 요청 본문의 함수여야 한다 (지시서 12번 E2E 발견 F1, ADR-0054).
//
// 서버는 같은 Idempotency-Key로 다른 본문이 오면 409 IDEMPOTENCY_CONFLICT로 거부한다. 예전 에이전트는 키를
// sha256(원천|해시|variant|회차|규칙)로 만들어 파일 이름·경로가 본문에만 들어갔다. 같은 내용을 다른 이름으로 복사하면
// 키는 같고 본문은 달라 409가 났고, 에이전트는 그 파일을 재시도 없는 실패(FAILED)로 두었다.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
)

// 같은 내용을 다른 이름으로 복사해도 IDEMPOTENCY_CONFLICT로 영구 실패하지 않는다. 서버는 이미 아는 해시이므로
// ALREADY_REGISTERED로 답하고, 에이전트는 영수증을 확인한 뒤 완료로 기록한다.
func TestSameContentUnderNewNameIsNotAnIdempotencyConflict(t *testing.T) {
	ctx := context.Background()
	e := newEnv(t)
	a := e.agent(t)
	data := []byte("time,temp\n2026-11-01T00:00:00Z,1\n")
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])

	e.write(t, "S1/sensor/a.csv", data)
	e.scanStable(t, a)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	first, _ := a.Store.Get(ctx, "S1/sensor/a.csv")
	if first.Status != state.Done {
		t.Fatalf("처음 파일이 완료되지 않았다: %+v", first)
	}

	// 같은 바이트를 다른 이름으로 복사한다. 모의 서버는 이 해시를 이미 안다(bySHA) → ALREADY_REGISTERED(ast_x).
	e.fake.mu.Lock()
	e.fake.receipts["ast_x"] = fakeReceipt("ast_x", sha, int64(len(data)))
	e.fake.mu.Unlock()
	e.write(t, "S1/sensor/a_copy.csv", data)
	e.scanStable(t, a)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	second, _ := a.Store.Get(ctx, "S1/sensor/a_copy.csv")
	if second.Status != state.Done {
		t.Fatalf("같은 내용의 복사본은 완료로 끝나야 한다 (상태 %s, 최근 오류 %q)", second.Status, second.LastError)
	}
	if len(e.fake.uploads) != 1 {
		t.Fatalf("복사본 때문에 업로드 세션이 늘었다: %d", len(e.fake.uploads))
	}
}

var localIDPattern = regexp.MustCompile(`^lc-[0-9a-f]{32}$`)

// 본문이 같으면 키가 같고(재시도는 처음 응답을 되받는다), 서버가 비교하는 본문이 하나라도 다르면 키도 다르다.
// 본문에 새 필드가 생겨도 키가 따라가는지 한눈에 보이도록 '키가 같다 ⇔ 본문이 같다'를 쌍마다 확인한다.
func TestIdempotencyKeyFollowsTheRequestBody(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	base := state.File{RelPath: "S1/sensor/a.csv", RuleID: "csv", SourceDeviceID: sensorDev, SessionLabel: "S1",
		Size: 33, SHA256: strings.Repeat("a", 64)}

	type variant struct {
		name  string
		same  bool // 본문이 base와 같아야 하는가
		build func() (*api.Policy, *state.File, string)
	}
	policyWith := func(edit func(*api.Policy)) *api.Policy {
		p := *a.policy.Load()
		p.WatchRules = append([]api.WatchRule(nil), p.WatchRules...)
		edit(&p)
		return &p
	}
	file := func(edit func(*state.File)) *state.File {
		f := base
		edit(&f)
		return &f
	}
	abs := func(f *state.File) string { return filepath.Join(e.root, filepath.FromSlash(f.RelPath)) }
	cases := []variant{
		{"같은 파일을 다시", true, func() (*api.Policy, *state.File, string) {
			f := file(func(*state.File) {})
			return a.policy.Load(), f, abs(f)
		}},
		{"다른 파일 이름(같은 내용 복사본)", false, func() (*api.Policy, *state.File, string) {
			f := file(func(f *state.File) { f.RelPath = "S1/sensor/a_copy.csv" })
			return a.policy.Load(), f, abs(f)
		}},
		{"같은 이름, 다른 폴더", false, func() (*api.Policy, *state.File, string) {
			f := file(func(f *state.File) { f.RelPath = "S1/sensor/old/a.csv" })
			return a.policy.Load(), f, abs(f)
		}},
		{"다른 내용(해시)", false, func() (*api.Policy, *state.File, string) {
			f := file(func(f *state.File) { f.SHA256 = strings.Repeat("b", 64) })
			return a.policy.Load(), f, abs(f)
		}},
		{"다른 크기", false, func() (*api.Policy, *state.File, string) {
			f := file(func(f *state.File) { f.Size = 34 })
			return a.policy.Load(), f, abs(f)
		}},
		{"다른 회차 표시값", false, func() (*api.Policy, *state.File, string) {
			f := file(func(f *state.File) { f.SessionLabel = "S2" })
			return a.policy.Load(), f, abs(f)
		}},
		{"다른 규칙·원천 장비", false, func() (*api.Policy, *state.File, string) {
			f := file(func(f *state.File) { f.RuleID, f.SourceDeviceID = "cam", camera; f.RelPath = "S1/cam01/a.mp4" })
			return a.policy.Load(), f, abs(f)
		}},
		{"규칙의 기본 데이터 역할이 바뀐 정책", false, func() (*api.Policy, *state.File, string) {
			p := policyWith(func(p *api.Policy) {
				for i := range p.WatchRules {
					if p.WatchRules[i].RuleID == "csv" {
						p.WatchRules[i].DataRoleDefault = "SUPPORT"
					}
				}
			})
			f := file(func(*state.File) {})
			return p, f, abs(f)
		}},
	}

	baseP := a.policy.Load()
	baseM, baseKey := a.manifest(baseP, &base, abs(&base))
	seen := map[string]string{} // 키 → 사례 이름 (서로 다른 본문이 같은 키를 쓰지 않는지)
	for _, c := range cases {
		p, f, path := c.build()
		m, key := a.manifest(p, f, path)
		if !localIDPattern.MatchString(key) {
			t.Fatalf("%s: 키 형식 %q", c.name, key)
		}
		if got := m["client"].(map[string]any)["local_collection_id"]; got != key {
			t.Fatalf("%s: client.local_collection_id %v는 Idempotency-Key %q와 같아야 한다 (계약)", c.name, got, key)
		}
		if same := bodyWithoutLocalID(t, m) == bodyWithoutLocalID(t, baseM); same != c.same {
			t.Fatalf("%s: 시험 전제가 틀렸다 (본문 같음=%v)", c.name, same)
		}
		if (key == baseKey) != c.same {
			t.Errorf("%s: 키 %q, 기준 키 %q — 본문이 %s인데 키는 %s",
				c.name, key, baseKey, map[bool]string{true: "같음", false: "다름"}[c.same],
				map[bool]string{true: "같다", false: "다르다"}[key == baseKey])
		}
		if !c.same {
			if prev, dup := seen[key]; dup {
				t.Errorf("%s: 다른 본문인데 %q와 같은 키를 쓴다", c.name, prev)
			}
			seen[key] = c.name
		}
	}

	// 에이전트 버전도 본문(client.version)에 들어간다 — 같은 파일이라도 버전이 바뀌면 키가 바뀐다.
	a.Version = "other-version"
	if m, key := a.manifest(baseP, &base, abs(&base)); key == baseKey ||
		bodyWithoutLocalID(t, m) == bodyWithoutLocalID(t, baseM) {
		t.Errorf("버전이 다르면 본문도 키도 달라야 한다 (키 %q)", key)
	}
}

// bodyWithoutLocalID는 매니페스트에서 local_collection_id만 뺀 정본 JSON이다 (map은 키 순서로 직렬화된다).
func bodyWithoutLocalID(t *testing.T, m map[string]any) string {
	t.Helper()
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var back map[string]any
	if err := json.Unmarshal(raw, &back); err != nil {
		t.Fatal(err)
	}
	delete(back["client"].(map[string]any), "local_collection_id")
	out, err := json.Marshal(back)
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}
