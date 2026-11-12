//go:build windows

package config

import (
	"errors"
	"strings"

	"golang.org/x/sys/windows"
)

// CheckServiceUser는 등록·서비스 설치를 수행하는 현재 계정과 서비스 계정을 SID로 비교한다.
// 빈 계정은 Windows 서비스 기본값인 LocalSystem이다. 기기/사용자 이름 표기 차이로 비교하지 않는다.
func CheckServiceUser(username string) error {
	current, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return errors.New("현재 Windows 실행 계정을 확인하지 못했습니다")
	}
	var target *windows.SID
	username = strings.TrimSpace(username)
	if username == "" || strings.EqualFold(username, "LocalSystem") {
		target, err = windows.CreateWellKnownSid(windows.WinLocalSystemSid)
	} else {
		target, _, _, err = windows.LookupSID("", username)
	}
	if err != nil || target == nil {
		return errors.New("서비스 실행 계정을 확인하지 못했습니다. Windows 계정 설정을 확인하세요")
	}
	if !current.User.Sid.Equals(target) {
		return errors.New("서비스 실행 계정이 현재 Windows 등록 계정과 다릅니다. 같은 계정으로 등록하고 실행해야 합니다")
	}
	return nil
}
