package timex

import (
	"math"
	"testing"
	"time"
)

func TestSecondsClampsInsteadOfOverflowing(t *testing.T) {
	limit := time.Duration(MaxSeconds) * time.Second
	for _, c := range []struct {
		in   int64
		want time.Duration
	}{
		{0, 0},
		{-1, 0},
		{math.MinInt64, 0},
		{30, 30 * time.Second},
		{MaxSeconds, limit},
		{MaxSeconds + 1, limit},
		// 퍼즈로 찾은 넘침 값: time.Duration(n)*time.Second가 int64를 넘어 음수(-292년)가 된다
		{math.MaxInt64/int64(time.Second) + 1, limit},
		// 2^64 나노초를 조금 넘는 초는 넘쳐서 0.29초쯤의 작은 양수가 된다 (쓰기 완료 판정 대기를 건너뛰게 한다)
		{18446744074, limit},
		{math.MaxInt64, limit},
	} {
		if got := Seconds(c.in); got != c.want {
			t.Errorf("Seconds(%d) = %v, want %v", c.in, got, c.want)
		}
	}
	// 상한 대기를 지금 시각에 더해도 UnixNano가 넘치지 않는다 (재시도 시각은 SQLite에 UnixNano로 저장한다)
	now := time.Now()
	if next := now.Add(limit); next.UnixNano() <= now.UnixNano() {
		t.Fatalf("지금+상한 %v의 UnixNano가 넘쳤다", next)
	}
}
