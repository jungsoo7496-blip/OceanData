package sched

import (
	"bytes"
	"context"
	"io"
	"testing"
	"time"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

func TestActiveWindows(t *testing.T) {
	ws := []api.TransferWindow{{Days: []string{"MON", "TUE"}, StartLocal: "22:00", EndLocal: "06:00"}}
	seoul, _ := time.LoadLocation("Asia/Seoul")
	at := func(d, h int) time.Time { return time.Date(2026, 11, d, h, 30, 0, 0, seoul) } // 2026-11-02는 월요일
	cases := map[time.Time]bool{at(2, 23): true, at(3, 5): true, at(3, 7): false, at(4, 5): true, at(5, 5): false,
		at(2, 12): false}
	for tm, want := range cases {
		if _, ok := Active(ws, "Asia/Seoul", tm.UTC()); ok != want {
			t.Errorf("%v: got %v want %v", tm, ok, want)
		}
	}
	if _, ok := Active(nil, "Asia/Seoul", time.Now()); !ok {
		t.Fatal("시간대가 없으면 항상 허용")
	}
}

func TestLimiterRate(t *testing.T) {
	l := NewLimiter()
	kbps := 800 // 100 KB/s
	l.SetKbps(&kbps)
	start := time.Now()
	n, _ := io.Copy(io.Discard, l.Reader(context.Background(), bytes.NewReader(make([]byte, 250_000))))
	el := time.Since(start)
	if n != 250_000 || el < 1200*time.Millisecond || el > 3*time.Second {
		t.Fatalf("250KB @100KB/s: %v", el)
	}
}

func TestBackoff(t *testing.T) {
	if Backoff(0, time.Minute) != time.Second || Backoff(3, time.Minute) != 8*time.Second || Backoff(30, time.Minute) != time.Minute {
		t.Fatal("백오프 오류")
	}
}
