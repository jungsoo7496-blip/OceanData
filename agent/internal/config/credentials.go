package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

var errCredentialFormat = errors.New("자격증명 파일 형식이 올바르지 않습니다. 같은 실행 계정으로 다시 등록하세요")

// SaveCredentials는 Unix에서 0600, Windows에서 현재 사용자 DPAPI로 보호해 저장한다.
// Windows 등록·실행 계정은 같아야 하며 다른 계정/컴퓨터에서 복호화하는 우회는 제공하지 않는다.
func SaveCredentials(dir string, cr *api.Credentials) error {
	if !validCredentials(cr) {
		return errCredentialFormat
	}
	plain, err := json.Marshal(cr) // #nosec G117 -- OS별 보호를 거쳐서만 디스크에 저장한다.
	if err != nil {
		return errCredentialFormat
	}
	defer clear(plain)
	protected, err := protectCredentials(plain)
	if err != nil {
		return err
	}
	defer clear(protected)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("자격증명 저장 폴더를 준비하지 못했습니다: %w", err)
	}
	if err := writeAtomic(filepath.Join(dir, "credentials.json"), protected, 0o600); err != nil {
		return fmt.Errorf("자격증명을 안전하게 저장하지 못했습니다: %w", err)
	}
	return nil
}

func LoadCredentials(dir string) (*api.Credentials, error) {
	p := filepath.Join(dir, "credentials.json")
	if err := checkCredentialPermissions(p); err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, fmt.Errorf("자격증명을 읽지 못했습니다. 'marine-agent enroll'로 등록하세요: %w", err)
	}
	defer clear(b)
	plain, migrate, err := unprotectCredentials(b)
	if err != nil {
		return nil, err
	}
	defer clear(plain)
	cr, err := parseCredentials(plain)
	if err != nil {
		return nil, err
	}
	if migrate {
		// 기존 평문을 읽었어도 DPAPI 보호와 파일 교체가 끝나기 전에는 사용 가능한 자격증명으로 반환하지 않는다.
		if err := SaveCredentials(dir, cr); err != nil {
			return nil, fmt.Errorf("기존 자격증명의 보호 변환에 실패했습니다. 같은 실행 계정으로 다시 등록하세요: %w", err)
		}
	}
	return cr, nil
}

func validCredentials(cr *api.Credentials) bool {
	return cr != nil && cr.DeviceID != "" && cr.ClientID != "" && cr.ClientSecret != "" && cr.TokenEndpoint != ""
}

func strictJSON(b []byte, out any) error {
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if err := d.Decode(out); err != nil {
		return errCredentialFormat
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return errCredentialFormat
	}
	return nil
}

func parseCredentials(b []byte) (*api.Credentials, error) {
	var cr api.Credentials
	if strictJSON(b, &cr) != nil || !validCredentials(&cr) {
		return nil, errCredentialFormat
	}
	return &cr, nil
}
