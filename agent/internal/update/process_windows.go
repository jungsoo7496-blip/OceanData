package update

import (
	"os/exec"
	"syscall"

	"golang.org/x/sys/windows"
)

func configureBackgroundProcess(cmd *exec.Cmd) {
	// 서비스나 백그라운드 CLI가 시작한 워커는 별도 콘솔 없이 준비 신호와 stdin으로 통신한다.
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW}
}
