package mediameta

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func box(typ string, payload []byte) []byte {
	b := make([]byte, 8+len(payload))
	binary.BigEndian.PutUint32(b, uint32(len(b)))
	copy(b[4:8], typ)
	copy(b[8:], payload)
	return b
}

func mvhd(t time.Time) []byte {
	p := make([]byte, 100)
	if !t.IsZero() {
		binary.BigEndian.PutUint32(p[4:8], uint32(t.Sub(mp4Epoch)/time.Second))
	}
	return box("mvhd", p)
}

func write(t *testing.T, data []byte) string {
	p := filepath.Join(t.TempDir(), "x.mp4")
	if err := os.WriteFile(p, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestMP4CreationTime(t *testing.T) {
	want := time.Date(2026, 11, 1, 1, 2, 3, 0, time.UTC)
	data := append(box("ftyp", []byte("isom\x00\x00\x02\x00")), box("moov", mvhd(want))...)
	got, ok := MP4CreationTime(write(t, data))
	if !ok || !got.Equal(want) {
		t.Fatalf("got %v %v", got, ok)
	}
	// creation_time=0 (ffmpeg -fflags +bitexact 등)은 근거가 아니다
	zero := append(box("ftyp", []byte("isom")), box("moov", mvhd(time.Time{}))...)
	if _, ok := MP4CreationTime(write(t, zero)); ok {
		t.Fatal("0은 근거로 쓰면 안 된다")
	}
	if _, ok := MP4CreationTime(write(t, []byte("not an mp4 file at all"))); ok {
		t.Fatal("mp4가 아니면 ok=false")
	}
	// version 1 creation_time 21946744073초(2599년경)가 time.Duration 곱셈에서 넘쳐 2014년으로 통과하던 문제
	if got, ok := MP4CreationTime(write(t, append(box("ftyp", []byte("isom")), box("moov", mvhdV1(21946744073))...))); ok {
		t.Fatalf("넘친 시각 %v를 근거로 썼다", got)
	}
}

func TestMP4CreationTimeRejectsShortExtendedMvhd(t *testing.T) {
	const creationSeconds = 0xd0000000
	payload := mvhdV1(creationSeconds)[8:]
	want := mp4Epoch.Add(creationSeconds * time.Second)
	ftyp := box("ftyp", []byte("isom"))

	valid := append(ftyp, box("moov", largeBox("mvhd", payload))...)
	if got, ok := MP4CreationTime(write(t, valid)); !ok || !got.Equal(want) {
		t.Fatalf("완전한 확장 mvhd: got %v %v, want %v", got, ok, want)
	}

	// 64비트 시각의 마지막 바이트가 빠진 mvhd 뒤에 free를 붙인다. free 크기의 첫 0 바이트를
	// 상자 경계 너머에서 읽으면 그럴듯한 시각이 완성된다.
	short := largeBox("mvhd", payload[:11])
	malformed := append(ftyp, box("moov", append(short, box("free", nil)...))...)
	if got, ok := MP4CreationTime(write(t, malformed)); ok {
		t.Fatalf("짧은 확장 mvhd 뒤 상자의 바이트를 촬영시각 %v로 썼다", got)
	}
}
