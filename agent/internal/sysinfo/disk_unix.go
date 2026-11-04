//go:build !windows

// Package sysinfo는 디스크 여유 공간 등 OS 정보를 읽는다.
package sysinfo

import (
	"math"
	"syscall"
)

func DiskFree(path string) (int64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, err
	}
	if st.Bsize <= 0 {
		return 0, nil
	}
	free := uint64(st.Bavail) * uint64(st.Bsize) //nolint:unconvert // #nosec G115 -- Bsize > 0 확인함
	if free > math.MaxInt64 {
		return math.MaxInt64, nil
	}
	return int64(free), nil // #nosec G115 -- 위에서 범위를 확인했다
}
