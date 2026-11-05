package core

// MQTT_TEST_BROKER(예: tcp://127.0.0.1:1883)가 있을 때만 돈다. 개발용 Mosquitto 컨테이너:
//   docker run -d -p 1883:1883 eclipse-mosquitto:2.0.20 mosquitto -c /mosquitto-no-auth.conf

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/api"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/config"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/sensor"
	"github.com/jungsoo7496-blip/oceandata/agent/internal/state"
)

func TestMQTTPluginSubscribeQueueAndSendInOrder(t *testing.T) {
	broker := os.Getenv("MQTT_TEST_BROKER")
	if broker == "" {
		t.Skip("MQTT_TEST_BROKER가 없어 건너뜁니다")
	}
	var mu sync.Mutex
	var received []map[string]any
	failFirst := 3 // 처음 3번은 서버 장애(503) → 대기열 유지 후 시간순 전송
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch r.URL.Path {
		case "/v1/auth/token":
			_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "tok", "token_type": "Bearer", "expires_in": 300})
		case "/v1/devices/" + collector + "/policy":
			_ = json.NewEncoder(w).Encode(api.Policy{PolicyVersion: "pv-1", CollectorDeviceID: collector,
				HeartbeatIntervalS: 3600, SizeStableS: 1, MaxParallelParts: 1, Timezone: "UTC"})
		case "/v1/devices/" + sensorDev + "/observation-mapping":
			_ = json.NewEncoder(w).Encode(map[string]any{"device_id": sensorDev, "version": 1, // ObservationMappingRecord
				"created_at": "2026-11-01T00:00:00Z", "mapping": map[string]any{
					"format": "mqtt_json", "topic_filter": "test/+/ctd", "timestamp": map[string]any{"field": "ts"},
					"columns": []any{map[string]any{"field": "temp", "metric": "sea_water_temperature", "unit": "degC"}}}})
		case "/v1/observations":
			if failFirst > 0 {
				failFirst--
				w.WriteHeader(503)
				_, _ = io.WriteString(w, `{"status":503,"code":"SERVICE_UNAVAILABLE"}`)
				return
			}
			var b map[string]any
			_ = json.NewDecoder(r.Body).Decode(&b)
			obs := b["observations"].([]any)
			for _, o := range obs {
				received = append(received, o.(map[string]any))
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"accepted": len(obs), "duplicates": 0, "rejected": 0, "results": []any{}})
		case "/v1/agents/heartbeat":
			w.WriteHeader(204)
		default:
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()

	dir := t.TempDir()
	cfg := &config.Config{ServerURL: srv.URL, WatchRoot: t.TempDir(), DeviceID: collector, MaxBackoffS: 1,
		SensorPlugins: []sensor.Config{{SourceDeviceID: sensorDev, Broker: broker, ClientID: "agent-test", QoS: 1}}}
	cfg.Defaults()
	cfg.MaxBackoffS = 1
	st, err := state.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = st.Close() }()
	hc, _ := api.NewHTTPClient("", 10*time.Second)
	a := New(cfg, dir, api.New(srv.URL, hc, &api.Credentials{ClientID: "dcr_x", ClientSecret: "s"}, "test"), st,
		slog.New(slog.NewTextHandler(io.Discard, nil)), "test")
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	go func() { _ = a.Run(ctx) }()

	// 모의 센서 발행기
	pub := mqtt.NewClient(mqtt.NewClientOptions().AddBroker(broker).SetClientID("sim-sensor"))
	if tok := pub.Connect(); tok.Wait() && tok.Error() != nil {
		t.Fatal(tok.Error())
	}
	defer pub.Disconnect(100)
	time.Sleep(2 * time.Second) // 구독 대기
	for i := 0; i < 5; i++ {
		msg := fmt.Sprintf(`{"ts":"2026-11-03T01:%02d:00Z","temp":%d.5}`, i, 18+i)
		pub.Publish("test/buoy1/ctd", 1, false, msg).Wait()
	}
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(received)
		mu.Unlock()
		if n >= 5 {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(received) != 5 {
		t.Fatalf("관측값 %d개만 받았다", len(received))
	}
	for i, o := range received { // 시간순
		if o["observed_at"] != fmt.Sprintf("2026-11-03T01:%02d:00Z", i) || o["metric"] != "sea_water_temperature" {
			t.Fatalf("%d: %v", i, o)
		}
	}
	if n, _ := st.ObservationQueueSize(context.Background()); n != 0 {
		t.Fatalf("보낸 관측값이 대기열에 남았다: %d", n)
	}
}
