//go:build !windows

package config

import (
	"bytes"
	"fmt"
	"os"
)

func protectCredentials(plain []byte) ([]byte, error) {
	return bytes.Clone(plain), nil
}

func unprotectCredentials(b []byte) ([]byte, bool, error) {
	return bytes.Clone(b), false, nil
}

func checkCredentialPermissions(p string) error {
	if st, err := os.Stat(p); err == nil && st.Mode().Perm()&0o077 != 0 {
		return fmt.Errorf("자격증명 파일 권한이 너무 넓습니다 (%v). chmod 600 %s", st.Mode().Perm(), p)
	}
	return nil
}
