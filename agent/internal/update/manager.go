package update

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Masterminds/semver/v3"
)

// Options는 운영자가 정하는 업데이트 설정이다. URL 기본값은 없고 자동 갱신은 기본 꺼짐이다.
type Options struct {
	Binary, Dir, PublicKey, URL, SignatureURL string
	Auto                                      bool
	Interval, Timeout, StartupTimeout, Stable time.Duration
	MaxBytes                                  int64
	Client                                    *http.Client
	Log                                       *slog.Logger
	// WorkerArgs는 준비 통지 옵션 앞에 붙는 고정 명령 인자다.
	WorkerArgs []string
	env        []string // 합성 프로세스 시험에서만 사용한다.
}

// Candidate에는 서명을 확인한 바이트만 보관한다. 원래 다운로드·로컬 파일은 다시 열어 적용하지 않는다.
type Candidate struct {
	data     []byte
	version  *semver.Version
	maxBytes int64
}

var ErrWorkerRunning = errors.New("자식 프로세스 종료를 확인하지 못했습니다 (추가 교체·복원 중단)")

func (o Options) validate() error {
	if o.Binary == "" || o.Dir == "" || o.MaxBytes <= 0 || o.Timeout <= 0 ||
		o.StartupTimeout <= 0 || o.Stable <= 0 || o.Stable >= o.StartupTimeout {
		return errors.New("업데이트 경로·크기·대기 시간 설정이 올바르지 않습니다")
	}
	if o.Auto && o.Interval <= 0 {
		return errors.New("자동 업데이트 주기는 양수여야 합니다")
	}
	return nil
}

func validateHTTPS(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" {
		return errors.New("업데이트 주소는 자격증명·fragment가 없는 HTTPS URL이어야 합니다")
	}
	return nil
}

func (o Options) client() (*http.Client, error) {
	if err := validateHTTPS(o.URL); err != nil {
		return nil, err
	}
	if err := validateHTTPS(o.SignatureURL); err != nil {
		return nil, err
	}
	c := http.Client{Timeout: o.Timeout}
	if o.Client != nil {
		c = *o.Client
	}
	c.Timeout = o.Timeout
	previous := c.CheckRedirect
	c.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if err := validateHTTPS(req.URL.String()); err != nil {
			return err
		}
		if len(via) >= 5 {
			return errors.New("업데이트 리디렉션 횟수 초과")
		}
		if previous != nil {
			return previous(req, via)
		}
		return nil
	}
	return &c, nil
}

func download(ctx context.Context, c *http.Client, raw string, limit int64) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return nil, errors.New("업데이트 요청을 만들지 못했습니다")
	}
	resp, err := c.Do(req)
	if err != nil {
		return nil, errors.New("업데이트 HTTPS 요청 실패 (인증서·연결·대기 시간 확인)")
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("업데이트 HTTP 상태 오류: %d", resp.StatusCode)
	}
	if resp.ContentLength > limit {
		return nil, errors.New("업데이트 파일 크기가 한도를 넘었습니다")
	}
	return readLimited(resp.Body, limit)
}

// Fetch는 자동 갱신이 꺼져 있으면 URL 검증·HTTP 클라이언트 생성도 하지 않는다.
func Fetch(ctx context.Context, o Options) (*Candidate, error) {
	if !o.Auto {
		return nil, nil
	}
	if err := o.validate(); err != nil {
		return nil, err
	}
	c, err := o.client()
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, o.Timeout)
	defer cancel()
	data, err := download(ctx, c, o.URL, o.MaxBytes)
	if err != nil {
		return nil, err
	}
	sig, err := download(ctx, c, o.SignatureURL, maxSignatureBytes)
	if err != nil {
		return nil, err
	}
	return candidate(ctx, o, data, sig)
}

// Local은 수동 파일도 같은 크기·서명·버전 검사로 확인한다.
func Local(ctx context.Context, o Options, file, sigPath string) (*Candidate, error) {
	if err := o.validate(); err != nil {
		return nil, err
	}
	data, err := readFile(file, o.MaxBytes)
	if err != nil {
		return nil, err
	}
	sig, err := readFile(sigPath, maxSignatureBytes)
	if err != nil {
		return nil, err
	}
	return candidate(ctx, o, data, sig)
}

