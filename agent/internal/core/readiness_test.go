package core

import (
	"context"
	"errors"
	"net/http"
	"sync/atomic"
	"testing"
	"time"
)

func TestRunSignalsLocalReadinessBeforeOfflinePolicy(t *testing.T) {
	e := newEnv(t)
	a := e.agent(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := a.Store.SetKV(ctx, KeyPolicy, ""); err != nil {
		t.Fatal(err)
	}
	a.policy.Store(nil)
	e.fake.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		e.fake.json(w, 503, map[string]any{"status": 503, "code": "SERVICE_UNAVAILABLE"})
	})
	ready := make(chan struct{}, 1)
	var calls atomic.Int32
	a.OnReady = func() { calls.Add(1); ready <- struct{}{} }
	ran := make(chan error, 1)
	go func() { ran <- a.Run(ctx) }()
	select {
	case <-ready:
	case <-time.After(5 * time.Second):
		t.Fatal("서버에 연결할 수 없어도 로컬 기동 확인은 가능해야 한다")
	}
	cancel()
	select {
	case err := <-ran:
		if !errors.Is(err, context.Canceled) || calls.Load() != 1 {
			t.Fatal("로컬 준비 훅·취소 동작이 잘못됐다", calls.Load(), err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("기동 대기 중 취소가 멈추지 않았다")
	}
}
