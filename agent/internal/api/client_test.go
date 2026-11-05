package api

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
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