func candidate(ctx context.Context, o Options, data, signature []byte) (*Candidate, error) {
	if err := verify(data, signature, o.PublicKey); err != nil {
		return nil, err
	}
	oldData, err := readFile(o.Binary, o.MaxBytes)
	if err != nil {
		return nil, err
	}
	if digest(oldData) == digest(data) {
		return nil, nil
	}
	// 검증한 바이트를 비공개 임시 실행 파일에 쓰고 서명된 바이너리 자체의 버전을 조회한다.
	suffix := ""
	if strings.HasSuffix(strings.ToLower(o.Binary), ".exe") {
		suffix = ".exe"
	}
	f, err := os.CreateTemp(filepath.Dir(o.Binary), ".marine-candidate-*"+suffix)
	if err != nil {
		return nil, err
	}
	path := f.Name()
	defer func() { _ = os.Remove(path) }() // 임시 파일 정리. 실패하거나 이미 없어도 결과는 바뀌지 않는다
	if err = f.Chmod(0o700); err == nil {
		_, err = f.Write(data)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return nil, err
	}
	if closeErr != nil {
		return nil, closeErr
	}
	newVersion, err := versionOf(ctx, o, path)
	if err != nil {
		return nil, err
	}
	currentVersion, err := versionOf(ctx, o, o.Binary)
	if err != nil {
		return nil, err
	}
	if !newVersion.GreaterThan(currentVersion) {
		return nil, errors.New("같거나 이전 버전의 업데이트를 거부했습니다")
	}
	return &Candidate{data: data, version: newVersion, maxBytes: o.MaxBytes}, nil
}

type limitedOutput struct{ b []byte }

func (w *limitedOutput) Write(b []byte) (int, error) {
	if len(w.b)+len(b) > 256 {
		return 0, errors.New("버전 출력 크기 초과")
	}
	w.b = append(w.b, b...)
	return len(b), nil
}

func versionOf(ctx context.Context, o Options, path string) (*semver.Version, error) {
	ctx, cancel := context.WithTimeout(ctx, o.Timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, path, "version") // #nosec G204 -- 서명 검증한 staged 바이너리 또는 현재 설치본만 실행한다
	configureBackgroundProcess(cmd)
	cmd.Env = o.env
	var output limitedOutput
	cmd.Stdout = &output
	cmd.WaitDelay = o.StartupTimeout // 버전 명령의 자손이 출력 핸들을 잡아도 무한히 기다리지 않는다.
	if err := cmd.Run(); err != nil {
		return nil, errors.New("업데이트 바이너리 버전 확인 실패")
	}
	v, err := semver.StrictNewVersion(strings.TrimSpace(string(output.b)))
	if err != nil {
		return nil, errors.New("업데이트 바이너리 버전이 SemVer 형식이 아닙니다")
	}
	return v, nil
}

type worker struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser
	done  chan struct{}
	err   error
}

func (w *worker) stop(timeout time.Duration) error {
	_ = w.stdin.Close() // 서비스·감독 프로세스 종료는 stdin EOF로 자식에게 전달한다.
	select {
	case <-w.done:
		return nil
	case <-time.After(timeout):
		killErr := w.cmd.Process.Kill()
		select {
		case <-w.done:
			return errors.New("자식 종료 대기 시간 초과 (강제 종료 확인)")
		case <-time.After(timeout):
			return errors.Join(fmt.Errorf("%w: pid=%d", ErrWorkerRunning, w.cmd.Process.Pid), killErr)
		}
	}
}

func startWorker(ctx context.Context, o Options) (*worker, error) {
	var nonce [24]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, err
	}
	token := hex.EncodeToString(nonce[:])
	ready := filepath.Join(o.Dir, ".update-ready-"+token)
	defer func() { _ = os.Remove(ready) }() // 임시 파일 정리. 실패하거나 이미 없어도 결과는 바뀌지 않는다
	args := append(append([]string(nil), o.WorkerArgs...), "--ready-file", ready, "--ready-token", token, "--parent-stdin")
	cmd := exec.Command(o.Binary, args...) // #nosec G204 -- 현재 설치본·검증 후 설치한 경로, 고정 인자만 실행한다
	configureBackgroundProcess(cmd)
	cmd.Env = o.env
	// nil은 OS devnull 핸들이다. io.Discard로 생기는 불필요한 복사 goroutine·pipe를 만들지 않는다.
	cmd.Stdout, cmd.Stderr = nil, nil // 제품 워커는 자체 순환 로그에 쓴다.
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	if err = cmd.Start(); err != nil {
		_ = stdin.Close()
		return nil, errors.New("업데이트 자식을 시작하지 못했습니다")
	}
	w := &worker{cmd: cmd, stdin: stdin, done: make(chan struct{})}
	go func() { w.err = cmd.Wait(); close(w.done) }()
	startup, cancel := context.WithTimeout(ctx, o.StartupTimeout)
	defer cancel()
	tick := time.NewTicker(20 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-w.done:
			return nil, errors.New("업데이트 자식이 준비 전에 종료됐습니다")
		case <-startup.Done():
			stopErr := w.stop(o.StartupTimeout)
			return nil, errors.Join(errors.New("업데이트 자식 준비 시간 초과"), stopErr)
		case <-tick.C:
			if b, err := os.ReadFile(ready); err == nil && string(b) == token {
				select {
				case <-w.done:
					return nil, errors.New("업데이트 자식이 기동 확인 중 종료됐습니다")
				case <-startup.Done():
					stopErr := w.stop(o.StartupTimeout)
					return nil, errors.Join(errors.New("업데이트 자식 기동 확인 시간 초과"), stopErr)
				case <-time.After(o.Stable):
					return w, nil
				}
			}
		}
	}
}

