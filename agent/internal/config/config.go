// Package config는 에이전트 로컬 설정(서버 주소·감시 루트 등)과 자격증명 파일을 다룬다.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sync"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/sensor"
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
	// 자동 업데이트는 기본 비활성이다. 주소·주기·한도는 현장 설정에서 지정한다.
	AutoUpdate            bool   `json:"auto_update"`
	UpdateURL             string `json:"update_url,omitempty"`
	UpdateSignatureURL    string `json:"update_signature_url,omitempty"`
	UpdateIntervalS       int    `json:"update_interval_s"`
	UpdateTimeoutS        int    `json:"update_timeout_s"`
	UpdateMaxBytes        int64  `json:"update_max_bytes"`
	UpdateStartupTimeoutS int    `json:"update_startup_timeout_s"`
	UpdateStableS         int    `json:"update_stable_s"`
	// SensorPlugins는 MQTT 등 Sensor_Plugin 설정이다 (07번). 비어 있으면 쓰지 않는다.
	SensorPlugins []sensor.Config `json:"sensor_plugins,omitempty"`
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
	if c.UpdateIntervalS == 0 {
		c.UpdateIntervalS = 3600
	}
	if c.UpdateTimeoutS == 0 {
		c.UpdateTimeoutS = 120
	}
	if c.UpdateMaxBytes == 0 {
		c.UpdateMaxBytes = 64 << 20
	}
	if c.UpdateStartupTimeoutS == 0 {
		c.UpdateStartupTimeoutS = 30
	}
	if c.UpdateStableS == 0 {
		c.UpdateStableS = 5
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

var atomicWriteMu sync.Mutex

func writeAtomic(p string, b []byte, mode os.FileMode) error {
	// Windows 파일 교체는 같은 프로세스의 동시 Rename도 거부할 수 있으므로 드문 설정 쓰기를 직렬화한다.
	atomicWriteMu.Lock()
	defer atomicWriteMu.Unlock()
	// 고정 .tmp 파일을 열지 않아 동시 저장 충돌과 기존 심볼릭 링크를 따라 쓰는 일을 피한다.
	f, err := os.CreateTemp(filepath.Dir(p), "."+filepath.Base(p)+"-*")
	if err != nil {
		return err
	}
	defer f.Close()
	defer os.Remove(f.Name())
	if err := f.Chmod(mode); err != nil {
		return err
	}
	if _, err := f.Write(b); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), p)
}
