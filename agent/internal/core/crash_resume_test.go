package core

// T03-b는 실제 테스트 자식 프로세스를 강제 종료한다. 72시간은 기존 Agent.Now·Store.Now로 진행시키며
// 실제 72시간 대기·전원 차단·운영 스토리지 내구성·15번 캡처 장비까지 검증했다고 해석하지 않는다.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/config"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
)

const crashChildEnv = "MARINE_T03_CRASH_CHILD_SPEC"
const crashRelPath = "S1/sensor/crash.csv"

type crashChildSpec struct {
	Phase, URL, Dir, Root, Ready string
	Start                        time.Time
}

type offlineProof struct {
	Start, End    time.Time
	Attempts      int
	UploadID, SHA string
	Heartbeat     string
}

type crashChild struct {
	cmd  *exec.Cmd
	done chan struct{}
	err  error
	log  bytes.Buffer
}

func startCrashChild(t *testing.T, spec crashChildSpec) *crashChild {
	t.Helper()
	raw, err := json.Marshal(spec)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "child.json")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	p := &crashChild{done: make(chan struct{})}
	p.cmd = exec.Command(exe, "-test.run=^TestT03CrashRecoveryHelper$", "-test.v")
	p.cmd.Env = append(os.Environ(), crashChildEnv+"="+path)
	p.cmd.Stdout, p.cmd.Stderr = &p.log, &p.log
	if err := p.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { p.err = p.cmd.Wait(); close(p.done) }()
	t.Cleanup(func() {
		select {
		case <-p.done:
			return
		default:
			_ = p.cmd.Process.Kill()
		}
		select {
		case <-p.done:
		case <-time.After(5 * time.Second):
			t.Error("시험 자식 프로세스 정리 시간 초과")
		}
	})
	return p
}

func waitCrashChildSignal(t *testing.T, p *crashChild, ready func() bool) {
	t.Helper()
	deadline := time.NewTimer(30 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		if ready() {
			return
		}
		select {
		case <-p.done:
			t.Fatalf("준비 전에 자식 프로세스 종료: %v\n%s", p.err, p.log.String())
		case <-deadline.C:
			t.Fatal("자식 프로세스 준비 시간 초과")
		case <-ticker.C:
		}
	}
}

