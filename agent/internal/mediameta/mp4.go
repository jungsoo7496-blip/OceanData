// Package mediameta는 파일 내부 메타데이터에서 촬영시각 근거를 찾는다.
// 파일 생성·수정 시각은 절대 쓰지 않는다 (domain.md 6장, 06번 요구사항 4.2).
package mediameta

import (
	"encoding/binary"
	"io"
	"math"
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
	defer func() { _ = f.Close() }() // 읽기 전용
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
		switch size {
		case 1:
			if _, err := r.ReadAt(hdr[8:16], off+8); err != nil {
				return time.Time{}, false
			}
			raw := binary.BigEndian.Uint64(hdr[8:16])
			if end < 0 || raw > uint64(end) { // #nosec G115 -- end ≥ 0 확인. 파일보다 큰 상자는 손상으로 본다
				return time.Time{}, false
			}
			size = int64(raw) // #nosec G115 -- 위에서 end 이하로 확인했다
			hlen = 16
		case 0:
			size = end - off
		}
		if size < hlen || off+size > end {
			return time.Time{}, false
		}
		switch typ {
		case "moov":
			return findMvhd(r, off+hlen, off+size, depth+1)
		case "mvhd":
			// ISO/IEC 14496-12 MovieHeaderBox는 FullBox다: 상자 머리 다음에 version(1바이트)·flags(3바이트), 그다음
			// creation_time이 version 0은 32비트, version 1은 64비트로 온다. 읽는 것은 creation_time까지라 상자 크기가
			// 머리+4+4(version 0, 머리 8바이트면 16) 또는 머리+4+8(version 1, 20)보다 작으면, 곧 짧은 mvhd이면 뒤 상자의
			// 바이트를 촬영시각으로 읽게 되므로 근거로 쓰지 않는다. 정의되지 않은 version도 배치를 알 수 없어 쓰지 않는다
			var b [12]byte
			n := min(size-hlen, int64(len(b))) // 상자 안의 바이트만 읽는다
			if n < 1 {
				return time.Time{}, false
			}
			if _, err := r.ReadAt(b[:n], off+hlen); err != nil {
				return time.Time{}, false
			}
			var secs uint64
			switch {
			case b[0] == 0 && n >= 4+4:
				secs = uint64(binary.BigEndian.Uint32(b[4:8]))
			case b[0] == 1 && n >= 4+8:
				secs = binary.BigEndian.Uint64(b[4:12])
			default:
				return time.Time{}, false
			}
			// time.Duration은 약 292년(2196년)까지만 나타낸다. 그보다 큰 값은 곱셈이 넘쳐 그럴듯한 과거 시각이
			// 되므로(퍼즈 시험으로 찾음) 계산 전에 거부한다. 넘치지 않게 계산해도 아래 미래 시각 검사에 걸리는 값이다
			if secs == 0 || secs > uint64(math.MaxInt64/time.Second) {
				return time.Time{}, false
			}
			t := mp4Epoch.Add(time.Duration(secs) * time.Second) // #nosec G115 -- 위에서 넘치지 않는 범위로 제한했다
			if t.Year() < 2000 || t.After(time.Now().Add(48*time.Hour)) {
				return time.Time{}, false // 기본값·잘못된 시계로 보이는 값은 근거로 쓰지 않는다
			}
			return t, true
		}
		off += size
	}
	return time.Time{}, false
}
