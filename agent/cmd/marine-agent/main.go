// marine-agent: 해양 실증데이터 플랫폼 동기화 에이전트 (06번 스펙).
//
//	marine-agent enroll --server https://api.example --code ABCD... --watch-root D:\export
//	marine-agent run | status | diag | install | uninstall | start | stop | update --file F --sig S | rollback | version
package main

import (
	"archive/zip"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/kardianos/service"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/config"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/core"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/logx"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/update"
)

var version = "0.1.0-dev" // 빌드 때 -ldflags "-X main.version=..."

func usage() {
	fmt.Fprint(os.Stderr, `사용법: marine-agent <명령> [옵션]
  enroll     등록 코드로 서버에 등록 (--server, --code, --watch-root, [--ca])
  run        포그라운드 실행
  status     연결 상태·대기 파일·마지막 성공·최근 오류
  diag       진단 파일(zip) 만들기 (비밀값 제외)
  install    서비스 설치 (--service-user 등록과 같은 계정, 비밀번호는 OS에서 별도 구성)   uninstall | start | stop
  update     서명 검증 후 수동 업데이트 (--file, --sig)   rollback
  version
공통 옵션: --dir 데이터 폴더 (기본 `+config.DefaultDir()+`, 환경변수 MARINE_AGENT_DIR)
`)
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd, args := os.Args[1], os.Args[2:]
	if cmd == "--version" {
		cmd = "version"
	}
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	dir := fs.String("dir", config.DefaultDir(), "데이터 폴더")
	server := fs.String("server", "", "API 주소 (enroll)")
	code := fs.String("code", "", "등록 코드 (enroll)")
	watchRoot := fs.String("watch-root", "", "감시 폴더 (enroll)")
	ca := fs.String("ca", "", "사설 CA 인증서 PEM (선택). 인증서 검증을 끄는 옵션은 없다")
	out := fs.String("out", "", "진단 파일 경로 (diag)")
	file := fs.String("file", "", "새 바이너리 (update)")
	sig := fs.String("sig", "", "서명 파일 (update)")
	serviceUser := fs.String("service-user", "", "등록과 같은 서비스 계정 이름 (install). 비밀번호를 받지 않습니다")
	readyFile := fs.String("ready-file", "", "내부 워커 준비 파일")
	readyToken := fs.String("ready-token", "", "내부 워커 준비 식별자")
	parentStdin := fs.Bool("parent-stdin", false, "내부 감독 프로세스 종료 감지")
	_ = fs.Parse(args)

	var err error
	switch cmd {
	case "enroll":
		err = enroll(*dir, *server, *code, *watchRoot, *ca)
	case "run":
		err = runForeground(*dir)
	case "status":
		err = status(*dir)
	case "diag":
		err = diag(*dir, *out)
	case "install", "uninstall", "start", "stop":
		err = serviceControl(*dir, cmd, *serviceUser)
	case "update":
		err = manualUpdate(*dir, *file, *sig)
	case "rollback":
		var exe string
		if exe, err = os.Executable(); err == nil {
			var unlock func()
			if unlock, err = update.Lock(*dir); err == nil {
				err = update.Rollback(exe)
				unlock()
			}
		}
	case "version":
		fmt.Println(version)
	case "service-run": // 서비스 관리자가 부르는 진입점
		err = serviceControl(*dir, "run", "")
	case "worker-run": // 감독 프로세스만 사용하는 실제 수집 워커
		err = runWorker(*dir, *readyFile, *readyToken, *parentStdin)
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "오류:", err)
		os.Exit(1)
	}
}

func enroll(dir, server, code, watchRoot, ca string) error {
	if server == "" || code == "" || watchRoot == "" {
		return errors.New("--server, --code, --watch-root가 필요합니다")
	}
	abs, err := filepath.Abs(watchRoot)
	if err != nil {
		return err
	}
	if st, err := os.Stat(abs); err != nil || !st.IsDir() {
		return fmt.Errorf("감시 폴더가 없습니다: %s", abs)
	}
	hc, err := api.NewHTTPClient(ca, 60*time.Second)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	cr, err := api.New(server, hc, nil, version).Enroll(ctx, code, version, core.OSName())
	if err != nil {
		var pr *api.Problem
		if errors.As(err, &pr) { // 서버가 준 사유를 그대로 보여준다 (요구사항 1.2)
			return fmt.Errorf("등록 실패 (%s): %s %s", pr.Code, pr.Title, pr.Detail)
		}
		return err
	}
	cfg := &config.Config{ServerURL: strings.TrimRight(server, "/"), CAFile: ca, WatchRoot: abs, DeviceID: cr.DeviceID}
	cfg.Defaults()
	if err := config.Save(dir, cfg); err != nil {
		return err
	}
	if err := config.SaveCredentials(dir, cr); err != nil {
		return err
	}
	if st, err := state.Open(dir); err == nil {
		_ = st.SetKV(ctx, core.KeyReenroll, "false")
		_ = st.Close()
	}
	fmt.Printf("등록 완료: 수집기 %s\n설정: %s\n이제 'marine-agent install' 또는 'marine-agent run'을 실행하세요.\n",
		cr.DeviceID, filepath.Join(dir, "config.json"))
	return nil
}

