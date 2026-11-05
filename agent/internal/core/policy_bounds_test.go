package core

// ADR-0040: 서버가 준 정수를 time.Duration으로 곱해 넘치던 문제(퍼즈 시험으로 찾음)의 회귀 시험.
// 계약 범위 밖 정책은 저장·적용하지 않고 이전 정책을 유지하며 하트비트로 POLICY_INVALID를 보고한다.
// 범위 검사를 거치지 않은 값이 와도 각 변환이 따로 넘치지 않는다.

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"testing"
	"time"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/config"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/timex"
)

// goDurationOverflowS는 time.Duration(n)*time.Second가 int64 나노초를 넘어 음수가 되는 가장 작은 초다.
const goDurationOverflowS = 1<<63/1_000_000_000 + 1

// agentWithoutPolicy는 e.agent와 같지만 정책을 받지 않는다 (시작 시점 시험용).
func (e *env) agentWithoutPolicy(t *testing.T) *Agent {
	t.Helper()
	cfg := &config.Config{ServerURL: e.fake.URL, WatchRoot: e.root, DeviceID: collector}
	cfg.Defaults()
	st, err := state.Open(e.dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	st.Now = func() time.Time { return e.clock }
	hc, _ := api.NewHTTPClient("", 30*time.Second)
	c := api.New(e.fake.URL, hc, &api.Credentials{DeviceID: collector, ClientID: "dcr_x", ClientSecret: "s"}, "test")
	a := New(cfg, e.dir, c, st, slog.New(slog.NewTextHandler(io.Discard, nil)), "test")
	a.Now = func() time.Time { return e.clock }
	return a
}

func (e *env) setPolicy(change func(p *api.Policy)) {
	e.fake.mu.Lock()
	defer e.fake.mu.Unlock()
	change(&e.fake.policy)
}

func storedPolicyVersion(t *testing.T, a *Agent) string {
	t.Helper()
	raw, err := a.Store.KV(context.Background(), KeyPolicy)
	var p api.Policy
	if err != nil || json.Unmarshal([]byte(raw), &p) != nil {
		t.Fatalf("저장된 정책을 읽지 못했다: %q, %v", raw, err)
	}
	return p.PolicyVersion
}

func TestPolicyOutOfContractRangeKeepsPreviousPolicy(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t) // pv-1 적용·저장
	ctx := context.Background()
	zero, huge := 0, maxPolicyInt32+1
	for _, c := range []struct {
		name   string
		change func(p *api.Policy)
	}{
		{"heartbeat_interval_s가 time.Duration을 넘침", func(p *api.Policy) { p.HeartbeatIntervalS = goDurationOverflowS }},
		{"heartbeat_interval_s가 계약 상한+1", func(p *api.Policy) { p.HeartbeatIntervalS = maxPolicySeconds + 1 }},
		{"heartbeat_interval_s가 계약 하한 미만", func(p *api.Policy) { p.HeartbeatIntervalS = minHeartbeatIntervalS - 1 }},
		{"heartbeat_interval_s가 음수", func(p *api.Policy) { p.HeartbeatIntervalS = -goDurationOverflowS }},
		{"heartbeat_interval_s가 0", func(p *api.Policy) { p.HeartbeatIntervalS = 0 }},
		{"size_stable_s가 넘쳐 0.29초가 되는 값", func(p *api.Policy) { p.SizeStableS = 18446744074 }},
		{"size_stable_s가 음수", func(p *api.Policy) { p.SizeStableS = -1 }},
		{"size_stable_s가 0", func(p *api.Policy) { p.SizeStableS = 0 }},
		{"max_parallel_parts가 파트 번호 상한+1", func(p *api.Policy) { p.MaxParallelParts = maxParallelParts + 1 }},
		{"max_parallel_parts가 0", func(p *api.Policy) { p.MaxParallelParts = 0 }},
		{"max_kbps가 0", func(p *api.Policy) {
			p.TransferWindows = []api.TransferWindow{{Days: []string{"MON"}, StartLocal: "00:00", EndLocal: "24:00", MaxKbps: &zero}}
		}},
		{"max_kbps가 32비트 상한+1", func(p *api.Policy) {
			p.TransferWindows = []api.TransferWindow{{Days: []string{"MON"}, StartLocal: "00:00", EndLocal: "24:00", MaxKbps: &huge}}
		}},
		{"priority가 음수", func(p *api.Policy) { p.WatchRules[1].Priority = -1 }},
	} {
		t.Run(c.name, func(t *testing.T) {
			var before api.Policy
			e.setPolicy(func(p *api.Policy) {
				before = *p
				before.WatchRules = append([]api.WatchRule(nil), p.WatchRules...) // priority 경우가 원소를 바꾼다
				p.PolicyVersion = "pv-bad"
				c.change(p)
			})
			t.Cleanup(func() { e.setPolicy(func(p *api.Policy) { *p = before }) })
			a.lastErr.Store("")
			if err := a.RefreshPolicy(ctx); !errors.Is(err, ErrPolicyInvalid) {
				t.Fatalf("계약 범위 밖 정책을 거부하지 않았다: %v", err)
			}
			if got := a.Policy(); got.PolicyVersion != "pv-1" || got.HeartbeatIntervalS != 60 || got.SizeStableS != 1 {
				t.Fatalf("이전 정책을 유지하지 않았다: %+v", got)
			}
			if v := storedPolicyVersion(t, a); v != "pv-1" {
				t.Fatalf("거부한 정책을 저장했다: %s", v)
			}
			hb, _ := a.Heartbeat(ctx) // 하트비트가 이미 보고하는 항목으로 알린다
			if hb["last_error_code"] != CodePolicyInvalid || hb["policy_version"] != "pv-1" {
				t.Fatalf("하트비트 last_error_code·적용 정책: %v %v", hb["last_error_code"], hb["policy_version"])
			}
		})
	}
	// 서버가 고친 정책은 다시 받아 적용한다. 상한 값은 그대로 받는다
	e.setPolicy(func(p *api.Policy) {
		p.PolicyVersion, p.HeartbeatIntervalS, p.SizeStableS, p.MaxParallelParts = "pv-2", maxPolicySeconds, maxPolicySeconds, maxParallelParts
	})
	if err := a.RefreshPolicy(ctx); err != nil {
		t.Fatal(err)
	}
	if a.Policy().PolicyVersion != "pv-2" || storedPolicyVersion(t, a) != "pv-2" {
		t.Fatal("계약 범위 안의 정책을 적용하지 않았다")
	}
	if got, want := a.heartbeatInterval(), time.Duration(maxPolicySeconds)*time.Second; got != want {
		t.Fatalf("하트비트 주기 %v, want %v", got, want)
	}
}

