// Package core는 동기화 에이전트 본체다: 감시 → 쓰기 완료 판정 → 해시·매니페스트 → 업로드·재개 → 하트비트.
//
// 원칙 (06번 요구사항 8, domain.md 9장):
//   - 감시 폴더의 원본을 지우거나 옮기거나 고치지 않는다. 삭제 옵션도 없다.
//   - 촬영시각은 파일 내부 메타데이터에서만 얻는다. 파일 수정 시각은 쓰지 않는다.
package core

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/fsnotify/fsnotify"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/config"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/mediameta"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/rules"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/sched"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/sensor"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/sysinfo"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/timex"
)

const SchemaVersion = "1.0.0"

// 로컬 상태 kv 키
const (
	KeyPolicy        = "policy"
	KeyReenroll      = "reenroll_required"
	KeyLastError     = "last_error_code"
	KeyLastHeartbeat = "last_heartbeat"
	KeyLastSuccess   = "last_success_at"
	KeyConnected     = "connected"
)

type Agent struct {
	Cfg     *config.Config
	Dir     string
	Client  *api.Client
	Store   *state.Store
	Log     *slog.Logger
	Version string
	// Now는 시험에서 바꿀 수 있다.
	Now func() time.Time
	// OnReady는 로컬 상태 확인 뒤 한 번 호출한다. 오프라인도 정상 기동이므로 서버 연결과 분리한다.
	OnReady func()

	policy   atomic.Pointer[api.Policy]
	limiter  *sched.Limiter
	wake     chan struct{}
	lastErr  atomic.Value // string
	reenroll atomic.Bool

	mu         sync.Mutex
	candidates map[string]candidate
	plugins    sync.Map // 이름 → 상태(OK/DEGRADED/FAILED)
}

type candidate struct {
	size, mtime int64
	stableSince time.Time
}

func New(cfg *config.Config, dir string, client *api.Client, store *state.Store, log *slog.Logger, version string) *Agent {
	a := &Agent{Cfg: cfg, Dir: dir, Client: client, Store: store, Log: log, Version: version, Now: time.Now,
		limiter: sched.NewLimiter(), wake: make(chan struct{}, 1), candidates: map[string]candidate{}}
	a.lastErr.Store("")
	return a
}

func (a *Agent) Policy() *api.Policy { return a.policy.Load() }

func (a *Agent) setErr(ctx context.Context, code string) {
	a.lastErr.Store(code)
	_ = a.Store.SetKV(ctx, KeyLastError, code)
}

// ---------------------------------------------------------------- 정책

// 정책 숫자 항목의 계약 범위 (contracts/schemas/device-policy.schema.json의 minimum·maximum, ADR-0040).
// 상한은 제품 한도가 아니라 time.Duration·동시 실행 수 계산이 넘치지 않는 기술 한도다.
const (
	minHeartbeatIntervalS = 30
	maxPolicySeconds      = timex.MaxSeconds // heartbeat_interval_s·size_stable_s
	maxParallelParts      = 10000            // 업로드 파트 번호 상한(openapi.yaml PartUrl.part_number)과 같다
	defaultParallelParts  = 4
	maxPolicyInt32        = math.MaxInt32 // transfer_windows[].max_kbps·watch_rules[].priority
)

// CodePolicyInvalid는 계약 범위를 벗어난 정책을 거부했을 때 하트비트 last_error_code로 보고하는 코드다.
const CodePolicyInvalid = "POLICY_INVALID"

// ErrPolicyInvalid는 정책의 숫자 값이 계약 범위를 벗어났다는 뜻이다. 그 정책은 저장·적용하지 않는다.
var ErrPolicyInvalid = errors.New("정책 값이 계약 범위를 벗어났습니다")

// checkPolicy는 에이전트가 시간·동시 전송 수·대역폭·규칙 순위로 쓰는 숫자가 계약 범위 안인지 본다.
func checkPolicy(p *api.Policy) error {
	type field struct {
		path            string
		value, low, max int
	}
	fields := []field{
		{"/heartbeat_interval_s", p.HeartbeatIntervalS, minHeartbeatIntervalS, maxPolicySeconds},
		{"/size_stable_s", p.SizeStableS, 1, maxPolicySeconds},
		{"/max_parallel_parts", p.MaxParallelParts, 1, maxParallelParts},
	}
	for i, w := range p.TransferWindows {
		if w.MaxKbps != nil { // null은 제한 없음
			fields = append(fields, field{fmt.Sprintf("/transfer_windows/%d/max_kbps", i), *w.MaxKbps, 1, maxPolicyInt32})
		}
	}
	for i, r := range p.WatchRules {
		fields = append(fields, field{fmt.Sprintf("/watch_rules/%d/priority", i), r.Priority, 0, maxPolicyInt32})
	}
	for _, f := range fields {
		if f.value < f.low || f.value > f.max {
			return fmt.Errorf("%w: %s=%d (범위 %d~%d)", ErrPolicyInvalid, f.path, f.value, f.low, f.max)
		}
	}
	return nil
}

