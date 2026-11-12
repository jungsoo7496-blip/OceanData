//go:build windows

package config

import "encoding/json"

// Windows에서는 자격증명과 같은 현재 사용자 DPAPI 봉투를 쓴다. 평문 파일은 받지 않는다(이전 형식이 없다).
func protectSecret(plain []byte) ([]byte, error) {
	encrypted, err := dpapi(plain, true)
	if err != nil {
		return nil, err
	}
	return json.Marshal(protectedCredentials{Version: 1, Protection: dpapiProtection, Data: encrypted})
}

func unprotectSecret(b []byte) ([]byte, error) {
	var envelope protectedCredentials
	if strictJSON(b, &envelope) != nil || envelope.Version != 1 || envelope.Protection != dpapiProtection ||
		len(envelope.Data) == 0 {
		return nil, errSecretFormat
	}
	return dpapi(envelope.Data, false)
}