type runtimeDeps struct {
	agent *core.Agent
	store *state.Store
	logw  *logx.Rotator
}

func open(dir string, toStderr bool) (*runtimeDeps, error) {
	cfg, err := config.Load(dir)
	if err != nil {
		return nil, err
	}
	cr, err := config.LoadCredentials(dir)
	if err != nil {
		return nil, err
	}
	hc, err := api.NewHTTPClient(cfg.CAFile, 10*time.Minute)
	if err != nil {
		return nil, err
	}
	lw, err := logx.NewRotator(filepath.Join(dir, "logs", "agent.log"), cfg.LogMaxBytes, cfg.LogKeep)
	if err != nil {
		return nil, err
	}
	var w io.Writer = lw
	if toStderr {
		w = io.MultiWriter(lw, os.Stderr)
	}
	log := slog.New(slog.NewJSONHandler(w, &slog.HandlerOptions{Level: slog.LevelInfo}))
	st, err := state.Open(dir)
	if err != nil {
		return nil, err
	}
	a := core.New(cfg, dir, api.New(cfg.ServerURL, hc, cr, version), st, log, version)
	return &runtimeDeps{agent: a, store: st, logw: lw}, nil
}

func runForeground(dir string) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return supervise(ctx, dir, true)
}

// runWorker의 준비 신호는 로컬 설정·자격증명·DB·감시 폴더 확인 뒤에만 쓰며 인터넷 연결을 요구하지 않는다.
func runWorker(dir, readyFile, readyToken string, parentStdin bool) error {
	if readyFile == "" || readyToken == "" || !parentStdin {
		return errors.New("worker-run은 감독 프로세스에서만 실행할 수 있습니다")
	}
	absDir, dirErr := filepath.Abs(dir)
	absReady, readyErrPath := filepath.Abs(readyFile)
	_, tokenErr := hex.DecodeString(readyToken)
	if dirErr != nil || readyErrPath != nil || tokenErr != nil || len(readyToken) != 48 ||
		filepath.Dir(absReady) != absDir || filepath.Base(absReady) != ".update-ready-"+readyToken {
		return errors.New("워커 준비 파일 경로가 올바르지 않습니다")
	}
	d, err := open(dir, true)
	if err != nil {
		return err
	}
	defer d.store.Close()
	defer d.logw.Close()
	if st, statErr := os.Stat(d.agent.Cfg.WatchRoot); statErr != nil || !st.IsDir() {
		return errors.New("감시 폴더가 없어 워커를 시작할 수 없습니다")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() { _, _ = io.Copy(io.Discard, os.Stdin); stop() }()
	readyErr := make(chan error, 1)
	d.agent.OnReady = func() { readyErr <- os.WriteFile(readyFile, []byte(readyToken), 0o600) }
	d.agent.Log.Info("에이전트 시작", "version", version, "watch_root", d.agent.Cfg.WatchRoot)
	runErr := make(chan error, 1)
	go func() { runErr <- d.agent.Run(ctx) }()
	select {
	case err = <-readyErr:
		if err != nil {
			stop()
			<-runErr
			return errors.New("워커 준비 상태를 알리지 못했습니다")
		}
	case err = <-runErr:
		return err
	}
	return <-runErr
}

func updateOptions(dir string, cfg *config.Config, log *slog.Logger) (update.Options, error) {
	exe, err := os.Executable()
	if err != nil {
		return update.Options{}, err
	}
	exe, err = filepath.EvalSymlinks(exe)
	if err != nil {
		return update.Options{}, err
	}
	hc, err := api.NewHTTPClient(cfg.CAFile, time.Duration(cfg.UpdateTimeoutS)*time.Second)
	if err != nil {
		return update.Options{}, err
	}
	return update.Options{Binary: exe, Dir: dir, PublicKey: cfg.UpdatePublicKey,
		Auto: cfg.AutoUpdate, URL: cfg.UpdateURL, SignatureURL: cfg.UpdateSignatureURL,
		Interval: time.Duration(cfg.UpdateIntervalS) * time.Second, Timeout: time.Duration(cfg.UpdateTimeoutS) * time.Second,
		StartupTimeout: time.Duration(cfg.UpdateStartupTimeoutS) * time.Second, Stable: time.Duration(cfg.UpdateStableS) * time.Second,
		MaxBytes: cfg.UpdateMaxBytes, Client: hc, Log: log, WorkerArgs: []string{"worker-run", "--dir", dir}}, nil
}

func supervise(ctx context.Context, dir string, toStderr bool) error {
	cfg, err := config.Load(dir)
	if err != nil {
		return err
	}
	if _, err = config.LoadCredentials(dir); err != nil {
		return err
	}
	lw, err := logx.NewRotator(filepath.Join(dir, "logs", "supervisor.log"), cfg.LogMaxBytes, cfg.LogKeep)
	if err != nil {
		return err
	}
	defer lw.Close()
	var writer io.Writer = lw
	if toStderr {
		writer = io.MultiWriter(lw, os.Stderr)
	}
	log := slog.New(slog.NewJSONHandler(writer, nil))
	o, err := updateOptions(dir, cfg, log)
	if err != nil {
		return err
	}
	return update.Supervisor(ctx, o)
}

func status(dir string) error {
	cfg, err := config.Load(dir)
	if err != nil {
		return err
	}
	st, err := state.Open(dir)
	if err != nil {
		return err
	}
	defer st.Close()
	ctx := context.Background()
	s, err := st.Stats(ctx)
	if err != nil {
		return err
	}
	conn := "확인 중"
	if re, _ := st.KV(ctx, core.KeyReenroll); re == "true" {
		conn = "재등록 필요 (자격증명 폐기)"
	} else if cr, err := config.LoadCredentials(dir); err == nil {
		hc, _ := api.NewHTTPClient(cfg.CAFile, 10*time.Second)
		c := api.New(cfg.ServerURL, hc, cr, version)
		cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		_, err := c.Do(cctx, "GET", "/v1/me", nil, nil, nil)
		cancel()
		switch {
		case err == nil:
			conn = "연결됨"
		case errors.Is(err, api.ErrRevoked):
			conn = "재등록 필요 (자격증명 폐기)"
		default:
			conn = "연결 안 됨"
		}
	}
	last, _ := st.KV(ctx, core.KeyLastSuccess)
	lastErr, _ := st.KV(ctx, core.KeyLastError)
	fmt.Printf("수집기        %s\n서버          %s\n연결 상태     %s\n감시 폴더     %s\n", cfg.DeviceID, cfg.ServerURL, conn, cfg.WatchRoot)
	fmt.Printf("대기 파일     %d개 (%d 바이트)\n완료          %d개\n정책 제외     %d개\n실패          %d개\n", s.QueueItems,
		s.QueueBytes, s.Done, s.Excluded, s.Failed)
	fmt.Printf("마지막 성공   %s\n최근 오류     %s\n", orDash(last), orDash(lastErr))
	return nil
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

// diag는 로그·설정(비밀값 제외)·상태 요약을 zip으로 묶는다 (요구사항 11.3).
func diag(dir, out string) error {
	if out == "" {
		out = filepath.Join(dir, fmt.Sprintf("diag-%s.zip", time.Now().UTC().Format("20060102T150405Z")))
	}
	f, err := os.Create(out)
	if err != nil {
		return err
	}
	defer f.Close()
	zw := zip.NewWriter(f)
	add := func(name string, r io.Reader) error {
		w, err := zw.Create(name)
		if err != nil {
			return err
		}
		_, err = io.Copy(w, r)
		return err
	}
	if cfg, err := config.Load(dir); err == nil {
		// 설정도 MQTT 암호·URL 쿼리 자격증명을 담을 수 있다. 원문을 진단 파일에 넣지 않는다.
		cfg.ServerURL, cfg.UpdateURL, cfg.UpdateSignatureURL = safeURL(cfg.ServerURL), safeURL(cfg.UpdateURL), safeURL(cfg.UpdateSignatureURL)
		for i := range cfg.SensorPlugins {
			cfg.SensorPlugins[i].Username, cfg.SensorPlugins[i].Password = "", ""
			cfg.SensorPlugins[i].Broker = safeURL(cfg.SensorPlugins[i].Broker)
		}
		if b, err := json.MarshalIndent(cfg, "", "  "); err == nil {
			_ = add("config.json", strings.NewReader(string(b)))
		}
	}
	logs, _ := filepath.Glob(filepath.Join(dir, "logs", "agent.log*"))
	supervisorLogs, _ := filepath.Glob(filepath.Join(dir, "logs", "supervisor.log*"))
	logs = append(logs, supervisorLogs...)
	for _, p := range logs {
		if lf, err := os.Open(p); err == nil {
			_ = add("logs/"+filepath.Base(p), lf)
			_ = lf.Close()
		}
	}
	if st, err := state.Open(dir); err == nil {
		ctx := context.Background()
		s, _ := st.Stats(ctx)
		hb, _ := st.KV(ctx, core.KeyLastHeartbeat)
		pol, _ := st.KV(ctx, core.KeyPolicy)
		summary, _ := json.MarshalIndent(map[string]any{"version": version, "stats": s,
			"last_heartbeat": json.RawMessage(nonEmpty(hb)), "policy": json.RawMessage(nonEmpty(pol))}, "", "  ")
		_ = add("summary.json", strings.NewReader(string(summary)))
		_ = st.Close()
	}
	if err := zw.Close(); err != nil {
		return err
	}
	fmt.Println("진단 파일:", out, "(credentials.json은 넣지 않았습니다)")
	return nil
}

func nonEmpty(s string) string {
	if s == "" {
		return "null"
	}
	return s
}

func safeURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "[주소 정보 제외]"
	}
	u.RawQuery, u.Fragment, u.User = "", "", nil
	return u.String()
}

