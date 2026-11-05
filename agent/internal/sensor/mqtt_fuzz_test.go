package sensor

import (
	"encoding/json"
	"math"
	"testing"
)

// 퍼즈 시험: 페이로드는 현장 MQTT 브로커(장비)에서, 매핑 프로필은 서버 응답에서 온다 (07번 요구사항 6).
// 실행: go test -run='^$' -fuzz='^FuzzToObservations$' -fuzztime=90s -parallel=2 ./internal/sensor
func FuzzToObservations(f *testing.F) {
	mapping := []byte(mappingJSON) // TestToObservations의 매핑
	for _, payload := range []string{
		`{"ts":"2026-11-03T01:00:00Z","temp":18.4,"sal":-999}`,
		`{"ts":"2026-11-03 10:00:00","temp":1}`,
		`not json`,
		`null`,
		`{"ts":{"a":[1,2]},"temp":"18.4","sal":null}`,
		`{"ts":1793667600,"temp":1e308,"sal":-0}`,
	} {
		f.Add(mapping, []byte(payload))
	}
	f.Add([]byte(`{"timestamp":{"field":"t","format":"%Y"},"missing_values":["NaN"],"columns":[{"field":"v","metric":"m",`+
		`"quality_field":"q","channel":"c"}]}`), []byte(`{"t":"2026","v":2.5,"q":"suspect"}`))
	f.Add([]byte(`{}`), []byte(`{}`))
	f.Fuzz(func(t *testing.T, mappingRaw, payload []byte) {
		var mp Mapping
		if json.Unmarshal(mappingRaw, &mp) != nil {
			return // 매핑 프로필은 JSON으로 받은 것만 쓴다
		}
		_, items, err := ToObservations(mp, payload)
		if err != nil {
			if items != nil {
				t.Fatalf("오류와 항목을 함께 돌려줬다: %v", items)
			}
			return
		}
		if len(items) > len(mp.Columns) {
			t.Fatalf("열 %d개에서 항목 %d개", len(mp.Columns), len(items))
		}
		for _, it := range items {
			if _, ok := it["metric"].(string); !ok {
				t.Fatalf("metric 없음: %v", it)
			}
			_, hasObserved := it["observed_at"]
			_, hasSource := it["source_timestamp"]
			if hasObserved == hasSource {
				t.Fatalf("observed_at·source_timestamp 중 하나만 있어야 한다: %v", it)
			}
			if q, ok := it["quality_flag"].(string); !ok || q == "" {
				t.Fatalf("quality_flag 없음: %v", it)
			}
			switch v := it["value"].(type) {
			case nil: // 결측은 0이나 직전 값으로 채우지 않고 null+MISSING
				if it["quality_flag"] != "MISSING" {
					t.Fatalf("값이 없는데 MISSING이 아니다: %v", it)
				}
			case float64:
				if math.IsNaN(v) || math.IsInf(v, 0) {
					t.Fatalf("유한하지 않은 값: %v", it)
				}
			default:
				t.Fatalf("숫자가 아닌 값 %T: %v", v, it)
			}
		}
		// 항목은 JSON으로 대기열에 넣어 POST /v1/observations로 보낸다
		if _, err := json.Marshal(items); err != nil {
			t.Fatalf("항목을 JSON으로 바꾸지 못했다: %v", err)
		}
	})
}
