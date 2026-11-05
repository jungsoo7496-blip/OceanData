package api

import (
	"bytes"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"
)

// 퍼즈 시험: 오류 응답의 본문(RFC 9457)과 Retry-After 머리는 서버나 중간 장비가 정한다 (06번 요구사항 6).
// 고장 났거나 악의적인 응답도 에이전트를 멈추게 하면 안 된다.
// 실행: go test -run='^$' -fuzz='^FuzzReadProblem$' -fuzztime=90s -parallel=2 ./internal/api
func FuzzReadProblem(f *testing.F) {
	f.Add(503, []byte(`{"status":503,"code":"SERVICE_UNAVAILABLE"}`), "") // TestTokenServiceFailureIsOfflineAndCanRecover
	f.Add(429, []byte(`{"status":429,"code":"RATE_LIMITED","title":"요청이 너무 많습니다","detail":"합성"}`), "30")
	f.Add(422, []byte(`{"code":"RULE_VIOLATION","errors":[{"field":"x"}],"unmet_conditions":[{}],"allowed_variants":["CLIP"]}`), "0")
	f.Add(502, []byte(`<html>bad gateway</html>`), "1.5")
	f.Add(400, []byte(`{"status":"x","code":1,"errors":{}}`), "-5")
	f.Add(599, []byte{}, "Wed, 21 Oct 2026 07:28:00 GMT")
	f.Add(409, []byte(`null`), "9223372037")
	f.Fuzz(func(t *testing.T, status int, body []byte, retryAfter string) {
		resp := &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(bytes.NewReader(body))}
		if retryAfter != "" {
			resp.Header.Set("Retry-After", retryAfter)
		}
		p := readProblem(resp)
		_ = p.Error()
		if p.Code == "" && http.StatusText(status) != "" {
			t.Fatalf("상태 %d의 오류 코드가 비었다", status)
		}
		ra := resp.Header.Get("Retry-After")
		if ra == "" && p.RetryAfter != 0 {
			t.Fatalf("Retry-After가 없는데 %v를 기다린다", p.RetryAfter)
		}
		// delay-seconds(숫자만)는 그 초 그대로 쓰고, time.Duration으로 나타낼 수 없을 만큼 크면 무시한다(백오프 사용)
		if ra != "" && strings.Trim(ra, "0123456789") == "" {
			want := time.Duration(0)
			if n, err := strconv.ParseInt(ra, 10, 64); err == nil && n <= math.MaxInt64/int64(time.Second) {
				want = time.Duration(n) * time.Second
			}
			if p.RetryAfter != want {
				t.Fatalf("Retry-After %q: got %v want %v", ra, p.RetryAfter, want)
			}
		}
	})
}
