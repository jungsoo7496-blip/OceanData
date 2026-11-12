package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// 07번 tasks 14: MQTT 브로커 암호 같은 작은 비밀값을 자격증명과 같은 OS 보호로 저장한다
// (Windows: 현재 사용자 DPAPI, Unix: 0600 파일). 설정 파일(config.json)에는 비밀 이름만 남긴다.

var secretNamePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,80}$`)

var errSecretFormat = errors.New("비밀값 파일 형식이 올바르지 않습니다. 같은 실행 계정으로 다시 저장하세요")

type secretFile struct {
	Version int    `json:"version"`
	Value   string `json:"value"`
}

func secretPath(dir, name string) (string, error) {
	if !secretNamePattern.MatchString(name) {
		return "", fmt.Errorf("비밀 이름이 올바르지 않습니다: %q", name)
	}
	return filepath.Join(dir, "secrets", name+".json"), nil
}

// SaveSecret은 값을 OS 보호를 거쳐 dir/secrets/<name>.json에 원자적으로 쓴다.
func SaveSecret(dir, name, value string) error {
	p, err := secretPath(dir, name)
	if err != nil {
		return err
	}
	if value == "" {
		return errors.New("빈 비밀값은 저장하지 않습니다")
	}
	plain, err := json.Marshal(secretFile{Version: 1, Value: value}) // #nosec G117 -- OS별 보호를 거쳐서만 쓴다.
	if err != nil {
		return errSecretFormat
	}
	defer clear(plain)
	protected, err := protectSecret(plain)
	if err != nil {
		return err
	}
	defer clear(protected)
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return fmt.Errorf("비밀값 저장 폴더를 준비하지 못했습니다: %w", err)
	}
	return writeAtomic(p, protected, 0o600)
}

// LoadSecret은 SaveSecret으로 저장한 값을 읽는다. Unix에서는 권한이 넓으면 거부한다.
func LoadSecret(dir, name string) (string, error) {
	p, err := secretPath(dir, name)
	if err != nil {
		return "", err
	}
	if err := checkCredentialPermissions(p); err != nil {
		return "", err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return "", fmt.Errorf("비밀값 %q을 읽지 못했습니다: %w", name, err)
	}
	defer clear(b)
	plain, err := unprotectSecret(b)
	if err != nil {
		return "", err
	}
	defer clear(plain)
	var f secretFile
	if strictJSON(plain, &f) != nil || f.Version != 1 || f.Value == "" {
		return "", errSecretFormat
	}
	return f.Value, nil
}

func sensorSecretName(sourceDeviceID string) string {
	var b strings.Builder
	for _, r := range sourceDeviceID {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' || r == '-' {
			b.WriteRune(r)
		}
	}
	return "mqtt-" + b.String()
}

// MigrateSensorSecrets는 config.json에 평문으로 남은 MQTT 암호를 보안 저장소로 옮기고 설정에서 지운다.
// 옮긴 개수를 돌려준다. 저장에 실패하면 설정을 바꾸지 않는다.
func MigrateSensorSecrets(dir string, c *Config) (int, error) {
	moved := 0
	for i := range c.SensorPlugins {
		p := &c.SensorPlugins[i]
		if p.Password == "" {
			continue
		}
		name := p.PasswordSecret
		if name == "" {
			name = sensorSecretName(p.SourceDeviceID)
		}
		if err := SaveSecret(dir, name, p.Password); err != nil {
			return 0, fmt.Errorf("MQTT 암호를 보안 저장소로 옮기지 못했습니다: %w", err)
		}
		p.PasswordSecret, p.Password = name, ""
		moved++
	}
	if moved > 0 {
		if err := Save(dir, c); err != nil {
			return 0, err
		}
	}
	return moved, nil
}

// ResolveSensorSecrets는 비밀 이름으로 암호를 읽어 메모리(ResolvedPassword)에만 채운다.
func ResolveSensorSecrets(dir string, c *Config) error {
	for i := range c.SensorPlugins {
		p := &c.SensorPlugins[i]
		if p.PasswordSecret == "" {
			continue
		}
		v, err := LoadSecret(dir, p.PasswordSecret)
		if err != nil {
			return err
		}
		p.ResolvedPassword = v
	}
	return nil
}
