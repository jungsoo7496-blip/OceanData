// Package plugin은 에이전트 확장 인터페이스다 (06번 요구사항 13). 실제 플러그인은 07번(MQTT)·15번(엣지 캡처).
package plugin

import (
	"context"
	"time"
)

// Capture_Plugin: 영상 등을 캡처해 감시 폴더에 파일을 만든다. 에이전트는 그 폴더를 감시해 같은 API로 보낸다.
// ROTATING 원천 장비에서 플러그인이 만든 파일만 영수증·현장 보존 기간 조건을 만족할 때 지울 수 있다 (domain.md 9장).
type CapturePlugin interface {
	Name() string
	Version() string
	// Run은 ctx가 끝날 때까지 캡처 파일을 OutputDir 아래에 만든다.
	Run(ctx context.Context, outputDir string) error
	// OwnsFile은 이 플러그인이 만든 파일인지 알려준다 (사용자가 넣은 파일은 절대 지우지 않는다).
	OwnsFile(relPath string) bool
}

// Observation은 Sensor_Plugin이 만든 관측값이다 (contracts/schemas/observation-batch.schema.json 항목).
type Observation struct {
	Metric          string    `json:"metric"`
	ObservedAt      time.Time `json:"observed_at"`
	Value           *float64  `json:"value"`
	Unit            string    `json:"unit,omitempty"`
	QualityFlag     string    `json:"quality_flag"`
	DepthM          *float64  `json:"depth_m,omitempty"`
	Channel         string    `json:"channel,omitempty"`
	SourceTimestamp string    `json:"source_timestamp,omitempty"`
}

// Sensor_Plugin: 관측값을 만들어 대기열에 넣는다. 에이전트가 POST /v1/observations 배치로 보낸다 (07번).
type SensorPlugin interface {
	Name() string
	Version() string
	SourceDeviceID() string
	Run(ctx context.Context, emit func(Observation)) error
}
