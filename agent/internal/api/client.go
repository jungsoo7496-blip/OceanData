// Package api는 플랫폼 API(contracts/openapi.yaml)를 호출한다.
// 서버 인증서는 항상 검증한다. 사설 CA 지정만 허용하고 검증을 끄는 옵션은 두지 않는다 (security.md).
package api

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// Problem은 RFC 9457 오류 응답이다.
type Problem struct {
	Status          int              `json:"status"`
	Code            string           `json:"code"`
	Title           string           `json:"title"`
	Detail          string           `json:"detail"`
	RequestID       string           `json:"request_id"`
	Errors          []map[string]any `json:"errors,omitempty"`
	UnmetConditions []map[string]any `json:"unmet_conditions,omitempty"`
	AllowedVariants []string         `json:"allowed_variants,omitempty"`
	RetryAfter      time.Duration    `json:"-"`
}

func (p *Problem) Error() string {
	msg := p.Title
	if p.Detail != "" {
		msg += ": " + p.Detail
	}
	return fmt.Sprintf("%d %s %s", p.Status, p.Code, msg)
}

var (
	// ErrRevoked는 자격증명이 폐기·무효라 재등록이 필요하다는 뜻이다 (06번 요구사항 10).
	ErrRevoked = errors.New("기기 자격증명이 폐기되었거나 유효하지 않습니다. 재등록이 필요합니다")
	// ErrOffline은 서버·스토리지에 연결할 수 없다는 뜻이다 (재시도 대상).
	ErrOffline = errors.New("서버에 연결할 수 없습니다")
)

// Credentials는 등록 때 받은 기기 자격증명이다.
type Credentials struct {
	DeviceID      string `json:"device_id"`
	ClientID      string `json:"client_id"`
	ClientSecret  string `json:"client_secret"`
	TokenEndpoint string `json:"token_endpoint"`
}

type Client struct {
	base  string
	http  *http.Client
	creds *Credentials
	ua    string

	mu       sync.Mutex
	token    string
	tokenExp time.Time
}

// NewHTTPClient는 시스템 루트 CA(+선택 사설 CA)로 검증하는 HTTP 클라이언트를 만든다.
func NewHTTPClient(caFile string, timeout time.Duration) (*http.Client, error) {
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	if caFile != "" {
		pem, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("사설 CA 파일을 읽을 수 없습니다: %w", err)
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, errors.New("사설 CA 파일에 인증서가 없습니다")
		}
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	tr.MaxIdleConnsPerHost = 16
	return &http.Client{Transport: tr, Timeout: timeout}, nil
}

func New(baseURL string, hc *http.Client, creds *Credentials, version string) *Client {
	return &Client{base: strings.TrimRight(baseURL, "/"), http: hc, creds: creds, ua: "marine-agent/" + version}
}

func readProblem(resp *http.Response) *Problem {
	p := &Problem{Status: resp.StatusCode}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	_ = json.Unmarshal(body, p)
	if p.Code == "" {
		p.Code = http.StatusText(resp.StatusCode)
	}
	if ra := resp.Header.Get("Retry-After"); ra != "" {
		if d, err := time.ParseDuration(ra + "s"); err == nil {
			p.RetryAfter = d
		}
	}
	return p
}

// Enroll은 1회용 등록 코드로 자격증명을 받는다 (POST /v1/agents/enroll).
func (c *Client) Enroll(ctx context.Context, code, agentVersion, osName string) (*Credentials, error) {
	body, _ := json.Marshal(map[string]string{"enrollment_code": code, "agent_version": agentVersion, "os": osName})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/v1/agents/enroll", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", c.ua)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrOffline, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusCreated {
		return nil, readProblem(resp)
	}
	var cr Credentials
	if err := json.NewDecoder(resp.Body).Decode(&cr); err != nil {
		return nil, err
	}
	return &cr, nil
}

func (c *Client) accessToken(ctx context.Context, force bool) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !force && c.token != "" && time.Now().Before(c.tokenExp) {
		return c.token, nil
	}
	if c.creds == nil {
		return "", ErrRevoked
	}
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {c.creds.ClientID},
		"client_secret": {c.creds.ClientSecret}}
	endpoint := c.creds.TokenEndpoint
	if endpoint == "" {
		endpoint = "/v1/auth/token"
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, c.base+endpoint, strings.NewReader(form.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", c.ua)
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("%w: %v", ErrOffline, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode == http.StatusUnauthorized {
		return "", ErrRevoked
	}
	if resp.StatusCode != http.StatusOK {
		problem := readProblem(resp)
		if resp.StatusCode >= 500 {
			return "", fmt.Errorf("%w: %v", ErrOffline, problem)
		}
		return "", problem
	}
	var tr struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&tr); err != nil {
		return "", err
	}
	c.token = tr.AccessToken
	ttl := time.Duration(tr.ExpiresIn) * time.Second
	c.tokenExp = time.Now().Add(ttl - ttl/5) // 만료 전에 갱신
	return c.token, nil
}

// Do는 인증된 JSON 요청을 보낸다. 401이면 토큰을 한 번 새로 받아 재시도하고, 그래도 401이면 ErrRevoked.
func (c *Client) Do(ctx context.Context, method, path string, in any, headers map[string]string, out any) (int, error) {
	var payload []byte
	if in != nil {
		payload, _ = json.Marshal(in)
	}
	for attempt := 0; attempt < 2; attempt++ {
		tok, err := c.accessToken(ctx, attempt > 0)
		if err != nil {
			return 0, err
		}
		var body io.Reader
		if payload != nil {
			body = bytes.NewReader(payload)
		}
		req, _ := http.NewRequestWithContext(ctx, method, c.base+path, body)
		req.Header.Set("Authorization", "Bearer "+tok)
		req.Header.Set("User-Agent", c.ua)
		if payload != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		for k, v := range headers {
			req.Header.Set(k, v)
		}
		resp, err := c.http.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return 0, ctx.Err()
			}
			return 0, fmt.Errorf("%w: %v", ErrOffline, err)
		}
		if resp.StatusCode == http.StatusUnauthorized && attempt == 0 {
			_ = resp.Body.Close()
			continue
		}
		defer func() { _ = resp.Body.Close() }()
		if resp.StatusCode == http.StatusUnauthorized {
			return resp.StatusCode, ErrRevoked
		}
		if resp.StatusCode >= 400 {
			p := readProblem(resp)
			if resp.StatusCode >= 500 {
				return resp.StatusCode, fmt.Errorf("%w: %v", ErrOffline, p)
			}
			return resp.StatusCode, p
		}
		if out != nil && resp.StatusCode != http.StatusNoContent {
			if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
				return resp.StatusCode, err
			}
		}
		return resp.StatusCode, nil
	}
	return 0, ErrRevoked
}

// PutPart는 사전 서명 URL로 파트를 스토리지에 직접 보낸다. URL은 로그에 남기지 않는다.
func (c *Client) PutPart(ctx context.Context, partURL string, r io.Reader, size int64) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, partURL, r)
	if err != nil {
		return "", errors.New("파트 URL 형식 오류")
	}
	req.ContentLength = size
	resp, err := c.http.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", fmt.Errorf("%w: 파트 전송 실패", ErrOffline)
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode == http.StatusForbidden {
		return "", &Problem{Status: 403, Code: "PART_URL_EXPIRED", Title: "파트 URL이 만료되었거나 거부되었습니다"}
	}
	if resp.StatusCode >= 300 {
		return "", fmt.Errorf("%w: 스토리지 응답 %d", ErrOffline, resp.StatusCode)
	}
	return resp.Header.Get("ETag"), nil
}
