// Package update는 서명 검증·원자 교체·새 프로세스 기동 확인과 자동 복원을 담당한다 (06번 요구사항 12).
package update

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	selfupdate "github.com/creativeprojects/go-selfupdate/update"
)

const DefaultMaxBytes int64 = 64 << 20
const maxSignatureBytes int64 = 1024

// Verify는 바이너리 전체에 대한 기존 Ed25519(base64) 서명을 확인한다.
func Verify(binPath, sigPath, pubKeyB64 string) error {
	data, err := readFile(binPath, DefaultMaxBytes)
	if err != nil {
		return err
	}
	sig, err := readFile(sigPath, maxSignatureBytes)
	if err != nil {
		return err
	}
	return verify(data, sig, pubKeyB64)
}

func verify(data, sigRaw []byte, key string) error {
	pub, err := base64.StdEncoding.DecodeString(key)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return errors.New("업데이트 공개키(update_public_key) 형식 오류")
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigRaw)))
	if err != nil || len(sig) != ed25519.SignatureSize {
		return errors.New("업데이트 서명 파일 형식 오류 (base64 Ed25519)")
	}
	if !ed25519.Verify(pub, data, sig) {
		return errors.New("업데이트 서명 검증 실패: 적용하지 않습니다")
	}
	return nil
}

func readFile(path string, limit int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return readLimited(f, limit)
}

func readLimited(r io.Reader, limit int64) ([]byte, error) {
	if limit <= 0 {
		return nil, errors.New("업데이트 크기 한도는 양수여야 합니다")
	}
	b, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, errors.New("업데이트 파일 크기가 한도를 넘었습니다")
	}
	if len(b) == 0 {
		return nil, errors.New("업데이트 파일이 비어 있습니다")
	}
	return b, nil
}

func digest(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}

// Apply는 기존 수동 호출 호환용 원자 교체다. 제품 경로는 검증한 Candidate를 Install로 적용한다.
func Apply(current, newBin string) error {
	data, err := readFile(newBin, DefaultMaxBytes)
	if err != nil {
		return err
	}
	return applyBytes(current, current+".bak", data)
}

func applyBytes(current, backup string, data []byte) error {
	checksum := sha256.Sum256(data)
	if err := selfupdate.Apply(bytes.NewReader(data), selfupdate.Options{
		TargetPath: current, OldSavePath: backup, Checksum: checksum[:], TargetMode: 0o755,
	}); err != nil {
		if rollbackErr := selfupdate.RollbackError(err); rollbackErr != nil {
			return fmt.Errorf("업데이트 교체와 복원이 모두 실패했습니다: %w", errors.Join(err, rollbackErr))
		}
		return fmt.Errorf("업데이트 파일을 원자 교체하지 못했습니다: %w", err)
	}
	return nil
}

func previousPointer(current string) string { return current + ".rollback" }

func install(current string, candidate *Candidate) error {
	previous, err := readFile(current, candidate.maxBytes)
	if err != nil {
		return err
	}
	// 실행 중인 Windows 감독 프로세스의 첫 백업을 다음 업데이트가 덮어쓰지 않게 고유 이름을 쓴다.
	f, err := os.CreateTemp(filepath.Dir(current), "."+filepath.Base(current)+".previous-*")
	if err != nil {
		return err
	}
	backup := f.Name()
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Remove(backup); err != nil {
		return err
	}
	if err = applyBytes(current, backup, candidate.data); err != nil {
		return err
	}
	pointer := []byte(filepath.Base(backup) + "\n" + digest(previous) + "\n")
	if err = writeAtomic(previousPointer(current), pointer, 0o600); err != nil {
		rollbackErr := restore(current, backup, digest(previous))
		return fmt.Errorf("복원 정보 저장 실패: %w", errors.Join(err, rollbackErr))
	}
	return nil
}

// Rollback은 기록한 이전 실행 파일의 해시를 확인한 뒤 원자적으로 되돌린다.
func Rollback(current string) error {
	backup, expected := current+".bak", ""
	if b, err := os.ReadFile(previousPointer(current)); err == nil {
		fields := strings.Split(strings.TrimSuffix(string(b), "\n"), "\n")
		if len(fields) != 2 || filepath.Base(fields[0]) != fields[0] ||
			!strings.HasPrefix(fields[0], "."+filepath.Base(current)+".previous-") || len(fields[1]) != 64 {
			return errors.New("업데이트 복원 정보가 올바르지 않습니다")
		}
		backup, expected = filepath.Join(filepath.Dir(current), fields[0]), fields[1]
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := restore(current, backup, expected); err != nil {
		return err
	}
	_ = os.Remove(previousPointer(current))
	return nil
}

func restore(current, backup, expected string) error {
	data, err := os.ReadFile(backup)
	if err != nil {
		return errors.New("되돌릴 이전 버전이 없습니다")
	}
	if expected != "" && digest(data) != expected {
		return errors.New("이전 버전의 해시가 달라 복원을 중단했습니다")
	}
	f, err := os.CreateTemp(filepath.Dir(current), "."+filepath.Base(current)+".failed-*")
	if err != nil {
		return err
	}
	failed := f.Name()
	if err = f.Close(); err != nil {
		_ = os.Remove(failed)
		return err
	}
	if err = os.Remove(failed); err != nil {
		return err
	}
	// 해시를 확인한 바로 그 바이트를 설치한다. 백업 경로를 다시 Rename하면 심볼릭 링크나
	// 경로 교체로 검증 후 다른 내용이 실행 경로에 들어갈 수 있다.
	return applyBytes(current, failed, data)
}

func writeAtomic(path string, data []byte, mode os.FileMode) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".update-state-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if err = f.Chmod(mode); err == nil {
		_, err = f.Write(data)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(tmp, path)
}
