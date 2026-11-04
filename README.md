# 해양 실증데이터 플랫폼 — Kiro 작업 패키지 v0.2

> **개발 진행 중.** 현재 상태와 로컬에서 이어가는 방법은 [`HANDOFF.md`](HANDOFF.md)를 먼저 본다.
> 빠른 시작: `cp .env.example .env && make venv && make up && make seed`

기준 문서 v0.04를 Kiro가 읽는 형식(steering + spec)으로 옮기고, GPT v0.05 검토 결과와 오픈소스 재사용 판정을 합친 패키지입니다.
하드웨어가 확정되기 전에 소프트웨어를 먼저 만들 수 있도록 스토리지·GPU·배포 형태를 인터페이스 뒤로 숨기는 규칙을 steering에 넣었습니다.
v0.1에서 바뀐 점은 `CHANGELOG.md`에 있습니다.

## 들어 있는 것
```
AGENTS.md                  AI 작업 규칙 (Kiro가 항상 읽음)
Makefile                   contracts-check, traceability
.kiro/steering/            product, tech, structure, domain (항상) / security, api-conventions (파일 작업 시) / baseline-reference (수동)
.kiro/specs/01~17/         requirements.md (설계·작업은 Kiro가 만든다)
contracts/                 OpenAPI 3.1, JSON Schema 11개, 코드표 5개, 예시(통과 17 / 실패 24)
tools/                     validate_contracts.py, check_traceability.py
docs/
  ROADMAP.md               진행 순서, 사업 게이트, 하드웨어 후 할 일
  ACCEPTANCE.md            수용시험 T01~T20 세부 케이스 (GPT AT-01~30 대응 포함)
  OSS-CANDIDATES.md        오픈소스 59건 + 추가 후보 판정
  TRACEABILITY.md          기준 문서 기능 ID·수용시험 ↔ 스펙 추적표 (자동 생성)
  adr/                     결정 기록 10건 + 양식
  baseline-v0.04.md        기준 문서
```

## 스펙 (폴더 번호 = 진행 순서)
01 개발 기반 → 02 인증·테넌트 → 03 메타데이터 표준 → 04 수집 API → 05 처리 파이프라인 → 06 에이전트 → 07 센서·이벤트 → 08 저장·보존·백업 → 09 워크스페이스 → 10 증빙·보고서 → 11 수집 모니터링 → 12 공공 Open API → 13 공유·데이터셋 → 14 비식별·태깅 → 15 엣지 수집기(장비 확인 후) → 16 NGSI-LD(조건부) / 17 수용시험·릴리스(01부터 병행)

## 사용 순서
1. 새 Git 저장소 루트에 이 패키지 내용을 그대로 복사합니다. `.kiro`는 점으로 시작하는 폴더라 macOS Finder 등에서는 숨김 폴더로 보일 수 있습니다.
2. 계약 검사를 한 번 돌려 봅니다: `pip install -r tools/requirements.txt` 후 `make contracts-check`, `make traceability`.
3. Kiro로 저장소를 열고 Steering 패널에서 파일이 인식됐는지 확인합니다.
4. `tech.md`의 "결정 대기 항목"을 먼저 봅니다. 발주처가 전자정부 표준프레임워크를 요구하면 개발 전에 API 서버 스택부터 고칩니다.
5. 12월 G1 회의 전에 `contracts/`를 검토·수정합니다 (`contracts/README.md`의 "동결 회의에서 정할 것").
6. 스펙을 번호 순서대로 하나씩 진행합니다. 17번은 01번과 함께 시작합니다.
   1. Specs 패널에서 스펙을 열고 `requirements.md`를 검토합니다.
   2. 설계(design.md)를 생성하고 검토합니다.
   3. 작업(tasks.md)을 생성하고 검토한 뒤 실행합니다.
   4. 테스트·계약 검사가 통과하면 다음 스펙으로 넘어갑니다.
7. 스펙이 패널에 보이지 않으면 Specs의 `+`에서 Feature Spec → Requirements-First로 같은 이름의 스펙을 만들고, 이 패키지의 `requirements.md`를 그대로 쓰라고 지시합니다.

## Kiro에 줄 지시 예시
- 설계: `01-dev-foundation의 requirements.md를 그대로 기준으로 design.md를 작성해줘. 요구사항은 다시 쓰지 말고, structure.md의 스펙 작성 규칙과 tech.md의 하드웨어 독립 규칙, docs/OSS-CANDIDATES.md의 판정을 지켜.`
- 작업: `design.md를 기준으로 tasks.md를 만들어줘. 작업은 반나절~하루 단위로, 각 작업이 실제로 구현하는 요구사항 번호와 테스트를 붙여.`
- 계약 변경: `이 변경은 contracts/부터 고치고 make contracts-check를 통과시킨 다음 구현해줘.`
- 맥락: `#baseline-reference 7장의 메타데이터 정의를 확인하고 03번 설계에 반영해줘.`

## Kiro 결과물을 검토할 때 볼 것
- 테넌트 범위를 화면에서만 가리고 서버에서 빠뜨리지 않았는가 (T01)
- 하드웨어(GPU·특정 스토리지)를 사야만 실행되게 만들지 않았는가
- 업로드가 곧 공유가 되는 경로가 생기지 않았는가 (domain.md 4장)
- 근거 없는 촬영시각·지표 값을 만들어 넣지 않았는가
- `contracts/`와 구현이 어긋나지 않는가 (계약 시험)
- tasks.md의 요구사항 번호가 실제 구현 내용과 맞는가
- 테스트를 돌리지 않고 완료로 표시하지 않았는가

## 주의
- `requirements.md`는 이미 작성되어 있습니다. Kiro에게 처음부터 다시 쓰게 하지 말고 고칠 부분만 지시하세요.
- 공유 등급·상태·필드명 같은 값을 바꾸려면 `domain.md`부터 고치고 `contracts/`를 맞춘 뒤 `make contracts-check`를 돌리세요.
- 오픈소스는 `docs/OSS-CANDIDATES.md`의 판정을 따릅니다. "PoC" 항목은 결과 ADR이 생긴 뒤에 씁니다.
- 15번은 앵커 기업 장비 정보를 받은 뒤, 16번은 도입 게이트를 통과한 뒤 진행합니다.