// covers: T03-b
func TestT03ForcedKillAnd72HourOfflineResume(t *testing.T) {
	e := newEnv(t)
	e.fake.policy.MaxParallelParts = 1
	e.fake.policy.HeartbeatIntervalS = 1
	data := bytes.Repeat([]byte("synthetic-sensor\n"), (4<<20)/17+1)
	e.write(t, crashRelPath, data)
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	before, err := os.Stat(filepath.Join(e.root, filepath.FromSlash(crashRelPath)))
	if err != nil {
		t.Fatal(err)
	}
	var offline, blockThird atomic.Bool
	blockThird.Store(true)
	var offlineRequests, partLists atomic.Int32
	third := make(chan struct{})
	release := make(chan struct{})
	var reached, releaseOnce sync.Once
	t.Cleanup(func() { releaseOnce.Do(func() { close(release) }) })
	handler := e.fake.Config.Handler
	e.fake.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if offline.Load() {
			offlineRequests.Add(1)
			e.fake.json(w, 503, map[string]any{"status": 503, "code": "SERVICE_UNAVAILABLE"})
			return
		}
		if r.URL.Path == "/s3/upl1/3" && blockThird.Load() {
			_, _ = io.Copy(io.Discard, r.Body)
			reached.Do(func() { close(third) })
			select {
			case <-release:
			case <-r.Context().Done():
			}
			w.WriteHeader(503) // 3번 파트는 저장하지 않았다. 재시작 후 다시 보내야 한다.
			return
		}
		if strings.HasSuffix(r.URL.Path, "/parts") && r.Method == "GET" {
			partLists.Add(1)
		}
		handler.ServeHTTP(w, r)
	})
	start := time.Date(2026, 11, 4, 0, 0, 0, 0, time.UTC)
	spec := crashChildSpec{Phase: "upload", URL: e.fake.URL, Dir: e.dir, Root: e.root, Start: start}
	first := startCrashChild(t, spec)
	waitCrashChildSignal(t, first, func() bool {
		select {
		case <-third:
			return true
		default:
			return false
		}
	})
	offline.Store(true)
	if err := first.cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-first.done:
		if first.err == nil || first.cmd.ProcessState.Success() {
			t.Fatal("정상 종료가 아니라 실제 강제 종료여야 한다")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("강제 종료한 프로세스가 끝나지 않았다")
	}
	blockThird.Store(false)
	releaseOnce.Do(func() { close(release) })
	st, err := state.Open(e.dir)
	if err != nil {
		t.Fatal(err)
	}
	f, err := st.Get(context.Background(), crashRelPath)
	if err != nil || f == nil || f.Status != state.Uploading || f.UploadID != "upl1" || f.SHA256 != sha {
		t.Fatalf("강제 종료 뒤 SQLite 업로드 상태가 복구되지 않았다: %+v, %v", f, err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	ready := filepath.Join(t.TempDir(), "offline-ready.json")
	spec.Phase, spec.Ready = "offline-resume", ready
	second := startCrashChild(t, spec)
	var proof offlineProof
	waitCrashChildSignal(t, second, func() bool {
		b, err := os.ReadFile(ready)
		return err == nil && json.Unmarshal(b, &proof) == nil
	})
	if proof.End.Sub(proof.Start) != 72*time.Hour || proof.Attempts != 73 || proof.UploadID != "upl1" || proof.SHA != sha {
		t.Fatalf("72시간 모의 단절 동안 업로드·재시도 보존 실패: %+v", proof)
	}
	var heartbeat map[string]any
	if json.Unmarshal([]byte(proof.Heartbeat), &heartbeat) != nil || heartbeat["sent_at"] != proof.End.Format(time.RFC3339) {
		t.Fatal("단절 중 최신 하트비트가 SQLite에 보존되지 않았다")
	}
	queue, ok := heartbeat["queue"].(map[string]any)
	if !ok || queue["items"] != float64(1) || queue["bytes"] != float64(len(data)) || heartbeat["last_success_at"] != nil {
		t.Fatal("단절 중 대기 파일·바이트·영수증 전 성공 상태가 잘못됐다")
	}
	if offlineRequests.Load() < 73 {
		t.Fatal("오프라인 요청을 실제 모의 API가 거부하지 않았다")
	}
	offline.Store(false) // 이후 자식 Agent.Run 루프가 사용자 명령 없이 자동 재개한다.
	select {
	case <-second.done:
		if second.err != nil {
			t.Fatalf("재시작 후 자동 재개 실패: %v\n%s", second.err, second.log.String())
		}
	case <-time.After(30 * time.Second):
		t.Fatal("연결 복구 뒤 자동 재개 시간 초과")
	}
	st, err = state.Open(e.dir)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = st.Close() }()
	f, err = st.Get(context.Background(), crashRelPath)
	if err != nil || f.Status != state.Done {
		t.Fatalf("검증된 영수증 뒤 완료 상태가 보존되지 않았다: %+v, %v", f, err)
	}
	raw, _ := st.KV(context.Background(), fmt.Sprintf("receipt:%d", f.ID))
	var receipt api.Receipt
	if json.Unmarshal([]byte(raw), &receipt) != nil || receipt.AssetID != f.AssetID || receipt.ChecksumSHA256 != sha ||
		receipt.ByteSize == nil || *receipt.ByteSize != int64(len(data)) {
		t.Fatalf("서버 실영수증과 SQLite 기록이 다르다: %q", raw)
	}
	e.fake.mu.Lock()
	defer e.fake.mu.Unlock()
	u := e.fake.uploads["upl1"]
	if len(e.fake.uploads) != 1 || u == nil || !u.completed || partLists.Load() != 1 {
		t.Fatal("동일 세션의 서버 파트 목록으로 재개하지 않았다")
	}
	var uploaded bytes.Buffer
	for n := 1; n <= u.partCount; n++ {
		uploaded.Write(u.parts[n])
		if count := e.fake.putCount[fmt.Sprintf("upl1/%d", n)]; count != 1 {
			t.Errorf("파트 %d 성공 전송 %d회: 이미 받은 파트는 다시 보내면 안 된다", n, count)
		}
	}
	got := sha256.Sum256(uploaded.Bytes())
	if hex.EncodeToString(got[:]) != sha || uploaded.Len() != len(data) || e.fake.receiptGets < 1 {
		t.Fatal("전체 파일 sha256·크기·실영수증 검증 실패")
	}
	local, err := os.ReadFile(filepath.Join(e.root, filepath.FromSlash(crashRelPath)))
	after, statErr := os.Stat(filepath.Join(e.root, filepath.FromSlash(crashRelPath)))
	if err != nil || statErr != nil || !bytes.Equal(local, data) || !before.ModTime().Equal(after.ModTime()) {
		t.Fatal("강제 종료·72시간 단절·재개 중 현장 원본을 변경하거나 지웠다")
	}
	t.Log("실제 자식 프로세스 강제 종료·SQLite 복구·72시간 논리 시각 단절·자동 재개·실영수증 검증 통과 (실제 72시간/전원 차단/15번 장비 시험 아님)")
}