// RefreshPolicy는 서버 정책을 받아 적용한다. 실패하면 마지막으로 저장한 정책을 쓴다 (오프라인 시작).
// 숫자 값이 계약 범위를 벗어난 정책(Go 정수에 담기지 않는 수 포함)은 저장·적용하지 않고 이전 정책을 그대로 쓰며,
// 하트비트 last_error_code로 POLICY_INVALID를 보고한다 (ADR-0040).
func (a *Agent) RefreshPolicy(ctx context.Context) error {
	p := api.Policy{MaxParallelParts: defaultParallelParts} // 선택 필드 생략과 명시적 0을 구분한다
	status, err := a.Client.Do(ctx, "GET", "/v1/devices/"+a.Cfg.DeviceID+"/policy", nil, nil, &p)
	var typeErr *json.UnmarshalTypeError
	var problem *api.Problem
	switch {
	case err == nil:
		err = checkPolicy(&p)
	case status/100 == 2 && errors.As(err, &typeErr): // 응답은 받았지만 값이 Go 형에 담기지 않는다 (int64를 넘는 수 등)
		err = fmt.Errorf("%w: %v", ErrPolicyInvalid, err)
	case status == http.StatusUnprocessableEntity && errors.As(err, &problem) && problem.Code == "RULE_VIOLATION":
		err = fmt.Errorf("%w: %v", ErrPolicyInvalid, problem)
	}
	if errors.Is(err, ErrPolicyInvalid) {
		a.setErr(ctx, CodePolicyInvalid)
		a.Log.Error("서버 정책 값이 계약 범위를 벗어나 적용하지 않습니다. 이전 정책을 계속 씁니다",
			"error", err.Error(), "policy_version", p.PolicyVersion)
	}
	if err != nil {
		if a.policy.Load() == nil {
			a.useCachedPolicy(ctx)
		}
		return err
	}
	b, _ := json.Marshal(p)
	_ = a.Store.SetKV(ctx, KeyPolicy, string(b))
	a.applyPolicy(ctx, &p)
	return nil
}

// useCachedPolicy는 마지막으로 저장한 정책을 적용한다. 예전 버전이 범위 검사 없이 저장했을 수 있어 다시 검사한다.
func (a *Agent) useCachedPolicy(ctx context.Context) {
	cached, _ := a.Store.KV(ctx, KeyPolicy)
	p := api.Policy{MaxParallelParts: defaultParallelParts}
	if cached == "" || json.Unmarshal([]byte(cached), &p) != nil {
		return
	}
	if err := checkPolicy(&p); err != nil {
		a.setErr(ctx, CodePolicyInvalid)
		a.Log.Error("저장된 정책 값이 계약 범위를 벗어나 쓰지 않습니다", "error", err.Error(), "policy_version", p.PolicyVersion)
		return
	}
	a.applyPolicy(ctx, &p)
	a.Log.Warn("서버 정책을 받지 못했거나 쓸 수 없어 저장된 정책을 씁니다", "policy_version", p.PolicyVersion)
}

func (a *Agent) applyPolicy(ctx context.Context, p *api.Policy) {
	if p.SizeStableS <= 0 {
		p.SizeStableS = 30
	}
	if p.HeartbeatIntervalS <= 0 {
		p.HeartbeatIntervalS = 300
	}
	if p.MaxParallelParts <= 0 {
		p.MaxParallelParts = defaultParallelParts
	}
	old := a.policy.Swap(p)
	if old == nil || old.PolicyVersion != p.PolicyVersion {
		a.Log.Info("정책 적용", "policy_version", p.PolicyVersion, "rules", len(p.WatchRules), "sources", len(p.Sources))
	}
	// 정책이 바뀌어 더 이상 허용되지 않는 대기 파일은 보내지 않는다 (06번 요구사항 2.4)
	n, err := a.Store.ExcludeDisallowed(ctx, func(f *state.File) (bool, string) {
		ok, reason := a.allowed(p, f)
		return ok, reason
	})
	if err == nil && n > 0 {
		a.Log.Warn("정책 변경으로 대기 파일을 제외했습니다", "count", n)
	}
}

func findRule(p *api.Policy, id string) *api.WatchRule {
	for i := range p.WatchRules {
		if p.WatchRules[i].RuleID == id {
			return &p.WatchRules[i]
		}
	}
	return nil
}

func dataTypeOf(rule *api.WatchRule, name string) string {
	if rule.DataType != "" {
		return rule.DataType
	}
	m := rules.MimeType(name)
	switch {
	case strings.HasPrefix(m, "video/"):
		return "VIDEO"
	case strings.HasPrefix(m, "image/"):
		return "IMAGE"
	case m == "text/csv":
		return "SENSOR"
	case strings.HasPrefix(m, "text/"):
		return "LOG"
	default:
		return "DOCUMENT"
	}
}

func variantOf(rule *api.WatchRule) string {
	if rule.Variant != "" {
		return rule.Variant
	}
	return "ORIGINAL"
}