func TestServerRejectedPolicyKeepsPreviousPolicy(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	e.fake.mu.Lock()
	e.fake.override = func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Path != "/v1/devices/"+collector+"/policy" {
			return false
		}
		e.fake.json(w, http.StatusUnprocessableEntity, map[string]any{"code": "RULE_VIOLATION"})
		return true
	}
	e.fake.mu.Unlock()
	ctx := context.Background()
	if err := a.RefreshPolicy(ctx); !errors.Is(err, ErrPolicyInvalid) {
		t.Fatalf("서버가 배포를 거부한 정책: %v", err)
	}
	if got := a.Policy().PolicyVersion; got != "pv-1" || storedPolicyVersion(t, a) != "pv-1" {
		t.Fatalf("이전 정책을 유지하지 않았다: %s", got)
	}
	if a.lastErr.Load() != CodePolicyInvalid {
		t.Fatal("정책 오류를 보고하지 않았다")
	}
}

// Go 정수(int64)에 담기지 않는 수는 JSON 해석 단계에서 걸린다. 그것도 계약 범위 밖 정책으로 보고 이전 정책을 쓴다.
func TestPolicyNumberBeyondInt64IsReportedAndIgnored(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	e.fake.mu.Lock()
	b, _ := json.Marshal(e.fake.policy)
	e.fake.mu.Unlock()
	var doc map[string]any
	if err := json.Unmarshal(b, &doc); err != nil {
		t.Fatal(err)
	}
	doc["policy_version"], doc["heartbeat_interval_s"] = "pv-raw", json.Number("99999999999999999999")
	raw, _ := json.Marshal(doc)
	e.fake.mu.Lock()
	e.fake.override = func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Path != "/v1/devices/"+collector+"/policy" {
			return false
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(raw)
		return true
	}
	e.fake.mu.Unlock()
	if err := a.RefreshPolicy(context.Background()); !errors.Is(err, ErrPolicyInvalid) {
		t.Fatalf("int64를 넘는 수를 정책 오류로 보지 않았다: %v", err)
	}
	if a.Policy().PolicyVersion != "pv-1" || a.lastErr.Load() != CodePolicyInvalid {
		t.Fatalf("이전 정책 유지·오류 코드: %s %v", a.Policy().PolicyVersion, a.lastErr.Load())
	}
}

