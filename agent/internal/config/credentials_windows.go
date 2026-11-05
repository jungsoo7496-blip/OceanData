//go:build windows

package config

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"runtime"
	"unsafe"

	"golang.org/x/sys/windows"
)

const dpapiProtection = "DPAPI_CURRENT_USER"

var errCredentialProtection = errors.New("Windows DPAPI로 자격증명을 보호하거나 복호화하지 못했습니다. 등록한 Windows 실행 계정과 컴퓨터를 확인하세요")

type protectedCredentials struct {
	Version    int    `json:"version"`
	Protection string `json:"protection"`
	Data       []byte `json:"data"`
}

type credentialPayload struct {
	Data   []byte `json:"credentials"`
	SHA256 []byte `json:"sha256"`
}

func protectCredentials(plain []byte) ([]byte, error) {
	sum := sha256.Sum256(plain)
	sealed, err := json.Marshal(credentialPayload{Data: plain, SHA256: sum[:]})
	if err != nil {
		return nil, errCredentialProtection
	}
	defer clear(sealed)
	encrypted, err := dpapi(sealed, true)
	if err != nil {
		return nil, err
	}
	return json.Marshal(protectedCredentials{Version: 1, Protection: dpapiProtection, Data: encrypted})
}

func unprotectCredentials(b []byte) ([]byte, bool, error) {
	// 명확한 구형 자격증명 JSON만 이행한다. 깨진 보호 파일을 임의의 평문으로 해석하지 않는다.
	if _, err := parseCredentials(b); err == nil {
		return bytes.Clone(b), true, nil
	}
	var envelope protectedCredentials
	if strictJSON(b, &envelope) != nil || envelope.Version != 1 || envelope.Protection != dpapiProtection || len(envelope.Data) == 0 {
		return nil, false, errCredentialFormat
	}
	sealed, err := dpapi(envelope.Data, false)
	if err != nil {
		return nil, false, err
	}
	defer clear(sealed)
	var payload credentialPayload
	if strictJSON(sealed, &payload) != nil {
		return nil, false, errCredentialProtection
	}
	defer clear(payload.Data)
	sum := sha256.Sum256(payload.Data)
	if subtle.ConstantTimeCompare(sum[:], payload.SHA256) != 1 {
		return nil, false, errCredentialProtection
	}
	return bytes.Clone(payload.Data), false, nil
}

func dpapi(data []byte, protect bool) ([]byte, error) {
	if len(data) == 0 || uint64(len(data)) > uint64(^uint32(0)) {
		return nil, errCredentialProtection
	}
	in := windows.DataBlob{Size: uint32(len(data)), Data: &data[0]} // #nosec G115 -- 위에서 Win32 DWORD 길이 범위를 검사했다.
	var out windows.DataBlob
	var err error
	if protect {
		// LOCAL_MACHINE은 쓰지 않는다. UI 없는 서비스에서도 CurrentUser 범위를 그대로 강제한다.
		err = windows.CryptProtectData(&in, nil, nil, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out)
	} else {
		err = windows.CryptUnprotectData(&in, nil, nil, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out)
	}
	runtime.KeepAlive(data)
	if err != nil {
		return nil, errCredentialProtection
	}
	if out.Data == nil {
		return nil, errCredentialProtection
	}
	// DPAPI가 할당한 포인터만 한 번 해제하며, Go 메모리로 복사한 뒤 원본을 지운다.
	defer windows.LocalFree(windows.Handle(uintptr(unsafe.Pointer(out.Data)))) // #nosec G103 -- Windows LocalAlloc/LocalFree 경계.
	if uint64(out.Size) > uint64(^uint(0)>>1) {
		return nil, errCredentialProtection
	}
	buf := unsafe.Slice(out.Data, int(out.Size)) // #nosec G103 -- OS 반환 길이를 확인한 DPAPI 버퍼에 한정한다.
	defer clear(buf)
	return bytes.Clone(buf), nil
}

func checkCredentialPermissions(string) error {
	// Windows의 os.FileMode 0600은 사용자별 ACL을 뜻하지 않는다. 기밀성은 CurrentUser DPAPI가 맡는다.
	return nil
}
