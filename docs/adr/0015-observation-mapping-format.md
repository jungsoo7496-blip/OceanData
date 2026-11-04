# ADR-0015: 관측값 매핑 프로필은 자체 JSON 형식 (Frictionless PoC 보류)

- 상태: 채택 (앵커 센서 기업 연계 카드를 받은 뒤 다시 검토)
- 날짜: 2026-11-04
- 관련: 07번 요구사항 4·5·6, docs/OSS-CANDIDATES.md (Frictionless: PoC)

## 맥락
OSS-CANDIDATES는 07번 CSV 매핑 프로필을 Frictionless Table Schema 형식으로 쓰는 PoC를 1순위 후보로 두었다. 그런데 실제 센서 기업의 파일 형식·MQTT 페이로드는 아직 모른다("확인 필요"). 지금 Frictionless(의존성 다수)를 넣으면 쓰지 않을 기능과 라이선스 검토 부담만 늘어난다.

## 결정
`contracts/openapi.yaml`의 `ObservationMapping`으로 최소 형식을 정한다: format(csv·json_lines·mqtt_json), 구분자, 헤더 행, 시각 필드·strptime 형식, 결측 표기 목록, 토픽 필터, 열별 (필드 → metric·단위·수심·채널·품질 필드).
- 해석 규칙은 REST 배치와 같다 (`services/observations.ingest_items`): 오프셋 없는 시각은 장비 source_timezone, 결측은 null+MISSING, 미등록 단위 SUSPECT.
- 버전마다 저장하고(`observation_mappings`), 수정 전후를 감사 로그에 남긴다. 장비 정책의 `observation_mapping_version`은 `m{버전}`.

## 다시 볼 조건
연계 카드에서 센서 기업이 Table Schema·CSVW 같은 표준 기술 파일을 이미 쓰고 있으면 Frictionless PoC를 진행해 이 형식과 변환기를 둔다.