func (a *Agent) allowed(p *api.Policy, f *state.File) (bool, string) {
	rule := findRule(p, f.RuleID)
	if rule == nil || rule.SourceDeviceID != f.SourceDeviceID {
		return false, "POLICY_RULE_REMOVED"
	}
	src := p.Source(f.SourceDeviceID)
	if src == nil {
		return false, "POLICY_SOURCE_REMOVED"
	}
	if src.CollectionMode != "C0_METADATA_ONLY" &&
		!rules.VariantAllowed(src.CollectionMode, dataTypeOf(rule, f.RelPath), variantOf(rule)) {
		return false, "POLICY_VARIANT_NOT_ALLOWED"
	}
	if len(src.AllowedDataRoles) > 0 && !contains(src.AllowedDataRoles, rule.DataRoleDefault) {
		return false, "POLICY_DATA_ROLE_NOT_ALLOWED"
	}
	return true, ""
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- 감시

// Scan은 감시 루트를 훑어 쓰기 완료된 파일을 대기열에 넣는다. 첫 실행에는 기존 파일도 대상이다 (요구사항 2.3).
func (a *Agent) Scan(ctx context.Context) (int, error) {
	p := a.policy.Load()
	if p == nil {
		return 0, errors.New("정책이 없습니다")
	}
	now := a.Now()
	stable := timex.Seconds(int64(p.SizeStableS)) // 정책 값과 별개로 변환도 넘치지 않게 한다 (ADR-0040)
	seen := map[string]bool{}
	added := 0
	err := filepath.WalkDir(a.Cfg.WatchRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // 읽을 수 없는 폴더는 건너뛴다
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if d.IsDir() || !d.Type().IsRegular() {
			return nil
		}
		rel, err := filepath.Rel(a.Cfg.WatchRoot, path)
		if err != nil {
			return nil
		}
		rel = filepath.ToSlash(rel)
		m, ok := rules.First(rel, p.WatchRules)
		if !ok {
			return nil
		}
		src := p.Source(m.Rule.SourceDeviceID)
		if src == nil {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		seen[rel] = true
		size, mtime := info.Size(), info.ModTime().UnixNano()
		a.mu.Lock()
		c, known := a.candidates[rel]
		if !known || c.size != size || c.mtime != mtime {
			a.candidates[rel] = candidate{size: size, mtime: mtime, stableSince: now}
			a.mu.Unlock()
			return nil
		}
		a.mu.Unlock()
		// 크기가 정해진 시간 동안 변하지 않고, 읽기로 열 수 있어야 쓰기 완료로 본다 (요구사항 3.1)
		if now.Sub(c.stableSince) < stable || !readable(path) {
			return nil
		}
		ok, err = a.Store.Enqueue(ctx, state.File{RelPath: rel, RuleID: m.Rule.RuleID,
			SourceDeviceID: m.Rule.SourceDeviceID, SessionLabel: m.SessionLabel, Size: size, MtimeNs: mtime,
			Priority: rules.Priority(src.CollectionMode, variantOf(&m.Rule))})
		if err != nil {
			return err
		}
		if ok {
			added++
			a.Log.Info("전송 대기열에 추가", "path", rel, "rule", m.Rule.RuleID, "bytes", size)
			if allowed, reason := a.allowed(p, &state.File{RelPath: rel, RuleID: m.Rule.RuleID,
				SourceDeviceID: m.Rule.SourceDeviceID}); !allowed {
				if f, _ := a.Store.Get(ctx, rel); f != nil {
					_ = a.Store.MarkStatus(ctx, f.ID, state.Excluded, reason)
				}
			}
		}
		return nil
	})
	a.mu.Lock()
	for k := range a.candidates {
		if !seen[k] {
			delete(a.candidates, k)
		}
	}
	a.mu.Unlock()
	if added > 0 {
		a.kick()
	}
	return added, err
}

func readable(path string) bool {
	f, err := os.Open(path) // Windows에서는 쓰기 중인 파일이 공유 위반으로 열리지 않을 수 있다
	if err != nil {
		return false
	}
	_ = f.Close()
	return true
}

func (a *Agent) kick() {
	select {
	case a.wake <- struct{}{}:
	default:
	}
}

// ---------------------------------------------------------------- 업로드

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer func() { _ = f.Close() }() // 읽기 전용
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil { // 스트리밍 (요구사항 4.1)
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func (a *Agent) manifest(p *api.Policy, f *state.File, abs string) (map[string]any, string) {
	rule := findRule(p, f.RuleID)
	dtype, variant := dataTypeOf(rule, f.RelPath), variantOf(rule)
	idemSrc := strings.Join([]string{f.SourceDeviceID, f.SHA256, variant, f.SessionLabel, rule.RuleID}, "|")
	sum := sha256.Sum256([]byte(idemSrc))
	localID := "lc-" + hex.EncodeToString(sum[:])[:32]
	m := map[string]any{
		"schema_version": SchemaVersion, "source_system_id": f.SourceDeviceID, "watch_rule_id": rule.RuleID,
		"data_type": dtype, "variant": variant, "data_role": rule.DataRoleDefault,
		"file": map[string]any{"original_filename": filepath.Base(abs), "original_relative_path": f.RelPath,
			"byte_size": f.Size, "checksum_sha256": f.SHA256, "mime_type": rules.MimeType(abs)},
		"client": map[string]any{"kind": "AGENT", "version": a.Version, "local_collection_id": localID},
	}
	if f.SessionLabel != "" {
		m["session_label"] = f.SessionLabel
	}
	// 촬영시각: 파일 내부 메타데이터만 근거로 쓴다 (요구사항 4.2)
	tq := map[string]any{"source": "UNKNOWN", "verified": false}
	if dtype == "VIDEO" {
		if t, ok := mediameta.MP4CreationTime(abs); ok {
			m["capture_start_at"] = t.UTC().Format(time.RFC3339)
			tq = map[string]any{"source": "DEVICE", "verified": false}
		}
	}
	m["timestamp_quality"] = tq
	return m, localID
}

// ProcessOnce는 대기열에서 보낼 수 있는 파일을 처리한다. 처리한 파일 수를 돌려준다.
func (a *Agent) ProcessOnce(ctx context.Context) (int, error) {
	if a.reenroll.Load() {
		return 0, api.ErrRevoked
	}
	p := a.policy.Load()
	if p == nil {
		return 0, errors.New("정책이 없습니다")
	}
	w, ok := sched.Active(p.TransferWindows, p.Timezone, a.Now())
	if !ok {
		return 0, nil // 전송 허용 시간대 밖 (요구사항 7.2)
	}
	if w != nil {
		a.limiter.SetKbps(w.MaxKbps)
	} else {
		a.limiter.SetKbps(nil)
	}
	files, err := a.Store.Next(ctx, 20)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, f := range files {
		if ctx.Err() != nil {
			return n, ctx.Err()
		}
		err := a.processFile(ctx, p, f)
		n++
		switch {
		case err == nil:
			_ = a.Store.SetKV(ctx, KeyConnected, "true")
		case errors.Is(err, api.ErrRevoked):
			a.reenroll.Store(true)
			_ = a.Store.SetKV(ctx, KeyReenroll, "true")
			a.setErr(ctx, "CREDENTIAL_REVOKED")
			a.Log.Error("자격증명이 폐기되었습니다. 전송을 멈추고 대기열은 유지합니다. 재등록하세요")
			return n, err
		case errors.Is(err, api.ErrOffline):
			_ = a.Store.SetKV(ctx, KeyConnected, "false")
			a.setErr(ctx, "NETWORK_UNREACHABLE")
			_ = a.Store.Retry(ctx, f.ID, "NETWORK_UNREACHABLE", sched.Backoff(f.Attempts, a.maxBackoff()))
			return n, err // 연결이 없으면 나머지도 기다린다
		default:
			a.Log.Warn("파일 처리 실패", "path", f.RelPath, "error", err.Error())
		}
	}
	return n, nil
}

func (a *Agent) processFile(ctx context.Context, p *api.Policy, f *state.File) error {
	abs := filepath.Join(a.Cfg.WatchRoot, filepath.FromSlash(f.RelPath))
	st, err := os.Stat(abs)
	if err != nil {
		return a.Store.MarkStatus(ctx, f.ID, state.Failed, "LOCAL_FILE_MISSING")
	}
	if pending, err := a.Store.ReceiptPending(ctx, f.ID); err != nil {
		return err
	} else if pending {
		return a.confirmReceipt(ctx, f)
	}
	if f.Status == state.Uploading && f.UploadID != "" {
		return a.resume(ctx, p, f, abs)
	}
	if ok, reason := a.allowed(p, f); !ok {
		return a.Store.MarkStatus(ctx, f.ID, state.Excluded, reason)
	}
	if st.Size() != f.Size || st.ModTime().UnixNano() != f.MtimeNs {
		return a.Store.Reset(ctx, f.ID) // 대기 중에 바뀌었다 → 다음 스캔이 다시 판정
	}
	if f.SHA256 == "" {
		sum, err := hashFile(abs)
		if err != nil {
			return a.Store.MarkStatus(ctx, f.ID, state.Failed, "LOCAL_READ_ERROR")
		}
		f.SHA256 = sum
		if err := a.Store.SetHash(ctx, f.ID, sum); err != nil {
			return err
		}
	}
	m, localID := a.manifest(p, f, abs)
	var created api.UploadCreated
	status, err := a.Client.Do(ctx, "POST", "/v1/uploads", m, map[string]string{"Idempotency-Key": localID}, &created)
	if err != nil {
		return a.handleProblem(ctx, f, err)
	}
	switch created.Status {
	case "ALREADY_REGISTERED", "METADATA_ONLY_REGISTERED":
		a.Log.Info("서버에 이미 있거나 메타데이터만 등록", "path", f.RelPath, "status", created.Status, "http", status)
		if created.AssetID == "" {
			return a.Store.MarkStatus(ctx, f.ID, state.Failed, "REGISTRATION_ASSET_MISSING")
		}
		if src := p.Source(f.SourceDeviceID); src != nil && src.CollectionMode == "C0_METADATA_ONLY" {
			// C0는 바이너리를 수집하지 않으므로 수집 무결성 로그·영수증이 생기지 않는다.
			return a.Store.MarkDone(ctx, f.ID, created.AssetID)
		}
		if created.Status == "METADATA_ONLY_REGISTERED" {
			return a.Store.MarkStatus(ctx, f.ID, state.Failed, "UNEXPECTED_METADATA_ONLY")
		}
		return a.awaitReceipt(ctx, f, created.AssetID)
	case "UPLOAD_CREATED":
		if err := a.Store.MarkUploading(ctx, f.ID, created.UploadID, created.AssetID); err != nil {
			return err
		}
		f.UploadID, f.AssetID = created.UploadID, created.AssetID
		f.Status = state.Uploading
		var done []api.CompletedPart
		pending := created.Parts
		if len(pending) < created.PartCount { // 응답의 URL 수가 한도로 잘렸으면 목록을 다시 받는다
			return a.resume(ctx, p, f, abs)
		}
		return a.sendAndComplete(ctx, p, f, abs, created.PartSize, created.PartCount, done, pending)
	default:
		return fmt.Errorf("알 수 없는 응답 상태 %q", created.Status)
	}
}

func (a *Agent) resume(ctx context.Context, p *api.Policy, f *state.File, abs string) error {
	var pl api.PartList
	_, err := a.Client.Do(ctx, "GET", "/v1/uploads/"+f.UploadID+"/parts", nil, nil, &pl)
	if err != nil {
		var pr *api.Problem
		if errors.As(err, &pr) && (pr.Status == 410 || pr.Status == 404) {
			a.Log.Warn("업로드 세션이 만료되어 처음부터 다시 보냅니다", "path", f.RelPath)
			return a.Store.Reset(ctx, f.ID)
		}
		if errors.As(err, &pr) && pr.Status == 409 { // 이미 완료된 세션
			return a.awaitReceipt(ctx, f, f.AssetID)
		}
		return a.handleProblem(ctx, f, err)
	}
	a.Log.Info("업로드 재개", "path", f.RelPath, "completed_parts", len(pl.CompletedParts), "pending", len(pl.PendingParts))
	return a.sendAndComplete(ctx, p, f, abs, pl.PartSize, pl.PartCount, pl.CompletedParts, pl.PendingParts)
}

func (a *Agent) sendAndComplete(ctx context.Context, p *api.Policy, f *state.File, abs string, partSize int64,
	partCount int, done []api.CompletedPart, pending []api.PartURL) error {
	got, err := a.sendParts(ctx, p, abs, f.Size, partSize, pending)
	done = append(done, got...)
	if err != nil {
		return err // 다음 시도에서 GET parts로 남은 파트만 보낸다
	}
	if len(done) < partCount {
		return a.resume(ctx, p, f, abs) // URL이 더 필요하다
	}
	parts := make([]map[string]any, 0, len(done))
	for _, d := range done {
		parts = append(parts, map[string]any{"part_number": d.PartNumber, "etag": d.ETag})
	}
	var us api.UploadStatus
	_, err = a.Client.Do(ctx, "POST", "/v1/uploads/"+f.UploadID+"/complete", map[string]any{"parts": parts},
		map[string]string{"Idempotency-Key": "complete-" + f.UploadID}, &us)
	if err != nil {
		var pr *api.Problem
		if errors.As(err, &pr) && pr.Status == 410 {
			return a.Store.Reset(ctx, f.ID)
		}
		return a.handleProblem(ctx, f, err)
	}
	a.Log.Info("업로드 완료", "path", f.RelPath, "asset_id", us.AssetID, "ingest_status", us.IngestStatus)
	if us.AssetID != f.AssetID {
		return a.Store.MarkStatus(ctx, f.ID, state.Failed, "UPLOAD_ASSET_MISMATCH")
	}
	return a.awaitReceipt(ctx, f, f.AssetID)
}

func (a *Agent) awaitReceipt(ctx context.Context, f *state.File, assetID string) error {
	if assetID == "" {
		return a.Store.MarkStatus(ctx, f.ID, state.Failed, "RECEIPT_ASSET_MISSING")
	}
	if err := a.Store.AwaitReceipt(ctx, f, assetID); err != nil {
		return err
	}
	f.AssetID = assetID
	return a.confirmReceipt(ctx, f)
}

// maxBackoff는 재시도 간격 상한(설정 max_backoff_s)이다.
func (a *Agent) maxBackoff() time.Duration { return timex.Seconds(int64(a.Cfg.MaxBackoffS)) }

func (a *Agent) receiptRetry(ctx context.Context, f *state.File) error {
	return a.Store.Retry(ctx, f.ID, "RECEIPT_PENDING", sched.Backoff(f.Attempts, a.maxBackoff()))
}

func (a *Agent) confirmReceipt(ctx context.Context, f *state.File) error {
	status := ""
	if f.UploadID != "" {
		var us api.UploadStatus
		_, err := a.Client.Do(ctx, "GET", "/v1/uploads/"+f.UploadID, nil, nil, &us)
		if err != nil {
			var pr *api.Problem
			if errors.As(err, &pr) && pr.Status == 404 {
				return a.receiptRetry(ctx, f)
			}
			return a.handleProblem(ctx, f, err)
		}
		if us.AssetID != f.AssetID {
			return a.Store.MarkStatus(ctx, f.ID, state.Failed, "UPLOAD_ASSET_MISMATCH")
		}
		status = us.IngestStatus
		switch status {
		case "QUARANTINED", "REJECTED", "FAILED":
			return a.Store.MarkStatus(ctx, f.ID, state.Failed, "SERVER_"+status)
		case "UPLOADING", "RECEIVED", "VALIDATING", "REGISTERED", "PROXIED", "READY_PRIVATE":
		default:
			return a.Store.MarkStatus(ctx, f.ID, state.Failed, "UPLOAD_STATUS_INVALID")
		}
	}
	receipt, err := a.Client.Receipt(ctx, f.AssetID)
	if err != nil {
		var pr *api.Problem
		if errors.As(err, &pr) && pr.Status == 404 {
			return a.receiptRetry(ctx, f)
		}
		return a.handleProblem(ctx, f, err)
	}
	if receipt.AssetID != f.AssetID || receipt.ChecksumSHA256 != f.SHA256 || receipt.ByteSize == nil || *receipt.ByteSize != f.Size {
		return a.Store.MarkStatus(ctx, f.ID, state.Failed, "RECEIPT_MISMATCH")
	}
	_, timeErr := time.Parse(time.RFC3339, receipt.RegisteredAt)
	entryHash, hashErr := hex.DecodeString(receipt.IntegrityEntryHash)
	if timeErr != nil || receipt.IntegrityLogEntryID == "" || hashErr != nil || len(entryHash) != sha256.Size {
		return a.Store.MarkStatus(ctx, f.ID, state.Failed, "RECEIPT_INVALID")
	}
	if f.UploadID != "" && status != "REGISTERED" && status != "PROXIED" && status != "READY_PRIVATE" {
		return a.receiptRetry(ctx, f)
	}
	raw, err := json.Marshal(receipt)
	if err != nil {
		return err
	}
	return a.Store.MarkDoneWithReceipt(ctx, f, string(raw))
}

func (a *Agent) sendParts(ctx context.Context, p *api.Policy, abs string, size, partSize int64,
	pending []api.PartURL) ([]api.CompletedPart, error) {
	if len(pending) == 0 {
		return nil, nil
	}
	file, err := os.Open(abs)
	if err != nil {
		return nil, err
	}
	defer func() { _ = file.Close() }()            // 읽기 전용
	sem := make(chan struct{}, p.MaxParallelParts) // 동시 전송 수 (요구사항 5.1)
	var mu sync.Mutex
	var out []api.CompletedPart
	var firstErr error
	var wg sync.WaitGroup
	for _, pu := range pending {
		pu := pu
		off := int64(pu.PartNumber-1) * partSize
		n := partSize
		if off+n > size {
			n = size - off
		}
		if n < 0 {
			n = 0
		}
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			r := a.limiter.Reader(ctx, io.NewSectionReader(file, off, n))
			etag, err := a.Client.PutPart(ctx, pu.URL, r, n)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				if firstErr == nil {
					firstErr = err
				}
				return
			}
			out = append(out, api.CompletedPart{PartNumber: pu.PartNumber, ETag: etag})
		}()
	}
	wg.Wait()
	if firstErr != nil {
		var pr *api.Problem
		if errors.As(firstErr, &pr) && pr.Code == "PART_URL_EXPIRED" {
			return out, fmt.Errorf("%w: 파트 URL 만료", api.ErrOffline) // 재개 때 새 URL을 받는다
		}
	}
	return out, firstErr
}

