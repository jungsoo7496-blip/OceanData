package sensor

import (
	"encoding/json"
	"testing"
)

const mappingJSON = `{"format":"mqtt_json","topic_filter":"site/+/ctd","timestamp":{"field":"ts"},
 "missing_values":["-999"],"columns":[{"field":"temp","metric":"sea_water_temperature","unit":"degC","depth_m":1.5},
 {"field":"sal","metric":"sea_water_salinity","unit":"PSU"}]}`

func TestToObservations(t *testing.T) {
	var mp Mapping
	if err := json.Unmarshal([]byte(mappingJSON), &mp); err != nil {
		t.Fatal(err)
	}
	_, items, err := ToObservations(mp, []byte(`{"ts":"2026-11-03T01:00:00Z","temp":18.4,"sal":-999}`))
	if err != nil || len(items) != 2 {
		t.Fatal(err, items)
	}
	if items[0]["observed_at"] != "2026-11-03T01:00:00Z" || items[0]["value"] != 18.4 || items[0]["depth_m"] != 1.5 {
		t.Fatalf("%v", items[0])
	}
	if items[1]["value"] != nil || items[1]["quality_flag"] != "MISSING" {
		t.Fatalf("결측은 null+MISSING: %v", items[1])
	}
	_, local, _ := ToObservations(mp, []byte(`{"ts":"2026-11-03 10:00:00","temp":1}`))
	if local[0]["source_timestamp"] != "2026-11-03 10:00:00" || local[0]["observed_at"] != nil {
		t.Fatalf("오프셋 없는 시각은 source_timestamp로: %v", local[0])
	}
	if _, _, err := ToObservations(mp, []byte(`not json`)); err == nil {
		t.Fatal("JSON 오류를 알려야 한다")
	}
}