func manualUpdate(dir, file, sig string) error {
	if file == "" || sig == "" {
		return errors.New("--file과 --sig가 필요합니다")
	}
	cfg, err := config.Load(dir)
	if err != nil {
		return err
	}
	o, err := updateOptions(dir, cfg, nil)
	if err != nil {
		return err
	}
	if err := update.Manual(context.Background(), o, file, sig); err != nil {
		return err
	}
	fmt.Println("서명·버전·새 워커 기동 확인 완료. 서비스를 시작하세요. 문제가 있으면 'marine-agent rollback'")
	return nil
}

// ---- 서비스 (kardianos/service, Zlib)

type program struct {
	dir    string
	cancel context.CancelFunc
	done   chan struct{}
}

func (p *program) Start(s service.Service) error {
	ctx, cancel := context.WithCancel(context.Background())
	p.cancel, p.done = cancel, make(chan struct{})
	go func() {
		defer close(p.done)
		if err := supervise(ctx, p.dir, false); err != nil {
			fmt.Fprintln(os.Stderr, "에이전트 감독 프로세스 종료:", err)
			os.Exit(1) // 서비스 관리자의 기존 재시작 정책이 작동하도록 실제 프로세스를 종료한다.
		}
	}()
	return nil
}

func (p *program) Stop(s service.Service) error {
	if p.cancel != nil {
		p.cancel()
		select {
		case <-p.done:
		case <-time.After(30 * time.Second):
		}
	}
	return nil
}

func serviceControl(dir, action, username string) error {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return err
	}
	if action == "install" {
		if _, err = config.LoadCredentials(abs); err != nil {
			return err
		}
		if err = config.CheckServiceUser(username); err != nil {
			return err
		}
	}
	svcCfg := &service.Config{
		Name: "marine-agent", DisplayName: "Marine Data Sync Agent",
		Description: "해양 실증데이터 플랫폼 동기화 에이전트", Arguments: []string{"service-run", "--dir", abs},
		UserName: username,
		Option:   service.KeyValue{"Restart": "always", "OnFailure": "restart"},
	}
	s, err := service.New(&program{dir: abs}, svcCfg)
	if err != nil {
		return err
	}
	if action == "run" {
		return s.Run()
	}
	if err := service.Control(s, action); err != nil {
		return fmt.Errorf("서비스 %s 실패 (관리자 권한이 필요할 수 있습니다): %w", action, err)
	}
	fmt.Println("서비스", action, "완료")
	return nil
}
