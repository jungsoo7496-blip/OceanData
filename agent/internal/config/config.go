// Package config는 에이전트 로컬 설정(서버 주소·감시 루트 등)과 자격증명 파일을 다룬다.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

type Config struct {
	ServerURL      string `json:"server_url"`
	CAFile         string `json:"ca_file,omitempty"`
	WatchRoot      string `json:"watch_root"`
	DeviceID       string `json:"device_id"`
	ScanIntervalS  int    `json:"scan_interval_s"`
	PolicyRefreshS int    `json:"policy_refresh_s"`
	MaxBackoffS    int    `json:"max_backoff_s"`
	LowDiskBytes   int64  `json:"low_disk_bytes"`
	LogMaxBytes    int64  `json:"log_max_bytes"`
	LogKeep        int    `json:"log_keep"`
	// UpdatePublicKey는 수동·자동 업데이트 서명 검증용 Ed25519 공개키(base64)다.
	UpdatePublicKey string `json:"update_public_key,omitempty"`
}

func (c *Config) Defaults() {
	if c.ScanIntervalS == 0 {
		c.ScanIntervalS = 10
	}
	if c.PolicyRefreshS == 0 {
		c.PolicyRefreshS = 600
	}
	if c.MaxBackoffS == 0 {
		c.MaxBackoffS = 900
	}
	if c.LowDiskBytes == 0 {
		c.LowDiskBytes = 5 << 30
	}
	if c.LogMaxBytes == 0 {
		c.LogMaxBytes = 10 << 20
	}
	if c.LogKeep == 0 {
		c.LogKeep = 5
	}
}

// DefaultDir는 OS별 데이터 폴더다. MARINE_AGENT_DIR로 바꿀 수 있다.
func DefaultDir() string {
	if d := os.Getenv("MARINE_AGENT_DIR"); d != "" {
		return d
	}
	if runtime.GOOS == "windows" {
		base := os.Getenv("ProgramData")
		if base == "" {
			base = `C:\ProgramData`
		}
		return filepath.Join(base, "MarineAgent")
	}
	return "/var/lib/marine-agent"
}

func Load(dir string) (*Config, error) {
	b, err := os.ReadFile(filepath.Join(dir, "config.json"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("설정이 없습니다. 먼저 'marine-agent enroll'을 실행하세요 (%s)", dir)
		}
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("config.json 형식 오류: %w", err)
	}
	c.Defaults()
	return &c, nil
}

func Save(dir string, c *Config) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(c, "", "  ")
	return writeAtomic(filepath.Join(dir, "config.json"), b, 0o600)
}

// SaveCredentials는 자격증명을 권한 0600 파일에 저장한다.
// 확인 필요: Windows는 DPAPI 보호로 바꿔야 한다 (security.md). 지금은 ProgramData 아래 0600 파일이다.
func SaveCredentials(dir string, cr *api.Credentials) error {
	b, _ := json.Marshal(cr) // #nosec G117 -- 자격증명 저장이 목적이다. 권한 0600 파일에만 쓴다
	return writeAtomic(filepath.Join(dir, "credentials.json"), b, 0o600)
}

func LoadCredentials(dir string) (*api.Credentials, error) {
	p := filepath.Join(dir, "credentials.json")
	if runtime.GOOS != "windows" {
		if st, err := os.Stat(p); err == nil && st.Mode().Perm()&0o077 != 0 {
			return nil, fmt.Errorf("자격증명 파일 권한이 너무 넓습니다 (%v). chmod 600 %s", st.Mode().Perm(), p)
		}
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, fmt.Errorf("자격증명이 없습니다. 'marine-agent enroll'로 등록하세요: %w", err)
	}
	var cr api.Credentials
	if err := json.Unmarshal(b, &cr); err != nil {
		return nil, err
	}
	return &cr, nil
}

func writeAtomic(p string, b []byte, mode os.FileMode) error {
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, b, mode); err != nil {
		return err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}
