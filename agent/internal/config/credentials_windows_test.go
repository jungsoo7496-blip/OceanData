//go:build windows

package config

import (
	"bytes"
	"encoding/json"
	"os"
	"os/user"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

func TestWindowsCredentialsAreOpaqueOnDisk(t *testing.T) {
	dir := t.TempDir()
	if err := SaveCredentials(dir, testCredentials()); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(dir, "credentials.json"))
	if err != nil || bytes.Contains(data, []byte(syntheticSecret)) || bytes.Contains(data, []byte("client_secret")) {
		t.Fatal("기기 자격증명 평문을 디스크에 기록하면 안 됩니다", err)
	}
	var envelope protectedCredentials
	if err := strictJSON(data, &envelope); err != nil || envelope.Version != 1 || envelope.Protection != dpapiProtection {
		t.Fatal("DPAPI CurrentUser 보호 형식이 아닙니다", err)
	}
}

func TestWindowsLegacyCredentialsMigrateBeforeReturn(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "credentials.json")
	plain, err := json.Marshal(testCredentials())
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, plain, 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := LoadCredentials(dir)
	if err != nil || !reflect.DeepEqual(got, testCredentials()) {
		t.Fatal("구형 자격증명 변환 실패", err)
	}
	data, err := os.ReadFile(p)
	if err != nil || bytes.Contains(data, []byte(syntheticSecret)) || bytes.Equal(data, plain) {
		t.Fatal("반환 전에 평문을 보호 파일로 교체해야 합니다", err)
	}
	if got, err = LoadCredentials(dir); err != nil || !reflect.DeepEqual(got, testCredentials()) {
		t.Fatal("이행한 보호 파일을 다시 읽지 못했습니다", err)
	}
}

func TestWindowsMigrationFailureDoesNotReturnPlainCredentials(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "credentials.json")
	plain, _ := json.Marshal(testCredentials())
	if err := os.WriteFile(p, plain, 0o600); err != nil {
		t.Fatal(err)
	}
	// Windows 읽기 전용 속성은 기존 파일의 교체를 막는다. 관리자 권한·서비스 설치는 필요 없다.
	if err := os.Chmod(p, 0o400); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(p, 0o600) })
	got, err := LoadCredentials(dir)
	if err == nil || got != nil || strings.Contains(err.Error(), syntheticSecret) {
		t.Fatal("보호 변환에 실패하면 평문 자격증명 사용을 중지해야 합니다")
	}
	after, err := os.ReadFile(p)
	if err != nil || !bytes.Equal(plain, after) {
		t.Fatal("실패한 이행은 원본 파일을 보존해야 합니다", err)
	}
	files, err := os.ReadDir(dir)
	if err != nil || len(files) != 1 {
		t.Fatal("변환 실패 후 임시 파일이 남았습니다", err)
	}
}

func TestWindowsCorruptProtectedCredentialsFailClosed(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "credentials.json")
	if err := SaveCredentials(dir, testCredentials()); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	var envelope protectedCredentials
	if err := strictJSON(b, &envelope); err != nil {
		t.Fatal(err)
	}
	envelope.Data[len(envelope.Data)/2] ^= 0x40
	broken, _ := json.Marshal(envelope)
	if err := os.WriteFile(p, broken, 0o600); err != nil {
		t.Fatal(err)
	}
	if cr, err := LoadCredentials(dir); err == nil || cr != nil || strings.Contains(err.Error(), syntheticSecret) {
		t.Fatal("손상 암호문은 오류이며 비밀값을 노출하면 안 됩니다")
	}
}

func TestWindowsChecksPayloadIntegrityAfterDPAPI(t *testing.T) {
	dir := t.TempDir()
	plain, _ := json.Marshal(testCredentials())
	sealed, _ := json.Marshal(credentialPayload{Data: plain, SHA256: []byte("합성 불일치 해시")})
	encrypted, err := dpapi(sealed, true)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(protectedCredentials{Version: 1, Protection: dpapiProtection, Data: encrypted})
	if err := os.WriteFile(filepath.Join(dir, "credentials.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	if cr, err := LoadCredentials(dir); err == nil || cr != nil {
		t.Fatal("DPAPI 호출 성공만으로 손상된 내부 데이터를 사용하면 안 됩니다")
	}
}

func TestWindowsUnsupportedEnvelopeIsNotLegacyPlaintext(t *testing.T) {
	for _, data := range []string{
		`{"version":2,"protection":"DPAPI_CURRENT_USER","data":"c3ludGhldGlj"}`,
		`{"version":1,"protection":"DPAPI_LOCAL_MACHINE","data":"c3ludGhldGlj"}`,
		`{"client_secret":"` + syntheticSecret + `"}`,
	} {
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, "credentials.json"), []byte(data), 0o600); err != nil {
			t.Fatal(err)
		}
		if cr, err := LoadCredentials(dir); err == nil || cr != nil || strings.Contains(err.Error(), syntheticSecret) {
			t.Fatal("모르는 보호 형식이나 불완전한 평문은 거부해야 합니다")
		}
	}
}

func TestWindowsServiceUserMustMatchEnrollmentIdentity(t *testing.T) {
	current, err := user.Current()
	if err != nil {
		t.Fatal(err)
	}
	if err := CheckServiceUser(current.Username); err != nil {
		t.Fatal("같은 사용자 계정은 SID가 일치해야 합니다", err)
	}
	tokenUser, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		t.Fatal(err)
	}
	system, err := windows.CreateWellKnownSid(windows.WinLocalSystemSid)
	if err != nil {
		t.Fatal(err)
	}
	if !tokenUser.User.Sid.Equals(system) && CheckServiceUser("") == nil {
		t.Fatal("일반 사용자 DPAPI를 기본 LocalSystem 서비스에서 쓰면 안 됩니다")
	}
	if CheckServiceUser(`.\marine-agent-nonexistent-synthetic-account-0199`) == nil {
		t.Fatal("확인하지 못한 서비스 계정을 허용하면 안 됩니다")
	}
}
