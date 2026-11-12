//go:build !windows

package config

import "bytes"

// Unix에서는 파일 권한(0600, 실행 계정 소유)으로 보호한다. 자격증명 파일과 같은 방식이다.
func protectSecret(plain []byte) ([]byte, error) { return bytes.Clone(plain), nil }

func unprotectSecret(b []byte) ([]byte, error) { return bytes.Clone(b), nil }
