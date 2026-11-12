package core

// 가짜 플랫폼 API(httptest)로 에이전트 흐름을 시험한다. 실제 스택 시험은 docs 참고 (T02·T03 수동 절차).

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/config"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
)

const (
	collector = "dev_00000000000000000000000301"
	camera    = "dev_00000000000000000000000302"
	sensorDev = "dev_00000000000000000000000303"
)

type fakeUpload struct {
	manifest  map[string]any
	partSize  int64
	partCount int
	parts     map[int][]byte
	completed bool
	status    string
}

type fakeServer struct {
	t  *testing.T
	mu sync.Mutex
	*httptest.Server
	policy      api.Policy
	revoked     bool
	uploads     map[string]*fakeUpload
	bySHA       map[string]string // sha → upload id (등록 완료)
	putCount    map[string]int    // "upl/part" → 전송 횟수
	failPutOnce map[int]bool      // 파트 번호 → 한 번 실패
	heartbeats  []map[string]any
	seq         int
	receipts    map[string]api.Receipt
	receiptGets int
	idemBody    map[string]string // Idempotency-Key → 본문 해시 (실제 서버의 run_idempotent처럼 다른 본문이면 409)
	// override가 true를 돌려주면 그 요청에는 override가 응답한 것이다 (시험별 응답 주입, f.mu를 잡은 채 부른다)
	override func(w http.ResponseWriter, r *http.Request) bool
}

func newFake(t *testing.T) *fakeServer {
	f := &fakeServer{t: t, uploads: map[string]*fakeUpload{}, bySHA: map[string]string{}, putCount: map[string]int{},
		failPutOnce: map[int]bool{}, receipts: map[string]api.Receipt{}, idemBody: map[string]string{}}
	f.policy = api.Policy{PolicyVersion: "pv-1", CollectorDeviceID: collector, Timezone: "Asia/Seoul",
		HeartbeatIntervalS: 60, SizeStableS: 1, MaxParallelParts: 2,
		Sources: []api.Source{{DeviceID: camera, CollectionMode: "C3_APPROVED_FULL", AllowedDataRoles: []string{"CORE"}},
			{DeviceID: sensorDev, CollectionMode: "C3_APPROVED_FULL"}},
		WatchRules: []api.WatchRule{
			{RuleID: "cam", SourceDeviceID: camera, PathPattern: "{session}/cam01/*", Include: []string{"*.mp4"},
				DataType: "VIDEO", Variant: "ORIGINAL", DataRoleDefault: "CORE", Priority: 1},
			{RuleID: "csv", SourceDeviceID: sensorDev, PathPattern: "{session}/sensor/*", Include: []string{"*.csv"},
				DataType: "SENSOR", Variant: "ORIGINAL", DataRoleDefault: "CORE", Priority: 2}}}
	f.Server = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.Close)
	return f
}