// Supervisor는 업데이트 중에만 기존 워커를 멈춘다. 새 워커의 로컬 준비·초기 생존 확인 실패는 자동 복원한다.
// 감독 프로세스는 서비스 관리자가 추적하는 원래 프로세스로 남고, 파일별 이전 버전 이름은 고유하다.
func Supervisor(ctx context.Context, o Options) error {
	if err := o.validate(); err != nil {
		return err
	}
	unlock, err := Lock(o.Dir)
	if err != nil {
		return err
	}
	defer unlock()
	if o.Auto {
		if _, err = o.client(); err != nil {
			return err
		}
	}
	w, err := startWorker(ctx, o)
	if err != nil {
		return err
	}
	if o.Log != nil {
		o.Log.Info("워커 기동 확인 완료")
	}
	defer func() {
		if w != nil {
			_ = w.stop(o.StartupTimeout)
		}
	}()
	var timer *time.Timer
	var due <-chan time.Time
	if o.Auto {
		timer = time.NewTimer(0)
		due = timer.C
		defer timer.Stop()
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-w.done:
			return fmt.Errorf("에이전트 자식이 종료됐습니다: %w", w.err)
		case <-due:
			c, fetchErr := Fetch(ctx, o)
			if fetchErr != nil {
				if o.Log != nil {
					o.Log.Warn("업데이트 확인 실패", "error", fetchErr.Error())
				}
			}
			if fetchErr == nil && c != nil {
				applied := false
				if err = w.stop(o.StartupTimeout); err != nil {
					return err
				}
				err = install(o.Binary, c)
				if err == nil {
					w, err = startWorker(ctx, o)
					applied = err == nil
					if err != nil {
						startupErr := err
						if errors.Is(startupErr, ErrWorkerRunning) {
							return startupErr
						}
						if err = Rollback(o.Binary); err != nil {
							return errors.Join(startupErr, err)
						}
						if o.Log != nil {
							o.Log.Warn("새 버전 기동 실패로 이전 버전을 복원했습니다", "error", startupErr.Error())
						}
					}
				} else {
					w = nil
					if o.Log != nil {
						o.Log.Warn("업데이트 적용 실패", "error", err.Error())
					}
				}
				if w == nil {
					if ctx.Err() != nil {
						return nil
					}
					w, err = startWorker(ctx, o)
					if err != nil {
						return err
					}
				}
				if o.Log != nil && applied {
					o.Log.Info("새 버전 기동 확인 완료", "version", c.version.String())
				} else if o.Log != nil {
					o.Log.Warn("이전 버전 워커로 계속 실행합니다")
				}
			}
			timer.Reset(o.Interval)
		}
	}
}

// Manual은 서비스가 멈춘 상태에서만 교체하고 새 워커 기동을 확인한다. 확인 뒤 워커를 닫아 서비스와 중복 실행하지 않는다.
func Manual(ctx context.Context, o Options, file, sig string) error {
	unlock, err := Lock(o.Dir)
	if err != nil {
		return err
	}
	defer unlock()
	c, err := Local(ctx, o, file, sig)
	if err != nil || c == nil {
		return err
	}
	if err = install(o.Binary, c); err != nil {
		return err
	}
	w, err := startWorker(ctx, o)
	if err != nil {
		if errors.Is(err, ErrWorkerRunning) {
			return err
		}
		if rollbackErr := Rollback(o.Binary); rollbackErr != nil {
			return fmt.Errorf("새 버전 기동과 이전 버전 복원 모두 실패했습니다: %w", errors.Join(err, rollbackErr))
		}
		return fmt.Errorf("새 버전 기동 실패, 이전 버전으로 복원했습니다: %w", err)
	}
	return w.stop(o.StartupTimeout)
}