// handleProblem은 서버 거부를 파일 상태로 바꾼다.
func (a *Agent) handleProblem(ctx context.Context, f *state.File, err error) error {
	if errors.Is(err, api.ErrRevoked) || errors.Is(err, api.ErrOffline) || errors.Is(err, context.Canceled) {
		return err
	}
	var pr *api.Problem
	if !errors.As(err, &pr) {
		_ = a.Store.Retry(ctx, f.ID, "UNEXPECTED", sched.Backoff(f.Attempts, a.maxBackoff()))
		return err
	}
	a.setErr(ctx, pr.Code)
	switch pr.Code {
	case "RULE_VIOLATION": // 서버 정책 판단을 따른다 (요구사항 2.4)
		a.Log.Warn("서버가 정책 위반으로 거부해 보내지 않습니다", "path", f.RelPath, "detail", pr.Detail)
		return a.Store.MarkStatus(ctx, f.ID, state.Excluded, "SERVER_RULE_VIOLATION")
	case "QUOTA_EXCEEDED", "RATE_LIMITED", "STORAGE_UNAVAILABLE", "SERVICE_UNAVAILABLE", "CONFLICT":
		wait := sched.Backoff(f.Attempts+3, a.maxBackoff())
		if pr.RetryAfter > 0 {
			wait = pr.RetryAfter
		}
		return a.Store.Retry(ctx, f.ID, pr.Code, wait)
	default: // VALIDATION_ERROR, NOT_FOUND, IDEMPOTENCY_CONFLICT, UNSUPPORTED_SCHEMA_VERSION 등
		a.Log.Error("서버가 파일을 거부했습니다", "path", f.RelPath, "code", pr.Code, "detail", pr.Detail)
		return a.Store.MarkStatus(ctx, f.ID, state.Failed, pr.Code)
	}
}

