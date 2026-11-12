// Package sensor는 Sensor_Plugin 구현이다 (07번 요구사항 6). 지금은 MQTT 구독 플러그인 하나.
//
// 현장 MQTT 브로커에 바깥으로만 연결한다 (센터는 MQTT 포트를 열지 않는다). 메시지를 서버의 매핑 프로필
// (format=mqtt_json)로 관측값 항목으로 바꿔 에이전트 대기열에 넣고, 전송은 POST /v1/observations로만 한다.
// 실제 토픽·페이로드 형식은 연계 카드를 받은 뒤 정한다 (확인 필요).
package sensor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/plugin"
)

// Mapping은 contracts의 ObservationMapping 중 MQTT에 쓰는 부분이다.
type Mapping struct {
	Format      string `json:"format"`
	TopicFilter string `json:"topic_filter"`
	Timestamp   *struct {
		Field  string `json:"field"`
		Format string `json:"format"`
	} `json:"timestamp"`
	MissingValues []string `json:"missing_values"`
	Columns       []struct {
		Field        string   `json:"field"`
		Metric       string   `json:"metric"`
		Unit         string   `json:"unit"`
		DepthM       *float64 `json:"depth_m"`
		Channel      string   `json:"channel"`
		QualityField string   `json:"quality_field"`
	} `json:"columns"`
}

// Config는 에이전트 config.json의 sensor_plugins 항목이다.
type Config struct {
	SourceDeviceID string `json:"source_device_id"`
	Broker         string `json:"broker"` // tcp://host:1883 또는 ssl://host:8883
	ClientID       string `json:"client_id"`
	Username       string `json:"username,omitempty"`
	// Password는 이전 설정과의 호환용이다. 시작할 때 OS 보안 저장소로 옮기고 설정에서는 지운다 (config.MigrateSensorSecrets).
	Password string `json:"password,omitempty"`
	// PasswordSecret은 보안 저장소의 비밀 이름이다. 값은 ResolvedPassword로 메모리에만 둔다 (설정 파일에 다시 쓰지 않는다).
	PasswordSecret   string `json:"password_secret,omitempty"`
	ResolvedPassword string `json:"-"`
	QoS              byte   `json:"qos"`
}

// password는 연결에 쓸 암호다: 보안 저장소에서 읽은 값, 없으면 (아직 옮기지 않은) 설정 값.
func (c Config) password() string {
	if c.ResolvedPassword != "" {
		return c.ResolvedPassword
	}
	return c.Password
}

type MQTTPlugin struct {
	Cfg     Config
	Mapping Mapping
	Log     *slog.Logger
}

func (m *MQTTPlugin) Name() string           { return "mqtt" }
func (m *MQTTPlugin) Version() string        { return "0.1.0" }
func (m *MQTTPlugin) SourceDeviceID() string { return m.Cfg.SourceDeviceID }

// ToObservations는 JSON 페이로드 하나를 관측값 항목들로 바꾼다 (시험 가능한 순수 함수).
func ToObservations(mp Mapping, payload []byte) ([]plugin.Observation, []map[string]any, error) {
	var rec map[string]any
	if err := json.Unmarshal(payload, &rec); err != nil {
		return nil, nil, fmt.Errorf("JSON이 아닙니다: %w", err)
	}
	if mp.Timestamp == nil || mp.Timestamp.Field == "" {
		return nil, nil, errors.New("매핑에 timestamp.field가 없습니다")
	}
	rawTS, ok := rec[mp.Timestamp.Field]
	if !ok {
		return nil, nil, errors.New("시각 필드가 없습니다")
	}
	ts := fmt.Sprint(rawTS)
	missing := map[string]bool{}
	for _, v := range mp.MissingValues {
		missing[v] = true
	}
	var items []map[string]any
	for _, col := range mp.Columns {
		raw, present := rec[col.Field]
		item := map[string]any{"metric": col.Metric}
		if mp.Timestamp.Format != "" || !hasOffset(ts) {
			item["source_timestamp"] = ts // 서버가 장비 source_timezone으로 해석
		} else {
			item["observed_at"] = ts
		}
		if !present || raw == nil || missing[fmt.Sprint(raw)] {
			item["value"], item["quality_flag"] = nil, "MISSING" // 0이나 직전 값으로 채우지 않는다
		} else {
			f, ok := raw.(float64)
			if !ok {
				continue // 숫자가 아니면 그 항목만 건너뛴다
			}
			item["value"], item["quality_flag"] = f, "GOOD"
			if col.Unit != "" {
				item["unit"] = col.Unit
			}
			if col.QualityField != "" {
				if q, ok := rec[col.QualityField].(string); ok && q != "" {
					item["quality_flag"] = strings.ToUpper(q)
				}
			}
		}
		if col.DepthM != nil {
			item["depth_m"] = *col.DepthM
		}
		if col.Channel != "" {
			item["channel"] = col.Channel
		}
		items = append(items, item)
	}
	return nil, items, nil
}

func hasOffset(ts string) bool {
	if len(ts) < 19 {
		return false
	}
	tail := ts[19:]
	return strings.HasSuffix(ts, "Z") || strings.ContainsAny(tail, "+-")
}

// Run은 브로커에 연결해 구독하고, 받은 관측값 항목을 emit으로 대기열에 넣는다. 연결이 끊기면 자동 재연결한다.
func (m *MQTTPlugin) Run(ctx context.Context, emit func(item map[string]any)) error {
	if m.Mapping.Format != "mqtt_json" || m.Mapping.TopicFilter == "" {
		return errors.New("MQTT 매핑 프로필(format=mqtt_json, topic_filter)이 없습니다")
	}
	opts := mqtt.NewClientOptions().AddBroker(m.Cfg.Broker).SetClientID(m.Cfg.ClientID).
		SetAutoReconnect(true).SetConnectRetry(true).SetConnectRetryInterval(5 * time.Second).
		SetMaxReconnectInterval(time.Minute).SetCleanSession(false).SetOrderMatters(true)
	if m.Cfg.Username != "" {
		opts.SetUsername(m.Cfg.Username).SetPassword(m.Cfg.password())
	}
	handler := func(_ mqtt.Client, msg mqtt.Message) {
		_, items, err := ToObservations(m.Mapping, msg.Payload())
		if err != nil {
			m.Log.Warn("MQTT 메시지를 관측값으로 바꾸지 못했습니다", "topic", msg.Topic(), "error", err.Error())
			return
		}
		for _, it := range items {
			emit(it)
		}
	}
	opts.SetOnConnectHandler(func(c mqtt.Client) {
		if t := c.Subscribe(m.Mapping.TopicFilter, m.Cfg.QoS, handler); t.Wait() && t.Error() != nil {
			m.Log.Error("MQTT 구독 실패", "topic", m.Mapping.TopicFilter, "error", t.Error().Error())
		} else {
			m.Log.Info("MQTT 구독", "topic", m.Mapping.TopicFilter)
		}
	})
	client := mqtt.NewClient(opts)
	if t := client.Connect(); t.WaitTimeout(30*time.Second) && t.Error() != nil {
		m.Log.Warn("MQTT 브로커 연결 실패, 계속 재시도합니다", "error", t.Error().Error())
	}
	<-ctx.Done()
	client.Disconnect(1000)
	return nil
}
