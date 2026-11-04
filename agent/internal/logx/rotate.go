// Package logx는 크기 기준으로 순환하는 로그 파일 Writer다 (06번 요구사항 11.4).
package logx

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

type Rotator struct {
	path     string
	maxBytes int64
	keep     int
	mu       sync.Mutex
	f        *os.File
	size     int64
}

func NewRotator(path string, maxBytes int64, keep int) (*Rotator, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	r := &Rotator{path: path, maxBytes: maxBytes, keep: keep}
	return r, r.open()
}

func (r *Rotator) open() error {
	f, err := os.OpenFile(r.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		return err
	}
	r.f, r.size = f, st.Size()
	return nil
}

func (r *Rotator) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.size+int64(len(p)) > r.maxBytes {
		if err := r.rotate(); err != nil {
			return 0, err
		}
	}
	n, err := r.f.Write(p)
	r.size += int64(n)
	return n, err
}

func (r *Rotator) rotate() error {
	_ = r.f.Close()
	_ = os.Remove(fmt.Sprintf("%s.%d", r.path, r.keep)) // 가장 오래된 것부터 지우고 한 칸씩 민다
	for i := r.keep - 1; i >= 1; i-- {
		_ = os.Rename(fmt.Sprintf("%s.%d", r.path, i), fmt.Sprintf("%s.%d", r.path, i+1))
	}
	_ = os.Rename(r.path, r.path+".1")
	return r.open()
}

func (r *Rotator) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.f.Close()
}