// ---------------------------------------------------------------- 하트비트

func (a *Agent) Heartbeat(ctx context.Context) (map[string]any, error) {
	st, err := a.Store.Stats(ctx)
	if err != nil {
		return nil, err
	}
	obsQueued, _ := a.Store.ObservationQueueSize(ctx)
	p := a.policy.Load()
	pv := "unknown"
	if p != nil {
		pv = p.PolicyVersion
	}
	free, _ := sysinfo.DiskFree(a.Dir)
	lastErr, _ := a.lastErr.Load().(string)
	if free > 0 && free < a.Cfg.LowDiskBytes {
		lastErr = "LOW_DISK"
		a.Log.Warn("로컬 디스크 여유 공간이 부족합니다", "free_bytes", free) // 요구사항 6.3
	}
	hb := map[string]any{
		"schema_version": SchemaVersion, "collector_device_id": a.Cfg.DeviceID, "agent_version": a.Version,
		"policy_version": pv, "sent_at": a.Now().UTC().Format(time.RFC3339),
		"queue":           map[string]any{"items": st.QueueItems, "bytes": st.QueueBytes, "observations": obsQueued},
		"disk_free_bytes": free, "policy_excluded": st.Excluded, "plugins": a.pluginStatus(),
	}
	if v, _ := a.Store.KV(ctx, KeyLastSuccess); v != "" {
		hb["last_success_at"] = v
	} else {
		hb["last_success_at"] = nil
	}
	if lastErr != "" {
		hb["last_error_code"] = lastErr
	} else {
		hb["last_error_code"] = nil
	}
	srcs := []any{}
	for _, s := range st.Sources {
		srcs = append(srcs, map[string]any{"device_id": s.DeviceID, "queue": map[string]any{"items": s.Items, "bytes": s.Bytes}})
	}
	hb["sources"] = srcs
	b, _ := json.Marshal(hb)
	_ = a.Store.SetKV(ctx, KeyLastHeartbeat, string(b)) // 연결이 없어도 로컬에 남긴다 (요구사항 9.2)
	_, err = a.Client.Do(ctx, "POST", "/v1/agents/heartbeat", hb, nil, nil)
	return hb, err
}

