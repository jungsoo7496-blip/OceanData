package update

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const helperMarker = "\nMARINE_UPDATE_SYNTHETIC_FIXTURE\n"

type helperInfo struct{ Version, Behavior string }

// 합성 실행 파일은 현재 시험 실행 파일에 버전·기동 동작 footer만 붙인다. 실제 별도 프로세스로 실행된다.
func TestMain(m *testing.M) {
	if os.Getenv("MARINE_UPDATE_HELPER") == "1" {
		exe, _ := os.Executable()
		data, _ := os.ReadFile(exe)
		offset := bytes.LastIndex(data, []byte(helperMarker))
		if offset < 0 {
			os.Exit(90)
		}
		var info helperInfo
		if json.Unmarshal(data[offset+len(helperMarker):], &info) != nil {
			os.Exit(91)
		}
		if len(os.Args) > 1 && os.Args[1] == "version" {
			if info.Behavior == "version-hang" {
				time.Sleep(time.Minute)
			}
			fmt.Println(info.Version)
			os.Exit(0)
		}
		f := flag.NewFlagSet("helper", flag.ContinueOnError)
		ready, token := f.String("ready-file", "", ""), f.String("ready-token", "", "")
		_ = f.Bool("parent-stdin", false, "")
		if f.Parse(os.Args[2:]) != nil {
			os.Exit(92)
		}
		attempts, err := os.OpenFile(filepath.Join(filepath.Dir(*ready), "fixture-worker-attempts"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
		if err != nil {
			os.Exit(94)
		}
		_, err = fmt.Fprintln(attempts, info.Version)
		if closeErr := attempts.Close(); err != nil || closeErr != nil {
			os.Exit(94)
		}
		if info.Behavior == "fail" {
			os.Exit(7)
		}
		if info.Behavior != "silent" {
			if os.WriteFile(*ready, []byte(*token), 0o600) != nil {
				os.Exit(93)
			}
		}
		if info.Behavior == "early-exit" {
			time.Sleep(10 * time.Millisecond)
			os.Exit(7)
		}
		if info.Behavior == "stubborn" {
			// stdin EOF를 무시하는 실제 자식으로 감독자의 강제 종료 경계를 검증한다.
			time.Sleep(time.Hour)
		}
		_, _ = io.Copy(io.Discard, os.Stdin)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

type fixture struct {
	o             Options
	data, sig     []byte
	file, sigFile string
}

func testFixture(t *testing.T, oldVersion, newVersion, behavior string) fixture {
	t.Helper()
	dir := t.TempDir()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	base, err := os.ReadFile(exe)
	if err != nil {
		t.Fatal(err)
	}
	build := func(v, b string) []byte {
		footer, _ := json.Marshal(helperInfo{v, b})
		return append(append(append([]byte(nil), base...), []byte(helperMarker)...), footer...)
	}
	ext := ""
	if runtime.GOOS == "windows" {
		ext = ".exe"
	}
	current := filepath.Join(dir, "marine agent"+ext)
	file := filepath.Join(dir, "candidate"+ext)
	if err = os.WriteFile(current, build(oldVersion, "healthy"), 0o755); err != nil {
		t.Fatal(err)
	}
	data := build(newVersion, behavior)
	if err = os.WriteFile(file, data, 0o755); err != nil {
		t.Fatal(err)
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	sig := []byte(base64.StdEncoding.EncodeToString(ed25519.Sign(priv, data)) + "\n")
	sigFile := file + ".sig"
	if err = os.WriteFile(sigFile, sig, 0o600); err != nil {
		t.Fatal(err)
	}
	o := Options{Binary: current, Dir: dir, PublicKey: base64.StdEncoding.EncodeToString(pub), MaxBytes: int64(len(data) + 1024),
		Timeout: 3 * time.Second, StartupTimeout: 2 * time.Second, Stable: 100 * time.Millisecond, Interval: time.Hour,
		WorkerArgs: []string{"worker-run"}, env: append(os.Environ(), "MARINE_UPDATE_HELPER=1")}
	if runtime.GOOS == "windows" {
		// Windows에서 새 PE를 생성·실행하는 시간이 합성 워커의 동작 판정을 가리지 않게 한다.
		o.Timeout = 20 * time.Second
		o.StartupTimeout = 12 * time.Second
	}
	return fixture{o, data, sig, file, sigFile}
}

func tlsFixtureServer(t *testing.T, f *fixture, handler func(http.ResponseWriter, *http.Request)) *httptest.Server {
	t.Helper()
	s := httptest.NewTLSServer(http.HandlerFunc(handler))
	t.Cleanup(s.Close)
	f.o.Auto, f.o.URL, f.o.SignatureURL, f.o.Client = true, s.URL+"/binary", s.URL+"/signature", s.Client()
	return s
}

func TestManualSignedProcessStartupAndRollback(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "healthy")
	before, _ := os.ReadFile(f.o.Binary)
	if err := Manual(context.Background(), f.o, f.file, f.sigFile); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(f.o.Binary)
	if !bytes.Equal(after, f.data) {
		t.Fatal("검증한 바이트가 적용되지 않음")
	}
	if err := Rollback(f.o.Binary); err != nil {
		t.Fatal(err)
	}
	after, _ = os.ReadFile(f.o.Binary)
	if !bytes.Equal(before, after) {
		t.Fatal("이전 실행 파일로 복원되지 않음")
	}
}

func TestNewWorkerStartupFailureAutomaticallyRollsBack(t *testing.T) {
	for _, behavior := range []string{"fail", "silent", "early-exit"} {
		t.Run(behavior, func(t *testing.T) {
			f := testFixture(t, "1.0.0", "2.0.0", behavior)
			before, _ := os.ReadFile(f.o.Binary)
			if err := Manual(context.Background(), f.o, f.file, f.sigFile); err == nil {
				t.Fatal("기동 실패가 성공 처리됨")
			}
			attempts, err := os.ReadFile(filepath.Join(f.o.Dir, "fixture-worker-attempts"))
			if err != nil || string(attempts) != "2.0.0\n" {
				t.Fatalf("서명된 새 워커가 실제로 시작되지 않았습니다 (%q, %v)", attempts, err)
			}
			after, _ := os.ReadFile(f.o.Binary)
			if !bytes.Equal(before, after) {
				t.Fatal("기동 실패 뒤 이전 버전으로 복원하지 않음")
			}
		})
	}
}

func TestWorkerIgnoringEOFIsKilledWithinBound(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "stubborn")
	if err := os.WriteFile(f.o.Binary, f.data, 0o755); err != nil {
		t.Fatal(err)
	}
	w, err := startWorker(context.Background(), f.o)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		select {
		case <-w.done:
			return
		default:
		}
		_ = w.cmd.Process.Kill()
		select {
		case <-w.done:
		case <-time.After(2 * time.Second):
			t.Error("EOF 무시 시험 자식 정리 시간 초과")
		}
	})
	started := time.Now()
	err = w.stop(200 * time.Millisecond)
	if err == nil {
		t.Fatal("EOF를 무시하는 자식을 정상 종료로 처리함")
	}
	if errors.Is(err, ErrWorkerRunning) {
		t.Fatalf("강제 종료 뒤 실제 종료를 확인하지 못함: %v", err)
	}
	if time.Since(started) > 2*time.Second {
		t.Fatal("강제 종료가 제한 시간을 넘김")
	}
	select {
	case <-w.done:
		if w.cmd.ProcessState == nil || w.err == nil {
			t.Fatal("실제 자식의 강제 종료 결과를 회수하지 못함")
		}
	default:
		t.Fatal("강제 종료 뒤 자식이 남아 있음")
	}
}

func TestSignedOldVersionAndTamperingAreRejected(t *testing.T) {
	f := testFixture(t, "2.0.0", "1.0.0", "healthy")
	if _, err := Local(context.Background(), f.o, f.file, f.sigFile); err == nil || !strings.Contains(err.Error(), "같거나 이전 버전") {
		t.Fatalf("서명된 이전 버전을 버전 비교로 거부하지 않았습니다: %v", err)
	}
	f.data[len(f.data)/2] ^= 1
	if err := os.WriteFile(f.file, f.data, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := Local(context.Background(), f.o, f.file, f.sigFile); err == nil || !strings.Contains(err.Error(), "서명 검증 실패") {
		t.Fatalf("변조한 실행 파일을 서명 검증으로 거부하지 않았습니다: %v", err)
	}
}

func TestAutomaticDisabledMakesNoNetworkRequests(t *testing.T) {
	var calls atomic.Int32
	f := testFixture(t, "1.0.0", "2.0.0", "healthy")
	tlsFixtureServer(t, &f, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(500) })
	f.o.Auto = false
	runSupervisorUntil(t, f.o, "워커 기동 확인 완료", "", 200*time.Millisecond)
	if calls.Load() != 0 {
		t.Fatal("자동 업데이트가 꺼졌는데 요청함")
	}
	if c, err := Fetch(context.Background(), Options{}); err != nil || c != nil {
		t.Fatal("꺼진 설정의 주소나 키를 검사함")
	}
}

func TestHTTPSAndRedirectDowngradeAreRejected(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "healthy")
	var plainCalls atomic.Int32
	plain := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { plainCalls.Add(1) }))
	defer plain.Close()
	tlsFixtureServer(t, &f, func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, plain.URL, http.StatusFound) })
	if _, err := Fetch(context.Background(), f.o); err == nil {
		t.Fatal("HTTPS→HTTP 리디렉션을 허용함")
	}
	if plainCalls.Load() != 0 {
		t.Fatal("HTTP 서버에 실제 요청함")
	}
	f.o.URL = plain.URL
	if _, err := Fetch(context.Background(), f.o); err == nil {
		t.Fatal("HTTP 업데이트 주소 허용")
	}
}

