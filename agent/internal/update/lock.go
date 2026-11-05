package update

import (
	"errors"
	"os"
	"path/filepath"
)

// Lock은 OS 파일 잠금이다. 프로세스 강제 종료 때도 OS가 해제하므로 오래된 PID 파일로 갱신이 막히지 않는다.
func Lock(dir string) (func(), error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, ".agent-run.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err = lockFile(f); err != nil {
		_ = f.Close()
		return nil, errors.New("에이전트가 실행 중입니다. 서비스를 멈춘 뒤 수동 업데이트·복원을 실행하세요")
	}
	return func() { unlockFile(f); _ = f.Close() }, nil
}