// ---------------------------------------------------------------- Sensor_Plugin (07번)

func (a *Agent) pluginStatus() []any {
	out := []any{}
	a.plugins.Range(func(k, v any) bool {
		out = append(out, map[string]any{"name": k, "version": "0.1.0", "status": v})
		return true
	})
	return out
}

// startSensorPlugins는 설정된 MQTT 플러그인을 띄운다. 매핑 프로필은 서버에서 받는다.
func (a *Agent) startSensorPlugins(ctx context.Context, wg *sync.WaitGroup) {
	for _, pc := range a.Cfg.SensorPlugins {
		pc := pc
		name := "mqtt:" + pc.SourceDeviceID
		a.plugins.Store(name, "DEGRADED")
		wg.Add(1)
		go func() {
			defer wg.Done()
			var rec struct { // contracts: ObservationMappingRecord
				Version int            `json:"version"`
				Mapping sensor.Mapping `json:"mapping"`
			}
			for attempt := 0; ; attempt++ {
				_, err := a.Client.Do(ctx, "GET", "/v1/devices/"+pc.SourceDeviceID+"/observation-mapping", nil, nil, &rec)
				if err == nil {
					break
				}
				a.Log.Warn("MQTT 매핑 프로필을 받을 수 없습니다", "source", pc.SourceDeviceID, "error", err.Error())
				select {
				case <-ctx.Done():
					return
				case <-time.After(sched.Backoff(attempt, 5*time.Minute)):
				}
			}
			plug := &sensor.MQTTPlugin{Cfg: pc, Mapping: rec.Mapping, Log: a.Log}
			a.plugins.Store(name, "OK")
			err := plug.Run(ctx, func(item map[string]any) {
				b, _ := json.Marshal(item)
				if err := a.Store.EnqueueObservation(ctx, pc.SourceDeviceID, string(b)); err != nil {
					a.Log.Error("관측값을 대기열에 넣지 못했습니다", "error", err.Error())
				}
			})
			if err != nil {
				a.plugins.Store(name, "FAILED")
				a.Log.Error("MQTT 플러그인 종료", "error", err.Error())
			}
		}()
	}
}