func TestDefaultTLSDoesNotTrustUnknownCertificate(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "healthy")
	tlsFixtureServer(t, &f, func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(f.data) })
	f.o.Client = nil
	if _, err := Fetch(context.Background(), f.o); err == nil {
		t.Fatal("미신뢰 TLS 인증서를 허용함")
	}
}

func TestDownloadSizeAndTimeoutLimits(t *testing.T) {
	for _, mode := range []string{"binary", "signature", "timeout"} {
		t.Run(mode, func(t *testing.T) {
			f := testFixture(t, "1.0.0", "2.0.0", "healthy")
			tlsFixtureServer(t, &f, func(w http.ResponseWriter, r *http.Request) {
				if mode == "timeout" {
					<-r.Context().Done()
					return
				}
				if r.URL.Path == "/signature" {
					if mode == "signature" {
						_, _ = io.WriteString(w, strings.Repeat("x", int(maxSignatureBytes)+1))
						return
					}
					_, _ = w.Write(f.sig)
					return
				}
				if mode == "binary" {
					_, _ = w.Write(append(f.data, make([]byte, 2048)...))
					return
				}
				_, _ = w.Write(f.data)
			})
			if mode == "timeout" {
				f.o.Timeout = 100 * time.Millisecond
			}
			if _, err := Fetch(context.Background(), f.o); err == nil {
				t.Fatal("크기·시간 한도를 넘은 다운로드 허용")
			}
		})
	}
}

