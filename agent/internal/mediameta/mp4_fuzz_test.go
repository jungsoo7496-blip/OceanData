package mediameta

import (
	"bytes"
	"encoding/binary"
	"math"
	"testing"
	"time"
)

// 퍼즈 시험: 카메라·인코더가 만든 MP4는 손상되거나 일부러 조작될 수 있다 (06번 요구사항 4.2).
// 실행: go test -run='^$' -fuzz='^FuzzFindMvhd$' -fuzztime=90s -parallel=2 ./internal/mediameta

// mvhdV1은 version 1(64비트 creation_time) mvhd 상자다.
func mvhdV1(secs uint64) []byte {
	p := make([]byte, 112)
	p[0] = 1
	binary.BigEndian.PutUint64(p[4:12], secs)
	return box("mvhd", p)
}

// largeBox는 size=1과 64비트 largesize 머리를 쓰는 상자다.
func largeBox(typ string, payload []byte) []byte {
	b := make([]byte, 16+len(payload))
	binary.BigEndian.PutUint32(b, 1)
	copy(b[4:8], typ)
	binary.BigEndian.PutUint64(b[8:16], uint64(len(b)))
	copy(b[16:], payload)
	return b
}

// toEOFBox는 size=0(파일 끝까지)인 상자다.
func toEOFBox(typ string, payload []byte) []byte {
	b := make([]byte, 8+len(payload))
	copy(b[4:8], typ)
	copy(b[8:], payload)
	return b
}

func cat(parts ...[]byte) []byte {
	return bytes.Join(parts, nil)
}

// writtenBE는 v가 data 어딘가에 빅엔디언 32비트 또는 64비트 정수로 적혀 있는지 본다.
func writtenBE(data []byte, v uint64) bool {
	for i := 0; i+4 <= len(data); i++ {
		if v <= math.MaxUint32 && uint64(binary.BigEndian.Uint32(data[i:])) == v {
			return true
		}
		if i+8 <= len(data) && binary.BigEndian.Uint64(data[i:]) == v {
			return true
		}
	}
	return false
}

func FuzzFindMvhd(f *testing.F) {
	want := time.Date(2026, 11, 1, 1, 2, 3, 0, time.UTC)
	secs := uint64(want.Sub(mp4Epoch) / time.Second)
	ftyp := box("ftyp", []byte("isom\x00\x00\x02\x00"))
	valid := cat(ftyp, box("moov", mvhd(want)))
	for _, seed := range [][]byte{
		valid, // TestMP4CreationTime의 정상 파일 (version 0)
		cat(ftyp, box("moov", mvhd(time.Time{}))), // creation_time=0
		[]byte("not an mp4 file at all"),
		cat(ftyp, box("moov", mvhdV1(secs))),                                        // version 1
		cat(ftyp, largeBox("moov", mvhd(want))),                                     // size=1 + 64비트 largesize
		cat(ftyp, toEOFBox("moov", mvhd(want))),                                     // size=0: 파일 끝까지
		cat(ftyp, box("moov", cat(box("trak", nil), mvhd(want)))),                   // mvhd 앞에 다른 상자
		box("moov", box("moov", box("moov", box("moov", box("moov", mvhd(want)))))), // 깊이 한도 밖
		valid[:len(valid)-60],                                                       // 잘린 파일 (moov·mvhd 중간에서 끝남)
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		got, ok := findMvhd(bytes.NewReader(data), 0, int64(len(data)), 0)
		if !ok {
			if !got.IsZero() {
				t.Fatalf("ok=false인데 시각 %v를 돌려줬다", got)
			}
			return
		}
		// 촬영시각 근거로 쓸 수 있는 범위(2000년 이후, 지금+48시간 이전, 초 단위)만 돌려준다
		if got.Year() < 2000 || got.After(time.Now().Add(48*time.Hour)) || got.Nanosecond() != 0 {
			t.Fatalf("범위 밖 시각 %v", got)
		}
		// 돌려준 시각은 파일에 적힌 creation_time(1904-01-01부터 초, 32비트 또는 64비트)이어야 한다
		if s := got.Unix() - mp4Epoch.Unix(); !writtenBE(data, uint64(s)) {
			t.Fatalf("시각 %v(1904년부터 %d초)는 입력에 적힌 값이 아니다", got, s)
		}
	})
}
