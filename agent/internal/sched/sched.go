// Package sched는 전송 허용 시간대·대역폭 한도(06번 요구사항 7.2, 7.3)와 재시도 백오프를 다룬다.
package sched

import (
	"context"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
)

var dayCode = [...]string{"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"}

func minutes(hhmm string) int {
	parts := strings.SplitN(hhmm, ":", 2)
	h, _ := strconv.Atoi(parts[0])
	m := 0
	if len(parts) == 2 {
		m, _ = strconv.Atoi(parts[1])
	}
	return h*60 + m
}

func hasDay(days []string, d string) bool {
	for _, x := range days {
		if x == d {
			return true
		}
	}
	return false
}

// Active는 지금 열려 있는 전송 시간대를 돌려준다. 시간대가 정의되지 않았으면 항상 허용(nil, true).
// end가 start보다 이르면 다음 날까지 이어진다 (스키마 설명).
func Active(windows []api.TransferWindow, tz string, now time.Time) (*api.TransferWindow, bool) {
	if len(windows) == 0 {
		return nil, true
	}
	loc, err := time.LoadLocation(tz)
	if err != nil || tz == "" {
		loc = time.UTC
	}
	t := now.In(loc)
	cur := t.Hour()*60 + t.Minute()
	today := dayCode[t.Weekday()]
	yesterday := dayCode[(t.Weekday()+6)%7]
	for i := range windows {
		w := &windows[i]
		s, e := minutes(w.StartLocal), minutes(w.EndLocal)
		if s <= e {
			if hasDay(w.Days, today) && cur >= s && cur < e {
				return w, true
			}
		} else { // 자정을 넘기는 시간대
			if (hasDay(w.Days, today) && cur >= s) || (hasDay(w.Days, yesterday) && cur < e) {
				return w, true
			}
		}
	}
	return nil, false
}

// Limiter는 모든 파트 전송이 함께 쓰는 토큰 버킷이다. rate<=0이면 제한 없음.
type Limiter struct {
	mu     sync.Mutex
	rate   float64 // bytes/s
	tokens float64
	last   time.Time
}

func NewLimiter() *Limiter { return &Limiter{last: time.Now()} }

// SetKbps는 한도를 바꾼다 (킬로비트/초). nil이면 제한 없음.
func (l *Limiter) SetKbps(kbps *int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if kbps == nil || *kbps <= 0 {
		l.rate = 0
		return
	}
	l.rate = float64(*kbps) * 1000 / 8
	if l.tokens > l.rate {
		l.tokens = l.rate
	}
}

func (l *Limiter) wait(ctx context.Context, n int) error {
	for {
		l.mu.Lock()
		if l.rate <= 0 {
			l.mu.Unlock()
			return nil
		}
		now := time.Now()
		l.tokens += now.Sub(l.last).Seconds() * l.rate
		if l.tokens > l.rate {
			l.tokens = l.rate // 버스트는 1초 분량까지
		}
		l.last = now
		if l.tokens >= float64(n) {
			l.tokens -= float64(n)
			l.mu.Unlock()
			return nil
		}
		need := (float64(n) - l.tokens) / l.rate
		l.mu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(need * float64(time.Second))):
		}
	}
}

// Reader는 읽을 때마다 한도를 지키는 io.Reader로 감싼다.
func (l *Limiter) Reader(ctx context.Context, r io.Reader) io.Reader {
	return &limited{ctx: ctx, r: r, l: l}
}

type limited struct {
	ctx context.Context
	r   io.Reader
	l   *Limiter
}

func (x *limited) Read(p []byte) (int, error) {
	if len(p) > 32*1024 {
		p = p[:32*1024]
	}
	n, err := x.r.Read(p)
	if n > 0 {
		if werr := x.l.wait(x.ctx, n); werr != nil {
			return n, werr
		}
	}
	return n, err
}

// Backoff는 지수 백오프(2^n초, 최대 max)다.
func Backoff(attempt int, max time.Duration) time.Duration {
	if attempt > 20 {
		attempt = 20
	}
	d := time.Duration(1<<attempt) * time.Second
	if d > max {
		return max
	}
	return d
}