type updateLogWriter func([]byte) (int, error)

func (w updateLogWriter) Write(p []byte) (int, error) { return w(p) }

// runSupervisorUntil은 파일 교체 시점이 아니라 워커 준비를 확인한 로그를 기다린다.
func runSupervisorUntil(t *testing.T, o Options, message, version string, observe time.Duration) {
	t.Helper()
	ready := make(chan struct{}, 1)
	o.Log = slog.New(slog.NewJSONHandler(updateLogWriter(func(p []byte) (int, error) {
		var event struct {
			Message string `json:"msg"`
			Version string `json:"version"`
		}
		if json.Unmarshal(p, &event) == nil && event.Message == message && event.Version == version {
			select {
			case ready <- struct{}{}:
			default:
			}
		}
		return len(p), nil
	}), nil))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { defer close(done); done <- Supervisor(ctx, o) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(3 * o.StartupTimeout):
			t.Error("감독 시험 프로세스 정리 시간 초과")
		}
	})
	select {
	case <-ready:
	case err := <-done:
		t.Fatalf("워커 기동을 확인하기 전에 감독 프로세스 종료: %v", err)
	case <-time.After(3*o.Timeout + 4*o.StartupTimeout):
		t.Fatalf("워커 기동 확인 시간 초과: %s", message)
	}
	if observe > 0 {
		select {
		case err := <-done:
			t.Fatalf("관측 중 감독 프로세스 종료: %v", err)
		case <-time.After(observe):
		}
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * o.StartupTimeout):
		t.Fatal("감독 프로세스 종료 시간 초과")
	}
}

func TestAutomaticUpdateStartsVerifiedWorker(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "healthy")
	tlsFixtureServer(t, &f, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/signature" {
			_, _ = w.Write(f.sig)
		} else {
			_, _ = w.Write(f.data)
		}
	})
	runSupervisorUntil(t, f.o, "새 버전 기동 확인 완료", "2.0.0", 0)
	data, _ := os.ReadFile(f.o.Binary)
	if !bytes.Equal(data, f.data) {
		t.Fatal("자동 업데이트 미적용")
	}
}

func TestRunningAgentRejectsManualUpdate(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "healthy")
	unlock, err := Lock(f.o.Dir)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	if err = Manual(context.Background(), f.o, f.file, f.sigFile); err == nil {
		t.Fatal("실행 중 수동 교체 허용")
	}
}

func TestAutomaticStartupFailureRestartsPreviousWorker(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "fail")
	previous, _ := os.ReadFile(f.o.Binary)
	tlsFixtureServer(t, &f, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/signature" {
			_, _ = w.Write(f.sig)
		} else {
			_, _ = w.Write(f.data)
		}
	})
	runSupervisorUntil(t, f.o, "이전 버전 워커로 계속 실행합니다", "", 0)
	current, _ := os.ReadFile(f.o.Binary)
	attempts, err := os.ReadFile(filepath.Join(f.o.Dir, "fixture-worker-attempts"))
	if !bytes.Equal(current, previous) || err != nil || string(attempts) != "1.0.0\n2.0.0\n1.0.0\n" {
		t.Fatalf("새 워커 실행 실패 뒤 이전 파일·워커가 복원되지 않았습니다 (%q, %v)", attempts, err)
	}
}

func TestChangingSourceAfterVerificationCannotChangeInstalledBytes(t *testing.T) {
	f := testFixture(t, "1.0.0", "2.0.0", "healthy")
	candidate, err := Local(context.Background(), f.o, f.file, f.sigFile)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(f.file, []byte("unverified-replacement"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err = install(f.o.Binary, candidate); err != nil {
		t.Fatal(err)
	}
	current, _ := os.ReadFile(f.o.Binary)
	if !bytes.Equal(current, f.data) {
		t.Fatal("검증 뒤 원본 파일 바꿔치기를 적용함")
	}
}