// TestT03CrashRecoveryHelper는 위 수용시험이 시작한 자식에서만 에이전트를 실행한다.
func TestT03CrashRecoveryHelper(t *testing.T) {
	path := os.Getenv(crashChildEnv)
	if path == "" {
		return
	}
	raw, err := os.ReadFile(path)
	var spec crashChildSpec
	if err != nil || json.Unmarshal(raw, &spec) != nil {
		t.Fatal("자식 시험 설정을 읽지 못했습니다")
	}
	st, err := state.Open(spec.Dir)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = st.Close() }()
	cfg := &config.Config{ServerURL: spec.URL, WatchRoot: spec.Root, DeviceID: collector, MaxBackoffS: 1,
		ScanIntervalS: 1, PolicyRefreshS: 1}
	cfg.Defaults()
	hc, err := api.NewHTTPClient("", 20*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	client := api.New(spec.URL, hc, &api.Credentials{DeviceID: collector, ClientID: "synthetic", ClientSecret: "synthetic"}, "test")
	a := New(cfg, spec.Dir, client, st, slog.New(slog.NewTextHandler(io.Discard, nil)), "test")
	clock := spec.Start
	a.Now = func() time.Time { return clock }
	st.Now = a.Now
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	policyErr := a.RefreshPolicy(ctx)
	if spec.Phase == "upload" {
		if policyErr != nil {
			t.Fatal(policyErr)
		}
		if _, err := a.Scan(ctx); err != nil {
			t.Fatal(err)
		}
		clock = clock.Add(2 * time.Second)
		if n, err := a.Scan(ctx); err != nil || n != 1 {
			t.Fatal("쓰기 안정 판정 실패", n, err)
		}
		_, err := a.ProcessOnce(ctx)
		t.Fatalf("파트 전송 중 강제 종료 전에 실행이 끝났다: %v", err)
	}
	if !errors.Is(policyErr, api.ErrOffline) || a.Policy() == nil {
		t.Fatal("단절된 서버 대신 SQLite 정책으로 시작해야 한다", policyErr)
	}
	for hour := 0; hour <= 72; hour++ {
		clock = spec.Start.Add(time.Duration(hour) * time.Hour)
		if n, err := a.ProcessOnce(ctx); n != 1 || !errors.Is(err, api.ErrOffline) {
			t.Fatalf("모의 단절 %d시간 재시도 실패: n=%d, err=%v", hour, n, err)
		}
		if _, err := a.Heartbeat(ctx); !errors.Is(err, api.ErrOffline) {
			t.Fatalf("모의 단절 하트비트가 성공했다: %v", err)
		}
		f, err := st.Get(ctx, crashRelPath)
		if err != nil || f == nil || f.Status != state.Uploading || f.UploadID != "upl1" ||
			f.Attempts != hour+1 || !f.NextAttemptAt.After(clock) || f.NextAttemptAt.Sub(clock) > time.Second {
			t.Fatalf("단절 중 대기열·백오프가 손상됐다: %+v, %v", f, err)
		}
	}
	f, _ := st.Get(ctx, crashRelPath)
	heartbeat, _ := st.KV(ctx, KeyLastHeartbeat)
	proof := offlineProof{Start: spec.Start, End: clock, Attempts: f.Attempts, UploadID: f.UploadID, SHA: f.SHA256, Heartbeat: heartbeat}
	raw, err = json.Marshal(proof)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(spec.Ready+".tmp", raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(spec.Ready+".tmp", spec.Ready); err != nil {
		t.Fatal(err)
	}
	// 실제 루프의 초 단위 백오프는 그대로 실행한다. 논리 시각만 72시간 이후에서 흐른다.
	base, realStart := clock, time.Now()
	a.Now = func() time.Time { return base.Add(time.Since(realStart)) }
	st.Now = a.Now
	ran := make(chan error, 1)
	go func() { ran <- a.Run(ctx) }()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		f, err := st.Get(ctx, crashRelPath)
		if err != nil || f == nil || f.Status == state.Failed {
			t.Fatalf("자동 재개 상태 오류: %+v, %v", f, err)
		}
		if f.Status == state.Done {
			cancel()
			if err := <-ran; err != nil {
				t.Fatal(err)
			}
			if n, err := a.ProcessOnce(context.Background()); n != 0 || err != nil {
				t.Fatal("영수증 있는 파일을 다시 보냈다", n, err)
			}
			return
		}
		select {
		case err := <-ran:
			t.Fatalf("자동 재개 전에 루프 종료: %v", err)
		case <-ctx.Done():
			t.Fatal("자동 재개 시간 초과")
		case <-ticker.C:
		}
	}
}
