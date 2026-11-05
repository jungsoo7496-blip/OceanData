package core

// 06번 요구사항 5.5: RECEIVED 응답은 영수증이 아니다. 등록·무결성 확인 전에는 원본과 대기열을 유지한다.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
)

func TestReceivedWaitsForReceiptAcrossRestart(t *testing.T) {
	e := newEnv(t)
	var allow atomic.Bool
	handler := e.fake.Config.Handler
	e.fake.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/receipt") && !allow.Load() {
			e.fake.json(w, 404, map[string]any{"status": 404, "code": "NOT_FOUND"})
			return
		}
		handler.ServeHTTP(w, r)
	})
	a := e.agent(t)
	ctx := context.Background()
	e.write(t, "S1/sensor/receipt.csv", []byte("synthetic\n1\n"))
	e.scanStable(t, a)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	f, err := a.Store.Get(ctx, "S1/sensor/receipt.csv")
	if err != nil || f == nil || f.Status != state.Uploading || f.LastError != "RECEIPT_PENDING" {
		t.Fatalf("RECEIVED만으로 완료하면 안 된다: %+v, %v", f, err)
	}
	if got, _ := a.Store.KV(ctx, KeyLastSuccess); got != "" {
		t.Fatal("영수증 전에는 성공 시각을 기록하면 안 된다")
	}
	if err := a.Store.Close(); err != nil {
		t.Fatal(err)
	}
	a2 := e.agent(t)
	allow.Store(true)
	e.clock = e.clock.Add(time.Hour)
	if _, err := a2.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	f, _ = a2.Store.Get(ctx, f.RelPath)
	if f.Status != state.Done {
		t.Fatalf("실제 영수증 검증 뒤 완료해야 한다: %+v", f)
	}
	raw, err := a2.Store.KV(ctx, fmt.Sprintf("receipt:%d", f.ID))
	var receipt api.Receipt
	if err != nil || json.Unmarshal([]byte(raw), &receipt) != nil || receipt.AssetID != f.AssetID || receipt.ChecksumSHA256 != f.SHA256 {
		t.Fatalf("검증된 영수증을 SQLite에 보관해야 한다: %q, %v", raw, err)
	}
	if pending, _ := a2.Store.ReceiptPending(ctx, f.ID); pending {
		t.Fatal("완료 뒤 영수증 대기 표시가 남았다")
	}
	if n, err := a2.ProcessOnce(ctx); err != nil || n != 0 {
		t.Fatalf("영수증이 있는 파일을 다시 보내면 안 된다: %d, %v", n, err)
	}
	e.fake.mu.Lock()
	defer e.fake.mu.Unlock()
	if e.fake.putCount["upl1/1"] != 1 || len(e.fake.uploads) != 1 || e.fake.receiptGets != 1 {
		t.Fatal("영수증 대기·재시작 때문에 파일을 다시 보냈다")
	}
}

func TestInvalidReceiptOrQuarantineNeverMarksDone(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*api.Receipt)
		status string
	}{
		{name: "asset", mutate: func(r *api.Receipt) { r.AssetID = "ast_other" }},
		{name: "sha256", mutate: func(r *api.Receipt) { r.ChecksumSHA256 = strings.Repeat("0", 64) }},
		{name: "size", mutate: func(r *api.Receipt) { n := int64(999); r.ByteSize = &n }},
		{name: "missing_size", mutate: func(r *api.Receipt) { r.ByteSize = nil }},
		{name: "registered_at", mutate: func(r *api.Receipt) { r.RegisteredAt = "unknown" }},
		{name: "entry_hash", mutate: func(r *api.Receipt) { r.IntegrityEntryHash = "bad" }},
		{name: "entry_id", mutate: func(r *api.Receipt) { r.IntegrityLogEntryID = "" }},
		{name: "quarantined", status: "QUARANTINED"},
		{name: "rejected", status: "REJECTED"},
		{name: "failed", status: "FAILED"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			e := newEnv(t)
			handler := e.fake.Config.Handler
			e.fake.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if tc.status != "" && r.URL.Path == "/v1/uploads/upl1" {
					e.fake.json(w, 200, map[string]any{"upload_id": "upl1", "asset_id": "ast_upl1", "ingest_status": tc.status})
					return
				}
				if tc.mutate != nil && strings.HasSuffix(r.URL.Path, "/receipt") {
					e.fake.mu.Lock()
					receipt := e.fake.receipts["ast_upl1"]
					e.fake.mu.Unlock()
					tc.mutate(&receipt)
					e.fake.json(w, 200, receipt)
					return
				}
				handler.ServeHTTP(w, r)
			})
			a := e.agent(t)
			data := []byte("synthetic,data\n1,2\n")
			rel := "S1/sensor/invalid.csv"
			e.write(t, rel, data)
			e.scanStable(t, a)
			if _, err := a.ProcessOnce(context.Background()); err != nil {
				t.Fatal(err)
			}
			f, err := a.Store.Get(context.Background(), rel)
			if err != nil || f == nil || f.Status != state.Failed {
				t.Fatalf("손상·격리·잘못된 영수증을 완료로 표시했다: %+v, %v", f, err)
			}
			if raw, _ := a.Store.KV(context.Background(), fmt.Sprintf("receipt:%d", f.ID)); raw != "" {
				t.Fatal("잘못된 영수증을 저장했다")
			}
			if got, err := os.ReadFile(filepath.Join(e.root, filepath.FromSlash(rel))); err != nil || string(got) != string(data) {
				t.Fatal("실패한 파일의 원본을 바꾸거나 지웠다")
			}
		})
	}
}

