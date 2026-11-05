package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestContractReceiptUsesBasePathAuthenticationAndRetry(t *testing.T) {
	tokens, requests := 0, 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/gateway/v1/auth/token":
			tokens++
			_, _ = fmt.Fprintf(w, `{"access_token":"synthetic-%d","expires_in":300}`, tokens)
		case "/gateway/v1/assets/ASSET-20261105-SYNTHETIC/receipt":
			requests++
			if r.Method != http.MethodGet || r.Header.Get("User-Agent") != "marine-agent/test" {
				t.Error("생성 요청도 GET·에이전트 식별자를 유지해야 합니다")
			}
			if r.Header.Get("Authorization") != fmt.Sprintf("Bearer synthetic-%d", tokens) {
				t.Error("인증 토큰이 누락되었습니다")
			}
			if requests == 1 {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			_, _ = w.Write([]byte(`{"asset_id":"ASSET-20261105-SYNTHETIC","byte_size":0}`))
		default:
			t.Errorf("계약 경로·서버 기본 경로가 잘못되었습니다: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	client := New(server.URL+"/gateway", server.Client(), &Credentials{ClientID: "synthetic", ClientSecret: "synthetic"}, "test")
	receipt, err := client.Receipt(context.Background(), "ASSET-20261105-SYNTHETIC")
	if err != nil || receipt.ByteSize == nil || *receipt.ByteSize != 0 || tokens != 2 || requests != 2 {
		t.Fatalf("401 후 인증 갱신·필드 존재 보존 실패: receipt=%+v tokens=%d requests=%d err=%v", receipt, tokens, requests, err)
	}
}

func TestContractReceiptPreservesMissingSizeAndProblem(t *testing.T) {
	for _, status := range []int{http.StatusOK, http.StatusNotFound} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/v1/auth/token" {
					_, _ = w.Write([]byte(`{"access_token":"synthetic","expires_in":300}`))
					return
				}
				w.WriteHeader(status)
				if status == http.StatusNotFound {
					_, _ = w.Write([]byte(`{"status":404,"code":"NOT_FOUND","title":"확인 중"}`))
				} else {
					_, _ = w.Write([]byte(`{"asset_id":"synthetic"}`))
				}
			}))
			defer server.Close()
			client := New(server.URL, server.Client(), &Credentials{ClientID: "synthetic", ClientSecret: "synthetic"}, "test")
			receipt, err := client.Receipt(context.Background(), "synthetic")
			if status == http.StatusOK && (err != nil || receipt.ByteSize != nil) {
				t.Fatalf("누락된 크기를 0으로 가정하면 안 됩니다: %+v %v", receipt, err)
			}
			var problem *Problem
			if status == http.StatusNotFound && (!errors.As(err, &problem) || problem.Status != status) {
				t.Fatalf("영수증 대기용 404 의미가 보존되지 않았습니다: %v", err)
			}
		})
	}
}
