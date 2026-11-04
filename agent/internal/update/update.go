// Package update는 서명 검증을 통과한 바이너리만 적용하는 수동 업데이트와 되돌리기다 (06번 요구사항 12).
// 자동 업데이트(go-selfupdate)는 OSS-CANDIDATES에서 PoC 대상이라 아직 쓰지 않는다.
package update

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Verify는 바이너리 파일의 Ed25519 서명(base64, 바이너리 전체에 대한 서명)을 확인한다.
func Verify(binPath, sigPath, pubKeyB64 string) error {
	if pubKeyB64 == "" {
		return errors.New("업데이트 공개키(update_public_key)가 설정되지 않았습니다")
	}
	pub, err := base64.StdEncoding.DecodeString(pubKeyB64)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return errors.New("업데이트 공개키 형식 오류")
	}
	bin, err := os.ReadFile(binPath)
	if err != nil {
		return err
	}
	sigRaw, err := os.ReadFile(sigPath)
	if err != nil {
		return err
	}
	sig, err := base64.StdEncoding.DecodeString(string(trim(sigRaw)))
	if err != nil {
		return errors.New("서명 파일 형식 오류 (base64)")
	}
	if !ed25519.Verify(pub, bin, sig) {
		return errors.New("서명 검증 실패: 적용하지 않습니다")
	}
	return nil
}

func trim(b []byte) []byte {
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r' || b[len(b)-1] == ' ') {
		b = b[:len(b)-1]
	}
	return b
}

// Apply는 현재 실행 파일을 .bak으로 남기고 새 바이너리로 바꾼다.
func Apply(current, newBin string) error {
	bak := current + ".bak"
	_ = os.Remove(bak)
	if err := os.Rename(current, bak); err != nil {
		return fmt.Errorf("현재 실행 파일을 백업할 수 없습니다: %w", err)
	}
	if err := copyFile(newBin, current); err != nil {
		_ = os.Rename(bak, current) // 실패하면 즉시 되돌린다
		return err
	}
	return nil
}

// Rollback은 .bak으로 되돌린다 (요구사항 12.2).
func Rollback(current string) error {
	bak := current + ".bak"
	if _, err := os.Stat(bak); err != nil {
		return errors.New("되돌릴 이전 버전이 없습니다")
	}
	failed := current + ".failed"
	_ = os.Remove(failed)
	if err := os.Rename(current, failed); err != nil {
		return err
	}
	return os.Rename(bak, current)
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	if err := os.MkdirAll(filepath.Dir(dst), 0o750); err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755) // #nosec G302 -- 실행 파일이라 실행 권한이 필요하다
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		return err
	}
	return out.Close()
}