func (f *fakeServer) json(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (f *fakeServer) handle(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.override != nil && f.override(w, r) {
		return
	}
	p := r.URL.Path
	if p == "/v1/auth/token" {
		if f.revoked {
			f.json(w, 401, map[string]any{"status": 401, "code": "UNAUTHENTICATED"})
			return
		}
		f.json(w, 200, map[string]any{"access_token": "tok", "token_type": "Bearer", "expires_in": 300})
		return
	}
	if strings.HasPrefix(p, "/s3/") { // 사전 서명 URL 자리 (스토리지 직송)
		var n int
		parts := strings.Split(strings.TrimPrefix(p, "/s3/"), "/")
		id := parts[0]
		_, _ = fmt.Sscanf(parts[1], "%d", &n)
		body, _ := io.ReadAll(r.Body)
		if f.failPutOnce[n] {
			delete(f.failPutOnce, n)
			hj, _ := w.(http.Hijacker)
			conn, _, _ := hj.Hijack() // 연결을 끊어 네트워크 단절을 흉내 낸다
			_ = conn.Close()
			return
		}
		f.putCount[fmt.Sprintf("%s/%d", id, n)]++
		f.uploads[id].parts[n] = body
		w.Header().Set("ETag", fmt.Sprintf("\"etag-%d\"", n))
		w.WriteHeader(200)
		return
	}
	if r.Header.Get("Authorization") != "Bearer tok" || f.revoked {
		f.json(w, 401, map[string]any{"status": 401, "code": "UNAUTHENTICATED"})
		return
	}
	switch {
	case strings.HasPrefix(p, "/v1/assets/") && strings.HasSuffix(p, "/receipt"):
		f.receiptGets++
		assetID := strings.Split(p, "/")[3]
		if receipt, ok := f.receipts[assetID]; ok {
			f.json(w, 200, receipt)
		} else {
			f.json(w, 404, map[string]any{"status": 404, "code": "NOT_FOUND"})
		}
	case p == "/v1/devices/"+collector+"/policy":
		f.json(w, 200, f.policy)
	case p == "/v1/agents/heartbeat":
		var hb map[string]any
		_ = json.NewDecoder(r.Body).Decode(&hb)
		f.heartbeats = append(f.heartbeats, hb)
		w.WriteHeader(204)
	case p == "/v1/uploads" && r.Method == "POST":
		var m map[string]any
		_ = json.NewDecoder(r.Body).Decode(&m)
		if key := r.Header.Get("Idempotency-Key"); key != "" {
			// 실제 서버(backend/app/services/idempotency.py)와 같게: 같은 키로 다른 본문이 오면 409 IDEMPOTENCY_CONFLICT.
			canon, _ := json.Marshal(m)
			sum := sha256.Sum256(canon)
			h := hex.EncodeToString(sum[:])
			if prev, seen := f.idemBody[key]; seen && prev != h {
				f.json(w, 409, map[string]any{"status": 409, "code": "IDEMPOTENCY_CONFLICT"})
				return
			}
			f.idemBody[key] = h
		}
		file := m["file"].(map[string]any)
		sha := file["checksum_sha256"].(string)
		if _, ok := f.bySHA[sha]; ok {
			f.json(w, 200, map[string]any{"status": "ALREADY_REGISTERED", "asset_id": "ast_x"})
			return
		}
		size := int64(file["byte_size"].(float64))
		f.seq++
		id := fmt.Sprintf("upl%d", f.seq)
		const partSize = 1 << 20
		count := int((size + partSize - 1) / partSize)
		if count == 0 {
			count = 1
		}
		f.uploads[id] = &fakeUpload{manifest: m, partSize: partSize, partCount: count, parts: map[int][]byte{}}
		f.json(w, 201, map[string]any{"status": "UPLOAD_CREATED", "upload_id": id, "asset_id": "ast_" + id,
			"part_size": partSize, "part_count": count, "parts": f.urls(id, 1, count)})
	case strings.HasSuffix(p, "/parts") && r.Method == "GET":
		id := strings.Split(p, "/")[3]
		u := f.uploads[id]
		if u.completed {
			f.json(w, 409, map[string]any{"status": 409, "code": "CONFLICT"})
			return
		}
		var done []map[string]any
		var pending []api.PartURL
		for n := 1; n <= u.partCount; n++ {
			if _, ok := u.parts[n]; ok {
				done = append(done, map[string]any{"part_number": n, "etag": fmt.Sprintf("\"etag-%d\"", n), "size": len(u.parts[n])})
			} else {
				pending = append(pending, f.urls(id, n, n)...)
			}
		}
		f.json(w, 200, map[string]any{"upload_id": id, "part_size": u.partSize, "part_count": u.partCount,
			"completed_parts": done, "pending_parts": pending, "expires_at": "2026-12-01T00:00:00Z"})
	case strings.HasSuffix(p, "/complete"):
		id := strings.Split(p, "/")[3]
		u := f.uploads[id]
		var buf bytes.Buffer
		for n := 1; n <= u.partCount; n++ {
			buf.Write(u.parts[n])
		}
		sum := sha256.Sum256(buf.Bytes())
		if hex.EncodeToString(sum[:]) != u.manifest["file"].(map[string]any)["checksum_sha256"] {
			f.json(w, 409, map[string]any{"status": 409, "code": "CONFLICT"})
			return
		}
		u.completed = true
		u.status = "REGISTERED" // 기본 모의 워커는 해시 검증 직후 등록한다.
		f.receipts["ast_"+id] = fakeReceipt("ast_"+id, hex.EncodeToString(sum[:]), int64(buf.Len()))
		f.bySHA[hex.EncodeToString(sum[:])] = id
		f.json(w, 202, map[string]any{"upload_id": id, "asset_id": "ast_" + id, "ingest_status": "RECEIVED"})
	case strings.HasPrefix(p, "/v1/uploads/") && r.Method == "GET":
		id := strings.Split(p, "/")[3]
		if u, ok := f.uploads[id]; ok {
			status := u.status
			if status == "" {
				status = "UPLOADING"
			}
			f.json(w, 200, map[string]any{"upload_id": id, "asset_id": "ast_" + id, "ingest_status": status})
		} else {
			f.json(w, 404, map[string]any{"status": 404, "code": "NOT_FOUND"})
		}
	default:
		f.json(w, 404, map[string]any{"status": 404, "code": "NOT_FOUND"})
	}
}

func fakeReceipt(assetID, sha string, size int64) api.Receipt {
	entry := sha256.Sum256([]byte(assetID + sha))
	return api.Receipt{AssetID: assetID, ChecksumSHA256: sha, ByteSize: &size,
		RegisteredAt: "2026-11-04T00:00:00Z", IntegrityLogEntryID: "1", IntegrityEntryHash: hex.EncodeToString(entry[:])}
}

func (f *fakeServer) urls(id string, from, to int) []api.PartURL {
	var out []api.PartURL
	for n := from; n <= to; n++ {
		out = append(out, api.PartURL{PartNumber: n, URL: fmt.Sprintf("%s/s3/%s/%d", f.URL, id, n)})
	}
	return out
}

type env struct {
	fake  *fakeServer
	dir   string
	root  string
	clock time.Time
}

func newEnv(t *testing.T) *env {
	e := &env{fake: newFake(t), dir: t.TempDir(), root: t.TempDir(), clock: time.Now()}
	return e
}

func (e *env) agent(t *testing.T) *Agent {
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
	if err := a.RefreshPolicy(context.Background()); err != nil {
		t.Fatal(err)
	}
	return a
}

func (e *env) write(t *testing.T, rel string, data []byte) {
	p := filepath.Join(e.root, filepath.FromSlash(rel))
	_ = os.MkdirAll(filepath.Dir(p), 0o755)
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

// 쓰기 완료 판정: 처음 본 스캔에서는 넣지 않고, size_stable_s가 지난 다음 스캔에서 넣는다.
func (e *env) scanStable(t *testing.T, a *Agent) int {
	ctx := context.Background()
	if n, _ := a.Scan(ctx); n != 0 {
		t.Fatalf("처음 본 파일을 바로 넣으면 안 된다 (%d)", n)
	}
	e.clock = e.clock.Add(2 * time.Second)
	n, err := a.Scan(ctx)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// covers: T02-a
func TestT02FolderToServerWithoutHumanIntervention(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	csv := bytes.Repeat([]byte("2026-11-01T00:00:00Z,18.4\n"), 100_000) // 약 2.6MB → 파트 3개
	e.write(t, "A-2026-11-01/sensor/wq.csv", csv)
	e.write(t, "A-2026-11-01/sensor/skip.tmp", []byte("x"))
	if n := e.scanStable(t, a); n != 1 {
		t.Fatalf("대기열 추가 %d", n)
	}
	if _, err := a.ProcessOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	e.fake.mu.Lock()
	defer e.fake.mu.Unlock()
	u := e.fake.uploads["upl1"]
	if u == nil || !u.completed || u.partCount != 3 {
		t.Fatalf("업로드가 끝나지 않았다: %+v", u)
	}
	m := u.manifest
	if m["session_label"] != "A-2026-11-01" || m["watch_rule_id"] != "csv" || m["source_system_id"] != sensorDev {
		t.Fatalf("매니페스트 %v", m)
	}
	if _, ok := m["capture_start_at"]; ok {
		t.Fatal("근거 없는 촬영시각을 넣었다 (파일 수정 시각 사용 금지)")
	}
	if m["timestamp_quality"].(map[string]any)["source"] != "UNKNOWN" {
		t.Fatal("timestamp_quality는 UNKNOWN이어야 한다")
	}
	file := m["file"].(map[string]any)
	if file["original_relative_path"] != "A-2026-11-01/sensor/wq.csv" || file["original_filename"] != "wq.csv" {
		t.Fatalf("원래 파일명·상대 경로 %v", file)
	}
	if _, err := os.Stat(filepath.Join(e.root, "A-2026-11-01/sensor/wq.csv")); err != nil {
		t.Fatal("원본을 지우면 안 된다")
	}
}

func TestT03ResumeOnlyRemainingPartsAfterRestart(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	data := bytes.Repeat([]byte("0123456789abcdef"), 4<<20/16) // 4MB → 파트 4개
	e.write(t, "S1/sensor/big.csv", data)
	e.scanStable(t, a)
	e.fake.mu.Lock()
	e.fake.failPutOnce[3] = true // 3번 파트 전송 중 연결 끊김
	e.fake.policy.MaxParallelParts = 1
	e.fake.mu.Unlock()
	_ = a.RefreshPolicy(context.Background())
	if _, err := a.ProcessOnce(context.Background()); err == nil {
		t.Fatal("끊김이 오류로 보고되어야 한다")
	}
	f, _ := a.Store.Get(context.Background(), "S1/sensor/big.csv")
	if f.Status != state.Uploading || f.UploadID == "" {
		t.Fatalf("업로드 상태가 남아 있어야 한다: %+v", f)
	}
	// 에이전트 재시작 (같은 Local_State)
	_ = a.Store.Close()
	a2 := e.agent(t)
	_, _ = a2.Store.Get(context.Background(), "S1/sensor/big.csv")
	e.clock = e.clock.Add(time.Hour) // 백오프 대기 경과
	if _, err := a2.ProcessOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	e.fake.mu.Lock()
	defer e.fake.mu.Unlock()
	if !e.fake.uploads["upl1"].completed {
		t.Fatal("재개 후 완료되지 않았다")
	}
	for n := 1; n <= 4; n++ {
		if c := e.fake.putCount[fmt.Sprintf("upl1/%d", n)]; c != 1 {
			t.Errorf("파트 %d 전송 %d회 (남은 파트만 한 번씩 보내야 한다)", n, c)
		}
	}
	if len(e.fake.uploads) != 1 {
		t.Fatal("새 업로드 세션을 만들면 안 된다")
	}
}

func TestAlreadyRegisteredIsNotResent(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	data := []byte("time,temp\n2026-11-01T00:00:00Z,1\n")
	sum := sha256.Sum256(data)
	e.fake.bySHA[hex.EncodeToString(sum[:])] = "old"
	e.fake.receipts["ast_x"] = fakeReceipt("ast_x", hex.EncodeToString(sum[:]), int64(len(data)))
	e.write(t, "S1/sensor/a.csv", data)
	e.scanStable(t, a)
	_, _ = a.ProcessOnce(context.Background())
	f, _ := a.Store.Get(context.Background(), "S1/sensor/a.csv")
	if f.Status != state.Done || len(e.fake.uploads) != 0 {
		t.Fatalf("ALREADY_REGISTERED는 완료로 기록: %+v", f)
	}
	// 다시 스캔해도 대기열에 넣지 않는다
	e.clock = e.clock.Add(time.Minute)
	if n, _ := a.Scan(context.Background()); n != 0 {
		t.Fatal("완료한 파일을 다시 넣었다")
	}
}

// covers: T02-c
func TestPolicyChangeExcludesQueuedFilesAndReports(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	e.write(t, "S1/cam01/a.mp4", []byte("\x00\x00\x00\x18ftypmp42 not really a video"))
	e.scanStable(t, a)
	e.fake.mu.Lock()
	e.fake.policy.PolicyVersion = "pv-2"
	e.fake.policy.Sources[0].CollectionMode = "C2_EVENT_CLIPS" // 원본을 더 이상 받지 않는다
	e.fake.mu.Unlock()
	if err := a.RefreshPolicy(context.Background()); err != nil {
		t.Fatal(err)
	}
	f, _ := a.Store.Get(context.Background(), "S1/cam01/a.mp4")
	if f.Status != state.Excluded || f.LastError != "POLICY_VARIANT_NOT_ALLOWED" {
		t.Fatalf("정책 제외가 되어야 한다: %+v", f)
	}
	hb, err := a.Heartbeat(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if hb["policy_excluded"] != 1 || hb["policy_version"] != "pv-2" {
		t.Fatalf("하트비트 %v", hb)
	}
}

func TestRevokedCredentialStopsButKeepsQueue(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	e.write(t, "S1/sensor/a.csv", []byte("t,v\n"))
	e.scanStable(t, a)
	e.fake.mu.Lock()
	e.fake.revoked = true
	e.fake.mu.Unlock()
	_, err := a.ProcessOnce(context.Background())
	if err != api.ErrRevoked {
		t.Fatalf("ErrRevoked가 아니다: %v", err)
	}
	if v, _ := a.Store.KV(context.Background(), KeyReenroll); v != "true" {
		t.Fatal("재등록 필요 표시가 없다")
	}
	st, _ := a.Store.Stats(context.Background())
	if st.QueueItems != 1 {
		t.Fatal("대기열을 유지해야 한다")
	}
}

func TestTransferWindowClosedPausesUpload(t *testing.T) {
	e := newEnv(t)
	e.fake.policy.TransferWindows = []api.TransferWindow{{Days: []string{"MON"}, StartLocal: "01:00", EndLocal: "01:01"}}
	a := e.agent(t)
	e.clock = time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC) // 수요일
	e.write(t, "S1/sensor/a.csv", []byte("t,v\n"))
	e.scanStable(t, a)
	if n, _ := a.ProcessOnce(context.Background()); n != 0 || len(e.fake.uploads) != 0 {
		t.Fatal("허용 시간대 밖에서는 보내면 안 된다")
	}
}

// covers: T02-b
// 쓰기 중인 파일(크기가 계속 바뀜)은 크기가 size_stable_s 동안 그대로일 때까지 대기열에 넣지 않는다.
func TestWritingFileIsNotQueuedUntilSizeStable(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	ctx := context.Background()
	chunk := bytes.Repeat([]byte("2026-11-01T00:00:00Z,18.4\n"), 1000)
	data := append([]byte{}, chunk...)
	e.write(t, "S1/sensor/growing.csv", data)
	for i := 0; i < 4; i++ { // 스캔 사이마다 계속 쓴다
		if n, err := a.Scan(ctx); err != nil || n != 0 {
			t.Fatalf("쓰기 중인 파일을 넣었다 (스캔 %d, n=%d, err=%v)", i, n, err)
		}
		e.clock = e.clock.Add(2 * time.Second)
		data = append(data, chunk...)
		e.write(t, "S1/sensor/growing.csv", data)
	}
	if n, _ := a.Scan(ctx); n != 0 { // 마지막 쓰기 직후 첫 관찰
		t.Fatalf("크기가 막 바뀐 파일을 넣었다 (%d)", n)
	}
	e.clock = e.clock.Add(2 * time.Second)
	if n, err := a.Scan(ctx); err != nil || n != 1 {
		t.Fatalf("크기가 안정된 뒤에는 넣어야 한다 (n=%d, err=%v)", n, err)
	}
	f, _ := a.Store.Get(ctx, "S1/sensor/growing.csv")
	if f.Size != int64(len(data)) {
		t.Fatalf("최종 크기로 넣어야 한다: %d != %d", f.Size, len(data))
	}
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	e.fake.mu.Lock()
	defer e.fake.mu.Unlock()
	if len(e.fake.uploads) != 1 {
		t.Fatalf("업로드 %d건 (1건이어야 함)", len(e.fake.uploads))
	}
}