// SendObservations는 대기 관측값을 원천 장비별로 시간순 배치 전송한다 (요구사항 6.1, 6.2).
func (a *Agent) SendObservations(ctx context.Context) (int, error) {
	sources, err := a.Store.ObservationSources(ctx)
	if err != nil {
		return 0, err
	}
	sent := 0
	for _, src := range sources {
		for {
			batch, err := a.Store.PeekObservations(ctx, src, 1000)
			if err != nil || len(batch) == 0 {
				break
			}
			items := make([]json.RawMessage, len(batch))
			ids := make([]int64, len(batch))
			for i, q := range batch {
				items[i], ids[i] = json.RawMessage(q.Item), q.ID
			}
			body := map[string]any{"schema_version": SchemaVersion, "source_system_id": src, "observations": items}
			var res struct {
				Accepted, Duplicates, Rejected int
			}
			_, err = a.Client.Do(ctx, "POST", "/v1/observations", body,
				map[string]string{"Idempotency-Key": fmt.Sprintf("obs-%s-%d-%d", src, ids[0], ids[len(ids)-1])}, &res)
			if err != nil {
				var pr *api.Problem
				if errors.As(err, &pr) && pr.Status == 400 {
					// 형식 오류 배치를 계속 붙잡으면 뒤 관측값이 막힌다. 기록하고 버린다
					a.Log.Error("서버가 관측값 배치 형식을 거부해 버립니다", "source", src, "count", len(ids))
					a.setErr(ctx, "OBSERVATION_BATCH_REJECTED")
				} else {
					return sent, err // 오프라인 등: 대기열 유지
				}
			} else if res.Rejected > 0 {
				a.Log.Warn("일부 관측값이 거부되었습니다", "source", src, "rejected", res.Rejected)
			}
			if err := a.Store.DeleteObservations(ctx, ids); err != nil {
				return sent, err
			}
			sent += len(ids)
		}
	}
	return sent, nil
}

