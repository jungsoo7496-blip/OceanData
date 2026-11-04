# 요구사항: 16 NGSI-LD 어댑터·광역 데이터 허브 연계 — 조건부

> **착수 조건 (모두 충족해야 시작):**
> 1. 사업계획·협약·연계 대상 문서에 NGSI-LD 또는 광역 허브 연계가 의무로 적혀 있음
> 2. 대표 사용 사례 2개 이상
> 3. 엔터티·관계·구독 정책 정의
> 4. 유지보수 가능한 브로커와 운영 인력
> 5. 상대 시스템과의 적합성 시험 계획
>
> 조건이 충족되지 않으면 이 스펙은 진행하지 않는다. 그동안은 13번의 DCAT 내보내기로 대비한다.

## 소개
도입 게이트를 통과한 경우에만, 내부 모델을 NGSI-LD 엔터티로 보여주는 어댑터와 광역 허브 연계를 만든다.
영상 저장·데이터셋 카탈로그를 NGSI-LD로 바꾸지 않는다.

## 용어집
- **NGSI_Adapter**: 내부 모델 ↔ NGSI-LD 엔터티 변환 계층
- **Context_Broker**: 표준을 따르는 NGSI-LD 브로커 (선정은 게이트 통과 후)
- **Hub_Connector**: 광역 데이터 허브 연계 구성요소

## 요구사항

### 요구사항 1: 엔터티 매핑
#### 수용 기준
1. THE NGSI_Adapter SHALL Vessel, Device, Sensor, DeviceStatus, VesselPosition, Observation, Event를 NGSI-LD 엔터티로 매핑한다.
2. THE NGSI_Adapter SHALL 영상 자산은 asset_id 참조만 넣고 파일 내용이나 사전 서명 URL을 넣지 않는다.

### 요구사항 2: 노출 범위
#### 수용 기준
1. THE NGSI_Adapter SHALL sharing_scope=PUBLIC 데이터와 통계만 외부에 노출한다.
2. IF 위치 정밀도 제한(location_precision)이 걸린 데이터이면, THEN THE NGSI_Adapter SHALL 일반화된 위치만 내보낸다.

### 요구사항 3: 적합성
#### 수용 기준
1. THE 시스템 SHALL 엔드포인트 이름이나 JSON-LD 형식만 흉내 낸 구현을 "NGSI-LD 지원"으로 표기하지 않는다.
2. THE 통합 테스트 SHALL 실제 Context_Broker와 상대 허브를 상대로 조회·갱신·구독 적합성을 시험한다 (T19).

### 요구사항 4: 광역 허브 연계
#### 수용 기준
1. THE Hub_Connector SHALL 상대 허브가 확정한 교환 스키마로 공개 메타데이터·통계만 동기화한다.
2. THE Hub_Connector SHALL 원본 영상과 L3 이하 데이터를 보내지 않는다.

## 추적
- 기능 ID: INT-01, INT-02
- 수용시험: T19(T19-a, T19-b) — 세부는 docs/ACCEPTANCE.md
- 외부 의존: 사업계획상 의무, 상대 시스템 명세