func TestAlreadyRegisteredStillNeedsMatchingReceipt(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	ctx := context.Background()
	data := []byte("synthetic,data\n1,2\n")
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	e.fake.mu.Lock()
	e.fake.bySHA[sha] = "old"
	e.fake.mu.Unlock()
	e.write(t, "S1/sensor/existing.csv", data)
	e.scanStable(t, a)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	f, _ := a.Store.Get(ctx, "S1/sensor/existing.csv")
	if f.Status == state.Done {
		t.Fatal("ALREADY_REGISTERED만으로 영수증 없이 완료했다")
	}
	e.fake.mu.Lock()
	e.fake.receipts["ast_x"] = fakeReceipt("ast_x", sha, int64(len(data)))
	e.fake.mu.Unlock()
	e.clock = e.clock.Add(time.Hour)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	f, _ = a.Store.Get(ctx, f.RelPath)
	if f.Status != state.Done || len(e.fake.uploads) != 0 {
		t.Fatal("기존 자산 영수증 검증 후에는 새 업로드 없이 완료해야 한다")
	}
}

func TestCompletedSessionConflictDoesNotReplaceReceipt(t *testing.T) {
	e := newEnv(t)
	var allow atomic.Bool
	handler := e.fake.Config.Handler
	e.fake.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/receipt") && !allow.Load() {
			e.fake.json(w, 404, map[string]any{"status": 404, "code": "NOT_FOUND"})
			return
		}
		handler.ServeHTTP(w, r)
	})
	a := e.agent(t)
	ctx := context.Background()
	e.write(t, "S1/sensor/conflict.csv", []byte("synthetic\n"))
	e.scanStable(t, a)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	f, _ := a.Store.Get(ctx, "S1/sensor/conflict.csv")
	// 서버 완료 직후, 영수증 대기 표시를 저장하기 전에 프로세스가 멈춘 상황도 복구한다.
	if err := a.Store.SetKV(ctx, fmt.Sprintf("receipt_pending:%d", f.ID), ""); err != nil {
		t.Fatal(err)
	}
	e.clock = e.clock.Add(time.Hour)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	f, _ = a.Store.Get(ctx, f.RelPath)
	if f.Status != state.Uploading {
		t.Fatal("GET parts 409는 수집 영수증을 대체할 수 없다")
	}
	allow.Store(true)
	e.clock = e.clock.Add(time.Hour)
	if _, err := a.ProcessOnce(ctx); err != nil {
		t.Fatal(err)
	}
	f, _ = a.Store.Get(ctx, f.RelPath)
	if f.Status != state.Done {
		t.Fatal("409 뒤에도 실제 영수증을 검증하면 완료해야 한다")
	}
}

func TestMetadataOnlyC0KeepsMetadataRegistrationSemantics(t *testing.T) {
	for _, status := range []string{"METADATA_ONLY_REGISTERED", "ALREADY_REGISTERED"} {
		t.Run(status, func(t *testing.T) {
			e := newEnv(t)
			e.fake.policy.Sources[1].CollectionMode = "C0_METADATA_ONLY"
			handler := e.fake.Config.Handler
			e.fake.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/v1/uploads" && r.Method == "POST" {
					e.fake.json(w, 201, map[string]any{"status": status, "asset_id": "ast_metadata_only"})
					return
				}
				handler.ServeHTTP(w, r)
			})
			a := e.agent(t)
			ctx := context.Background()
			e.write(t, "S1/sensor/metadata.csv", []byte("synthetic\n"))
			e.scanStable(t, a)
			if _, err := a.ProcessOnce(ctx); err != nil {
				t.Fatal(err)
			}
			f, _ := a.Store.Get(ctx, "S1/sensor/metadata.csv")
			if f.Status != state.Done || len(e.fake.uploads) != 0 || e.fake.receiptGets != 0 {
				t.Fatal("C0는 바이너리 수집·무결성 영수증 없이 메타데이터 등록만 완료한다")
			}
			if raw, _ := a.Store.KV(ctx, fmt.Sprintf("receipt:%d", f.ID)); raw != "" {
				t.Fatal("C0 메타데이터 완료를 실수집 영수증으로 꾸미면 안 된다")
			}
		})
	}
}