// ---------------------------------------------------------------- 실행 루프

func (a *Agent) Run(ctx context.Context) error {
	if v, _ := a.Store.KV(ctx, KeyReenroll); v == "true" {
		a.reenroll.Store(true)
		return api.ErrRevoked
	}
	if a.OnReady != nil {
		a.OnReady()
	}
	backoff := 0
	for a.policy.Load() == nil {
		if err := a.RefreshPolicy(ctx); err != nil && a.policy.Load() == nil {
			if errors.Is(err, api.ErrRevoked) {
				_ = a.Store.SetKV(ctx, KeyReenroll, "true")
				return err
			}
			wait := sched.Backoff(backoff, a.maxBackoff())
			a.Log.Warn("정책을 받을 수 없어 기다립니다", "error", err.Error(), "retry_in", wait.String())
			backoff++
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(wait):
			}
		}
	}
	watcher := a.startWatcher(ctx)
	if watcher != nil {
		defer func() { _ = watcher.Close() }()
	}
	var wg sync.WaitGroup
	loop := func(name string, every func() time.Duration, fn func() error, wake <-chan struct{}) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				if err := fn(); err != nil && !errors.Is(err, context.Canceled) {
					a.Log.Debug(name+" 실패", "error", err.Error())
				}
				select {
				case <-ctx.Done():
					return
				case <-time.After(every()):
				case <-wake: // nil 채널이면 기다리지 않는다
				}
			}
		}()
	}
	loop("scan", func() time.Duration { return timex.Seconds(int64(a.Cfg.ScanIntervalS)) },
		func() error { _, err := a.Scan(ctx); return err }, nil)
	uploadBackoff := 0
	loop("upload", func() time.Duration {
		if uploadBackoff > 0 {
			return sched.Backoff(uploadBackoff, a.maxBackoff())
		}
		return 5 * time.Second
	}, func() error {
		_, err := a.ProcessOnce(ctx)
		if errors.Is(err, api.ErrOffline) {
			uploadBackoff++ // 오프라인: 대기열 유지, 지수 백오프 (요구사항 6.1)
		} else {
			uploadBackoff = 0 // 연결 복구 시 사용자 조작 없이 재개 (요구사항 6.2)
		}
		return err
	}, a.wake)
	loop("heartbeat", a.heartbeatInterval, func() error { _, err := a.Heartbeat(ctx); return err }, nil)
	loop("policy", func() time.Duration { return timex.Seconds(int64(a.Cfg.PolicyRefreshS)) },
		func() error { return a.RefreshPolicy(ctx) }, nil)
	if len(a.Cfg.SensorPlugins) > 0 {
		a.startSensorPlugins(ctx, &wg)
		obsBackoff := 0
		loop("observations", func() time.Duration {
			if obsBackoff > 0 {
				return sched.Backoff(obsBackoff, a.maxBackoff())
			}
			return 5 * time.Second
		}, func() error {
			_, err := a.SendObservations(ctx)
			if errors.Is(err, api.ErrOffline) {
				obsBackoff++
			} else {
				obsBackoff = 0
			}
			return err
		}, nil)
	}
	<-ctx.Done()
	wg.Wait()
	return nil
}

// heartbeatInterval은 하트비트 루프의 대기 시간이다. 정책은 적용 전에 계약 범위를 검사했지만 변환도 따로 넘치지 않게 한다:
// 아주 큰 주기가 time.Duration 곱셈에서 음수가 되면 루프가 쉬지 않고 하트비트를 보냈다 (ADR-0040).
func (a *Agent) heartbeatInterval() time.Duration {
	return timex.Seconds(int64(a.policy.Load().HeartbeatIntervalS))
}

// startWatcher는 fsnotify로 변경을 감지해 스캔을 앞당긴다. 실패해도 주기 스캔으로 동작한다.
func (a *Agent) startWatcher(ctx context.Context) *fsnotify.Watcher {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		a.Log.Warn("파일 감시를 시작할 수 없어 주기 스캔만 합니다", "error", err.Error())
		return nil
	}
	addAll := func(root string) {
		_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if err == nil && d.IsDir() {
				_ = w.Add(path)
			}
			return nil
		})
	}
	addAll(a.Cfg.WatchRoot)
	go func() {
		var pending atomic.Bool
		for {
			select {
			case <-ctx.Done():
				return
			case ev, ok := <-w.Events:
				if !ok {
					return
				}
				if ev.Has(fsnotify.Create) {
					if st, err := os.Stat(ev.Name); err == nil && st.IsDir() {
						addAll(ev.Name)
					}
				}
				if pending.CompareAndSwap(false, true) { // 이벤트를 2초 모아 한 번 스캔
					time.AfterFunc(2*time.Second, func() { pending.Store(false); _, _ = a.Scan(ctx) })
				}
			case err := <-w.Errors:
				if err != nil { // 이벤트 유실(대기열 넘침 등)은 주기 스캔이 보완한다
					a.Log.Debug("파일 감시 오류", "error", err.Error())
				}
			}
		}
	}()
	return w
}

// OSName은 등록 요청의 os 값이다.
func OSName() string {
	if runtime.GOOS == "windows" {
		return "windows"
	}
	return "linux"
}
