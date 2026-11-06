package api

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync/atomic"
	"testing"
	"time"
)

func TestTokenServiceFailureIsOfflineAndCanRecover(t *testing.T) {
	var fail atomic.Bool
	fail.Store(true)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/auth/token" {
			if fail.Load() {
				w.WriteHeader(http.StatusServiceUnavailable)
				_, _ = w.Write([]byte(`{"status":503,"code":"SERVICE_UNAVAILABLE"}`))
				return
			}
			_, _ = w.Write([]byte(`{"access_token":"synthetic","expires_in":300}`))
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client := New(server.URL, server.Client(), &Credentials{ClientID: "synthetic", ClientSecret: "synthetic"}, "test")
	if _, err := client.Do(context.Background(), "GET", "/policy", nil, nil, nil); !errors.Is(err, ErrOffline) {
		t.Fatalf("토큰 서버 503도 재등록이 아닌 오프라인이어야 한다: %v", err)
	}
	fail.Store(false)
	if _, err := client.Do(context.Background(), "GET", "/policy", nil, nil, nil); err != nil {
		t.Fatal("서버 복구 후 토큰을 다시 받아야 한다", err)
	}
}

func TestLargeTokenLifetimeDoesNotTriggerImmediateRefresh(t *testing.T) {
	if strconv.IntSize < 64 {
		t.Skip("32비트 int에서는 시간 곱셈 전에 expires_in JSON 범위 검사로 거부됨")
	}
	var tokenCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/auth/token" {
			tokenCalls.Add(1)
			// 나노초로 환산하면 time.Duration 범위를 처음 넘는 정수 초다.
			_, _ = w.Write([]byte(`{"access_token":"synthetic","expires_in":9223372037}`))
			return
		}
		if r.Header.Get("Authorization") != "Bearer synthetic" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client := New(server.URL, server.Client(), &Credentials{ClientID: "synthetic", ClientSecret: "synthetic"}, "test")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for range 2 {
		if status, err := client.Do(ctx, http.MethodGet, "/policy", nil, nil, nil); status != http.StatusNoContent || err != nil {
			t.Fatalf("정책 요청 실패: 상태=%d 오류=%v", status, err)
		}
	}
	if got := tokenCalls.Load(); got != 1 {
		t.Fatalf("토큰을 두 정책 요청에 재사용해야 한다: 발급 요청 %d회", got)
	}
}

func TestPutEmptyPartSendsSignedContentLength(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if length := r.Header.Get("Content-Length"); length != "0" || len(r.TransferEncoding) != 0 {
			t.Errorf("빈 파트에 Content-Length: 0이 필요합니다: Content-Length=%q Transfer-Encoding=%v",
				length, r.TransferEncoding)
		}
		w.Header().Set("ETag", `"synthetic"`)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	client := New(server.URL, server.Client(), nil, "test")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	// 에이전트는 빈 파일도 같은 SectionReader 경로로 파트 전송을 요청한다.
	body := io.NewSectionReader(bytes.NewReader(nil), 0, 0)
	if etag, err := client.PutPart(ctx, server.URL, body, 0); err != nil || etag != `"synthetic"` {
		t.Fatalf("빈 파트 전송 실패: ETag=%q 오류=%v", etag, err)
	}
}