// 시작할 때(메모리에 정책 없음): 서버 정책이 범위 밖이면 저장된 이전 정책을 쓴다. 예전 버전이 검사 없이 저장한
// 범위 밖 정책은 서버에 닿지 않아도 쓰지 않는다.
func TestStartupUsesOnlyValidCachedPolicy(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t) // pv-1 저장
	_ = a.Store.Close()
	e.setPolicy(func(p *api.Policy) { p.PolicyVersion, p.HeartbeatIntervalS = "pv-bad", goDurationOverflowS })
	ctx := context.Background()
	b := e.agentWithoutPolicy(t)
	if err := b.RefreshPolicy(ctx); !errors.Is(err, ErrPolicyInvalid) {
		t.Fatalf("범위 밖 서버 정책: %v", err)
	}
	if b.Policy() == nil || b.Policy().PolicyVersion != "pv-1" || storedPolicyVersion(t, b) != "pv-1" {
		t.Fatal("시작할 때 저장된 이전 정책을 써야 한다")
	}

	// 예전 버전이 저장한 범위 밖 정책 + 서버 단절
	var legacy api.Policy
	e.setPolicy(func(p *api.Policy) { legacy = *p })
	legacy.PolicyVersion, legacy.HeartbeatIntervalS = "pv-legacy", goDurationOverflowS
	raw, _ := json.Marshal(legacy)
	if err := b.Store.SetKV(ctx, KeyPolicy, string(raw)); err != nil {
		t.Fatal(err)
	}
	_ = b.Store.Close()
	e.fake.Close()
	c := e.agentWithoutPolicy(t)
	if err := c.RefreshPolicy(ctx); !errors.Is(err, api.ErrOffline) {
		t.Fatalf("서버 단절: %v", err)
	}
	if c.Policy() != nil {
		t.Fatalf("범위 밖 저장 정책을 썼다: %+v", c.Policy())
	}
	if code, _ := c.Store.KV(ctx, KeyLastError); code != CodePolicyInvalid {
		t.Fatalf("저장 정책 거부를 기록하지 않았다: %q", code)
	}
}

