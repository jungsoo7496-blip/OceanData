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
}
