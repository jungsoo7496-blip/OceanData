// Package mediameta는 파일 내부 메타데이터에서 촬영시각 근거를 찾는다.
// 파일 생성·수정 시각은 절대 쓰지 않는다 (domain.md 6장, 06번 요구사항 4.2).
package mediameta

import (
	"encoding/binary"
	"io"
	"os"
	"time"
)

var mp4Epoch = time.Date(1904, 1, 1, 0, 0, 0, 0, time.UTC)

// MP4CreationTime은 MP4/MOV의 moov/mvhd creation_time을 읽는다. 값이 0이거나 없으면 ok=false.
// 장비·인코더가 기록한 값이라 timestamp_quality는 DEVICE, verified=false로 보낸다.
func MP4CreationTime(p string) (time.Time, bool) {
	f, err := os.Open(p)
	if err != nil {
		return time.Time{}, false
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return time.Time{}, false
	}
	return findMvhd(f, 0, st.Size(), 0)
}

func findMvhd(r io.ReaderAt, start, end int64, depth int) (time.Time, bool) {
	if depth > 4 {
		return time.Time{}, false
	}
	hdr := make([]byte, 16)
	for off := start; off+8 <= end; {
		if _, err := r.ReadAt(hdr[:8], off); err != nil {
			return time.Time{}, false
		}
		size := int64(binary.BigEndian.Uint32(hdr[:4])) // 32비트 값이라 넘치지 않는다
		typ := string(hdr[4:8])
		hlen := int64(8)
		if size == 1 {
			if _, err := r.ReadAt(hdr[8:16], off+8); err != nil {
				return time.Time{}, false
			}
			raw := binary.BigEndian.Uint64(hdr[8:16])
			if end < 0 || raw > uint64(end) { // #nosec G115 -- end ≥ 0 확인. 파일보다 큰 상자는 손상으로 본다
				return time.Time{}, false
			}
			size = int64(raw) // #nosec G115 -- 위에서 end 이하로 확인했다
			hlen = 16
		} else if size == 0 {
			size = end - off
		}
		if size < hlen || off+size > end {
			return time.Time{}, false
		}
		switch typ {
		case "moov":
			return findMvhd(r, off+hlen, off+size, depth+1)
		case "mvhd":
			b := make([]byte, 12)
			if _, err := r.ReadAt(b, off+hlen); err != nil {
				return time.Time{}, false
			}
			var secs uint64
			if b[0] == 1 { // version 1: 64비트
				b8 := make([]byte, 8)
				if _, err := r.ReadAt(b8, off+hlen+4); err != nil {
					return time.Time{}, false
				}
				secs = binary.BigEndian.Uint64(b8)
			} else {
				secs = uint64(binary.BigEndian.Uint32(b[4:8]))
			}
			if secs == 0 || secs > 1<<40 {
				return time.Time{}, false
			}
			t := mp4Epoch.Add(time.Duration(secs) * time.Second) // #nosec G115 -- 2^40초 이하로 제한했다
			if t.Year() < 2000 || t.After(time.Now().Add(48*time.Hour)) {
				return time.Time{}, false // 기본값·잘못된 시계로 보이는 값은 근거로 쓰지 않는다
			}
			return t, true
		}
		off += size
	}
	return time.Time{}, false
}
