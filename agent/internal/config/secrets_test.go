//go:build !windows

package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/sensor"
)

func TestSecretRoundTripAndPermissions(t *testing.T) {
	dir := t.TempDir()
	if err := SaveSecret(dir, "mqtt-dev_1", "s3cr3t-합성"); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, "secrets", "mqtt-dev_1.json")
	st, err := os.Stat(p)
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("권한 0600이어야 한다: %v %v", st.Mode().Perm(), err)
	}
	if v, err := LoadSecret(dir, "mqtt-dev_1"); err != nil || v != "s3cr3t-합성" {
		t.Fatalf("읽기 %q %v", v, err)
	}
	_ = os.Chmod(p, 0o644)
	if _, err := LoadSecret(dir, "mqtt-dev_1"); err == nil {
		t.Fatal("권한이 넓은 비밀 파일은 거부해야 한다")
	}
	for _, bad := range []string{"../x", "", "a/b", strings.Repeat("a", 81)} {
		if err := SaveSecret(dir, bad, "v"); err == nil {
			t.Fatalf("잘못된 이름 %q를 받았다", bad)
		}
	}
	if err := SaveSecret(dir, "empty", ""); err == nil {
		t.Fatal("빈 값은 저장하지 않는다")
	}
	_ = os.WriteFile(filepath.Join(dir, "secrets", "broken.json"), []byte(`{"version":2,"value":"x"}`), 0o600)
	if _, err := LoadSecret(dir, "broken"); err == nil {
		t.Fatal("알 수 없는 형식은 거부해야 한다")
	}
}

func TestMigrateSensorSecretsRemovesPlaintextFromConfig(t *testing.T) {
	dir := t.TempDir()
	c := &Config{ServerURL: "https://example.invalid", SensorPlugins: []sensor.Config{
		{SourceDeviceID: "dev_01ABC", Broker: "ssl://broker.invalid:8883", ClientID: "c1", Username: "u", Password: "plain-pw"},
		{SourceDeviceID: "dev_02DEF", Broker: "tcp://b:1883", ClientID: "c2"},
	}}
	if err := Save(dir, c); err != nil {
		t.Fatal(err)
	}
	loaded, err := Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	n, err := MigrateSensorSecrets(dir, loaded)
	if err != nil || n != 1 {
		t.Fatalf("옮긴 수 %d %v", n, err)
	}
	raw, _ := os.ReadFile(filepath.Join(dir, "config.json"))
	if strings.Contains(string(raw), "plain-pw") || !strings.Contains(string(raw), `"password_secret": "mqtt-dev_01ABC"`) {
		t.Fatalf("설정에 평문 암호가 남았거나 비밀 이름이 없다:\n%s", raw)
	}
	again, _ := Load(dir)
	if n, _ := MigrateSensorSecrets(dir, again); n != 0 {
		t.Fatal("두 번째 실행은 옮길 것이 없어야 한다")
	}
	if err := ResolveSensorSecrets(dir, again); err != nil {
		t.Fatal(err)
	}
	if again.SensorPlugins[0].ResolvedPassword != "plain-pw" || again.SensorPlugins[0].Password != "" {
		t.Fatalf("메모리에만 채워야 한다: %+v", again.SensorPlugins[0])
	}
	// 메모리의 값은 설정을 다시 저장해도 파일에 쓰이지 않는다
	if err := Save(dir, again); err != nil {
		t.Fatal(err)
	}
	raw, _ = os.ReadFile(filepath.Join(dir, "config.json"))
	if strings.Contains(string(raw), "plain-pw") {
		t.Fatal("ResolvedPassword가 설정 파일에 쓰였다")
	}
	// 비밀 파일이 없으면 시작하지 않는다 (조용히 빈 암호로 붙지 않는다)
	_ = os.Remove(filepath.Join(dir, "secrets", "mqtt-dev_01ABC.json"))
	if err := ResolveSensorSecrets(dir, again); err == nil {
		t.Fatal("비밀 파일이 없는데 통과했다")
	}
}