// 퍼즈로 찾은 증상: 아주 큰 heartbeat_interval_s가 음수 대기가 되어 하트비트 루프가 쉬지 않고 POST했다.
func TestHugeHeartbeatIntervalDoesNotSpinHeartbeatLoop(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t) // pv-1: 60초
	e.setPolicy(func(p *api.Policy) { p.PolicyVersion, p.HeartbeatIntervalS = "pv-2", goDurationOverflowS })
	// 루프를 시작하기 전에 새 정책을 받아 둔다 (적용했다면 하트비트 루프가 처음부터 그 주기로 돈다)
	if err := a.RefreshPolicy(context.Background()); !errors.Is(err, ErrPolicyInvalid) {
		t.Fatalf("범위 밖 정책: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := a.Run(ctx); err != nil {
		t.Fatal(err)
	}
	e.fake.mu.Lock()
	sent := len(e.fake.heartbeats)
	e.fake.mu.Unlock()
	if sent > 2 { // 시작할 때 한 번 (범위 밖 정책을 적용했다면 1초에 수백 번)
		t.Fatalf("하트비트 루프가 쉬지 않고 돌았다: 1초에 %d회", sent)
	}
	if a.Policy().PolicyVersion != "pv-1" || a.heartbeatInterval() != 60*time.Second {
		t.Fatalf("이전 정책(60초)을 유지해야 한다: %s %v", a.Policy().PolicyVersion, a.heartbeatInterval())
	}
}

// 범위 검사를 거치지 않은 값이 와도 각 변환이 따로 넘치지 않는다 (정책 적용 함수를 직접 부른다).
func TestDurationConversionsAreSafeOnTheirOwn(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	ctx := context.Background()
	p := *a.Policy()
	p.HeartbeatIntervalS = goDurationOverflowS
	p.SizeStableS = 18446744074 // time.Duration 곱셈이 넘치면 0.29초가 되어 쓰기 완료 판정 대기를 건너뛰었다
	a.applyPolicy(ctx, &p)
	if got := a.heartbeatInterval(); got != timex.Seconds(timex.MaxSeconds) {
		t.Fatalf("하트비트 대기 %v (음수면 루프가 쉬지 않는다)", got)
	}
	e.write(t, "S1/sensor/stable.csv", []byte("synthetic,1\n"))
	if n, _ := a.Scan(ctx); n != 0 {
		t.Fatal("처음 본 파일을 바로 넣었다")
	}
	e.clock = e.clock.Add(24 * time.Hour)
	if n, _ := a.Scan(ctx); n != 0 {
		t.Fatal("size_stable_s 변환이 넘쳐 쓰기 완료 판정 대기를 건너뛰었다")
	}
}

// 서버(또는 중간 장비)가 아주 큰 Retry-After를 주어도 재시도 시각(UnixNano)이 넘쳐 다음 주기에 곧바로 다시 보내지 않는다.
func TestHugeRetryAfterDoesNotRetryOnNextCycle(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	ctx := context.Background()
	rel := "S1/sensor/limited.csv"
	e.write(t, rel, []byte("synthetic,1\n"))
	if n := e.scanStable(t, a); n != 1 {
		t.Fatalf("대기열 추가 %d", n)
	}
	e.fake.mu.Lock()
	uploads := 0
	e.fake.override = func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Path != "/v1/uploads" || r.Method != http.MethodPost {
			return false
		}
		uploads++
		w.Header().Set("Retry-After", "9000000000") // 약 285년: time.Duration에는 들지만 지금+대기는 2262년을 넘는다
		e.fake.json(w, 429, map[string]any{"status": 429, "code": "RATE_LIMITED"})
		return true
	}
	e.fake.mu.Unlock()
	if n, _ := a.ProcessOnce(ctx); n != 1 {
		t.Fatalf("처리 %d", n)
	}
	f, err := a.Store.Get(ctx, rel)
	if err != nil || f.Status != state.Queued || f.LastError != "RATE_LIMITED" {
		t.Fatalf("재시도 대기 상태: %+v %v", f, err)
	}
	if !f.NextAttemptAt.After(e.clock.Add(365 * 24 * time.Hour)) {
		t.Fatalf("재시도 시각이 넘쳐 과거가 됐다: %v", f.NextAttemptAt)
	}
	if n, _ := a.ProcessOnce(ctx); n != 0 {
		t.Fatal("Retry-After를 무시하고 다음 주기에 다시 보냈다")
	}
	e.fake.mu.Lock()
	defer e.fake.mu.Unlock()
	if uploads != 1 {
		t.Fatalf("업로드 요청 %d회", uploads)
	}
}
