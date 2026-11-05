package main

import (
	"archive/zip"
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/config"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/sensor"
)

func TestDiagnosticArchiveExcludesCredentialsAndOriginals(t *testing.T) {
	base := t.TempDir()
	dir, watch := filepath.Join(base, "agent"), filepath.Join(base, "watch")
	if err := os.MkdirAll(watch, 0o700); err != nil {
		t.Fatal(err)
	}
	const deviceSecret = "synthetic-diag-device-secret-0619"
	const sensorSecret = "synthetic-diag-sensor-secret-0619"
	const sensorUser = "synthetic-diag-sensor-user-0619"
	original := []byte("synthetic-watch-original-content-0619\n")
	source := filepath.Join(watch, "original.csv")
	if err := os.WriteFile(source, original, 0o600); err != nil {
		t.Fatal(err)
	}
	secretURL := "https://synthetic:" + deviceSecret + "@example.invalid/asset?key=" + deviceSecret + "#" + deviceSecret
	cfg := &config.Config{ServerURL: secretURL, WatchRoot: watch, DeviceID: "dev_synthetic_diag",
		UpdateURL: secretURL, UpdateSignatureURL: secretURL,
		SensorPlugins: []sensor.Config{{SourceDeviceID: "dev_synthetic_sensor", ClientID: "synthetic",
			Username: sensorUser, Password: sensorSecret,
			Broker: "ssl://synthetic:" + sensorSecret + "@example.invalid:8883?key=" + sensorSecret + "#" + sensorSecret}}}
	cfg.Defaults()
	if err := config.Save(dir, cfg); err != nil {
		t.Fatal(err)
	}
	if err := config.SaveCredentials(dir, &api.Credentials{DeviceID: cfg.DeviceID, ClientID: "dcr_synthetic",
		ClientSecret: deviceSecret, TokenEndpoint: "/v1/auth/token"}); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "credentials.json.tmp"), []byte(deviceSecret), 0o600); err != nil {
		t.Fatal(err)
	}
	beforeConfig, err := os.ReadFile(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	beforeCredentials, err := os.ReadFile(filepath.Join(dir, "credentials.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "logs"), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"agent.log", "supervisor.log.1"} {
		if err := os.WriteFile(filepath.Join(dir, "logs", name), []byte("합성 상태 기록\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	archive := filepath.Join(base, "diagnostic.zip")
	if err := diag(dir, archive); err != nil {
		t.Fatal(err)
	}
	bundle, err := zip.OpenReader(archive)
	if err != nil {
		t.Fatal(err)
	}
	defer bundle.Close()
	entries := map[string]bool{}
	for _, file := range bundle.File {
		entries[file.Name] = true
		if strings.Contains(strings.ToLower(file.Name), "credentials") || file.Name == "original.csv" {
			t.Fatal("자격증명 파일·감시 원본을 진단 파일에 포함하면 안 됩니다")
		}
		r, err := file.Open()
		if err != nil {
			t.Fatal(err)
		}
		data, err := io.ReadAll(r)
		_ = r.Close()
		if err != nil {
			t.Fatal(err)
		}
		for _, secret := range [][]byte{[]byte(deviceSecret), []byte(sensorSecret), []byte(sensorUser), original} {
			if bytes.Contains(data, secret) {
				t.Fatalf("진단 항목 %s에 합성 비밀값 또는 원본이 포함됐습니다", file.Name)
			}
		}
	}
	for _, required := range []string{"config.json", "summary.json", "logs/agent.log", "logs/supervisor.log.1"} {
		if !entries[required] {
			t.Fatalf("진단에 필요한 항목이 없습니다: %s", required)
		}
	}
	for path, expected := range map[string][]byte{
		filepath.Join(dir, "config.json"):      beforeConfig,
		filepath.Join(dir, "credentials.json"): beforeCredentials,
		source:                                 original,
	} {
		got, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(got, expected) {
			t.Fatal("진단을 만드는 동안 원본 설정·자격증명·감시 파일이 바뀌었습니다", err)
		}
	}
}
