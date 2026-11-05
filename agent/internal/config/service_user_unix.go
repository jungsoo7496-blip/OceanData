//go:build !windows

package config

// CheckServiceUser의 Windows 계정 검사는 Unix 0600 파일 저장 경로에는 적용하지 않는다.
func CheckServiceUser(string) error {
	return nil
}
