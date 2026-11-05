package config

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

const syntheticSecret = "synthetic-device-secret-only-for-tests-0199"

func testCredentials() *api.Credentials {
	return &api.Credentials{DeviceID: "dev_synthetic", ClientID: "dcr_synthetic", ClientSecret: syntheticSecret,
		TokenEndpoint: "/v1/auth/token"}
}

func TestCredentialsRoundTripAndReplacement(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested")
	want := testCredentials()
	if err := SaveCredentials(dir, want); err != nil {
		t.Fatal(err)
	}
	got, err := LoadCredentials(dir)
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatal("자격증명 왕복 저장 실패", err)
	}
	want.ClientSecret += "-rotated"
	if err := SaveCredentials(dir, want); err != nil {
		t.Fatal(err)
	}
	got, err = LoadCredentials(dir)
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatal("자격증명 교체 실패", err)
	}
}

func TestCredentialErrorsDoNotExposePlaintext(t *testing.T) {
	dir := t.TempDir()
	malformed := []byte(`{"client_secret":"` + syntheticSecret + `","device_id":`)
	if err := os.WriteFile(filepath.Join(dir, "credentials.json"), malformed, 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := LoadCredentials(dir)
	if got != nil || err == nil || strings.Contains(err.Error(), syntheticSecret) || strings.Contains(err.Error(), "client_secret") {
		t.Fatal("손상된 파일은 자격증명·원문을 노출하지 않는 오류여야 합니다")
	}
	if err := SaveCredentials(dir, nil); err == nil || strings.Contains(err.Error(), syntheticSecret) {
		t.Fatal("빈 자격증명을 저장하면 안 됩니다")
	}
}

func TestCredentialWriteFailureCleansTemporaryFiles(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "credentials.json"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := SaveCredentials(dir, testCredentials()); err == nil || strings.Contains(err.Error(), syntheticSecret) {
		t.Fatal("파일 교체 실패를 성공으로 처리하면 안 됩니다")
	}
	files, err := os.ReadDir(dir)
	if err != nil || len(files) != 1 || files[0].Name() != "credentials.json" {
		t.Fatal("실패 후 자격증명 임시 파일이 남았습니다", err)
	}
}

func TestCredentialWritersDoNotShareFixedTemporaryFile(t *testing.T) {
	dir := t.TempDir()
	oldTmp := filepath.Join(dir, "credentials.json.tmp")
	if err := os.WriteFile(oldTmp, []byte("기존 파일은 건드리지 않는다"), 0o600); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			if err := SaveCredentials(dir, testCredentials()); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if got, err := LoadCredentials(dir); err != nil || !reflect.DeepEqual(got, testCredentials()) {
		t.Fatal("동시 쓰기 뒤 완전한 자격증명이 남아야 합니다", err)
	}
	data, err := os.ReadFile(oldTmp)
	if err != nil || !bytes.Equal(data, []byte("기존 파일은 건드리지 않는다")) {
		t.Fatal("기존 고정 이름 임시 파일을 덮어썼습니다", err)
	}
}
