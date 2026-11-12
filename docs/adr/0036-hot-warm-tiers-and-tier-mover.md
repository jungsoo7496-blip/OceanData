# ADR-0036: Hot/Warm 계층과 Tier_Mover — 설정 계층, 단일 위치 해석기, 복사 → 다시 읽어 sha256 → 전환 → 유예 삭제

- 상태: accepted
- 날짜: 2026-11-05
- 관련: 08번 요구사항 9.1~9.4·10.1, tech.md HW-1·HW-3·HW-8, domain.md 9장(존·객체 키), ADR-0001(S3 API만),
  ADR-0021(백업 대상 술어), ADR-0033(PermanentJobError)

## 맥락
요구사항 9는 계층(HOT·WARM)을 설정값으로 정의하고(계층마다 엔드포인트·버킷을 따로 두거나 같은 저장소를 함께 씀),
정책 표의 규칙(대상 존·변형, 경과 일수, 마지막 조회 후 일수)으로 객체를 다른 계층에 복사해 sha256이 같을 때만
원래 객체를 지우고 위치를 바꾸라고 한다. 이동 중에도 조회·다운로드 URL 발급이 끊기면 안 되고, 스토리지 제품의 자동
계층화에 기대면 안 된다. 지금까지는 자산 객체를 읽는 곳(다운로드 URL·처리 작업·데이터셋 사본·백업·삭제·무결성 검증 등
약 17곳)이 `settings.buckets[존]`으로 버킷을 직접 골랐다. 실제 WARM 장비·제품은 하드웨어 확정(G2) 전이라 정해지지 않았다.

## 결정
1. **설정 모델.** HOT은 기존 `S3_*`·`BUCKET_*` 그대로다. WARM은 `WARM_S3_ENDPOINT_URL`·`WARM_S3_PUBLIC_ENDPOINT_URL`·
   `WARM_S3_REGION`·`WARM_S3_ACCESS_KEY_ID`·`WARM_S3_SECRET_ACCESS_KEY`·`WARM_S3_FORCE_PATH_STYLE`과 존별
   `WARM_BUCKET_RAW`·`_PROC`·`_EVIDENCE`·`_DATASETS`(비우면 `<HOT 버킷>-warm`). 주소·자격증명을 비우면 HOT과 같은 S3 연결을
   쓰고 버킷만 다르다(같은 저장소 공유). 격리 존은 HOT에만 있다. 같은 주소에서 WARM 버킷이 HOT 버킷과 같으면 이동이 하나뿐인
   사본을 지우므로 기동하지 않는다(설정 오류, 키 이름만 알림). 자동 이동 스위치는 `TIERING_ENABLED`(기본 false).
2. **위치 = 자산 행의 (storage_tier, storage_zone, storage_key).** 객체 키는 계층과 무관하다(domain.md 9장 규칙 그대로).
   자산 객체를 읽는 곳은 모두 `services/tiers.locate()` 하나를 쓴다: 다운로드 URL, 처리 작업 임시 사본(`local_copy` — 프록시·
   썸네일·비식별·태깅·클립·검수 프레임·데이터셋 샘플), 센서 재파싱, 수신 무결성 검증, 무결성 표본 검증, 데이터셋 사본, 백업,
   삭제, 격리·재검증 이동, 일일 사용량 대조. 새 객체(업로드·파생본·보고서·데이터셋 사본)는 언제나 HOT에 쓴다. 격리·재검증
   이동은 HOT으로 간다(`relocate` — 같은 저장소면 기존 move_object, 다르면 스트림 복사 뒤 원래 객체 삭제).
3. **정책 표 `tier_policies`.** 열: 존(RAW·PROCESSED·EVIDENCE·DATASET), 변형(비우면 전부), `min_age_days`(ingested_at 기준),
   `min_idle_days`(마지막 조회 `last_accessed_at`, 없으면 ingested_at 기준, 비우면 보지 않음), `enabled`. 규칙은 HOT → WARM만.
   초안 1건 `raw_original_to_warm`(RAW·ORIGINAL·180일·90일)은 **꺼져 있고 값은 운영기관 확정 필요**다. 대상 자산: 삭제되지 않음,
   바이너리 있음, REGISTERED·PROXIED·READY_PRIVATE, HOT, 그 자산의 처리 작업이 대기·실행 중이 아님, 열린 이동 없음, 최근
   (`TIER_FAILED_RETRY_AFTER_S`, 기본 1일) 실패 없음, 원본 손상으로 실패한 적 없음. `last_accessed_at`은 사용자가 바이너리를
   열 때 갱신한다: 다운로드 URL 발급, 비식별 검수 프레임, 구간 클립 요청. 데이터셋 배포 URL은 DATASET 존 사본을 가리키므로
   원 자산 조회로 보지 않는다. 처리·백업 같은 시스템 읽기도 조회가 아니다.
4. **순서** (`tier_moves`, 자산마다 열린 이동(COPYING·SWITCHED)은 하나 — 부분 유일 인덱스):
   1. 고르기: 정비 주기(약 1분)마다 복사 중인 이동이 `TIER_MAX_IN_FLIGHT`(기본 2)보다 적으면 COPYING 행을 만들어 **커밋한 뒤**
      작업 `tier_move`를 넣는다(우선순위 0, 임대 6시간, 최대 3회).
   2. 복사·확인(작업): 두 위치가 물리적으로 다른 공간인지 표식 객체(`_tier_probe/…`, PUT·HEAD·DELETE)로 확인 → 원래 객체 HEAD
      → 원래 객체 GET 스트림을 대상에 PUT/멀티파트로 쓰면서 원래 쪽 sha256 계산 → **대상 사본을 끝까지 다시 읽어** sha256·크기를
      자산 기록(checksum_sha256·byte_size)과 비교한다. ETag는 믿지 않는다. 다시 실행되면 이미 확인되는 사본은 다시 쓰지 않는다.
   3. 전환: 자산 행을 잠그고 그사이 삭제·이동·변경이 없었는지 다시 본 뒤 storage_tier를 바꾸고 SWITCHED,
      `delete_after = 지금 + max(TIER_DELETE_GRACE_S(기본 3600), PRESIGN_DOWNLOAD_TTL_S)`. 여기까지 모든 읽기는 원래 위치다.
   4. 유예 삭제(정비 주기): delete_after가 지났고 전환 전에 시작한 그 자산의 처리 작업(RUNNING)이 없으면 원래 객체를 지우고
      DONE. 원래 자리가 다시 자산의 현재 위치가 됐거나 물리적으로 같은 공간이면 지우지 않는다. 자산이 그사이 지워졌으면 모든
      계층에서 지운다.
   - 이동 중 조회(9.3): 전환 전에는 원래 위치를 읽고 URL도 원래 위치로 발급한다. 전환 직전에 발급한 URL은 유예(≥ URL 유효시간)
     동안 원래 객체가 남아 끝까지 쓰인다. 기본 유예 1시간은 URL 5분보다 길어 서버 사이 시계 차이도 흡수한다. 전환 뒤 긴 읽기가
     유예를 넘겨 실패하면 지금 위치를 다시 보고 한 번 다시 읽는다(`local_copy`·데이터셋 사본·백업).
5. **실패 처리.**
   - 사본 sha256 불일치(`COPY_CHECKSUM_MISMATCH`): 사본을 지우고 FAILED. 원래 객체·위치 그대로. 작업은 PermanentJobError로
     재시도하지 않고, 정책이 재시도 간격 뒤 다시 고른다.
   - 원래 객체가 기록과 다름·없음(`SOURCE_CHECKSUM_MISMATCH`·`SOURCE_MISSING`): 사본을 지우고 FAILED, 운영자 알림
     (`tier.source_problem`), 다시 고르지 않는다(`make verify-integrity`로 조사).
   - 같은 저장 공간(`TIER_SAME_LOCATION`, 같은 저장소를 다른 주소로 가리킨 설정): 아무것도 지우지 않고 FAILED. 고르는 단계에서도
     확인해 이동을 만들지 않는다.
   - 저장소 일시 오류: 작업 재시도(지수 백오프), 다 쓰면 FAILED와 사본 정리.
   - 복사 중 자산이 지워지거나 격리·재검증으로 옮겨짐: CANCELLED, 사본 정리. `TIERING_ENABLED`를 끄면 진행 중 정책 이동은
     CANCELLED(되돌리기 이동은 계속). 커밋 뒤 작업을 넣지 못한 이동은 10분 뒤 정비 주기가 작업을 다시 넣는다.
   - 사본 정리가 실패하면 경고 로그를 남기고, 일일 사용량 대조가 차이로 드러낸다.
   - 삭제(요청·보존 만료)는 쓰는 모든 계층에서 같은 키를 지운다 → 이동과 삭제가 겹쳐도 사본이 남지 않는다. 일일 대조는 WARM
     버킷까지 재고, 잠시 두 벌인 객체(복사 중 사본·유예 중 원래 객체)는 빼고 비교한다.
6. **법적 보류 자산도 옮긴다.** 요구사항 4.1의 보류는 Retention_Engine·Deletion_Service의 *삭제* 금지다. 이동은 sha256·크기가
   기록과 같은 사본을 다시 읽어 확인하고 유예까지 지난 뒤에만 원래 객체를 지우므로 자산의 내용·checksum_sha256·무결성 로그·
   수신 영수증이 바뀌지 않는다. `tier_moves`에 이동 당시 법적 보류 여부·계층·시각을 남겨 보관 경위를 보인다. 보류 자산의 삭제
   요청은 계층과 무관하게 계속 거부된다. 법률 자문이 "원래 저장 매체 보존"을 요구하면 대상 술어에 `legal_hold = false`를 더한다
   (확인 필요). 분쟁 중 자주 여는 자산은 '마지막 조회 후 일수' 조건으로 HOT에 남는다.
7. **운영 가시성 — API는 더하지 않는다.** `python -m app.cli tiers`(dry-run: 규칙별 대상 수·용량, 계층별 저장량, 이동 상태·최근
   실패), `--recall`(WARM → HOT 되돌리기), 메트릭 `tier_moves_total{to_tier,result}`·`tier_move_bytes_total{to_tier}`,
   `/readyz` 경고 `storage_tier_unavailable:WARM`·`storage_encryption_unsupported:WARM:<존>`(준비 상태는 바꾸지 않는다 — HOT
   업로드·조회는 계속된다). 월간 리포트 `storage_by_tier`는 스냅숏의 계층별 합(`usage_snapshots.by_tier`, 0022 이전 스냅숏은
   모두 HOT). 계약 변경은 설명만이다(UsageReport.storage_by_tier, Readiness.warnings).
8. **마이그레이션 0022.** assets.storage_tier(기본 HOT)·last_accessed_at·`ix_assets_tier_age`, tier_policies(+초안 1건, 꺼짐),
   tier_moves, usage_snapshots.by_tier, job_policies `tier_move`. down은 WARM 자산이나 열린 이동이 있으면 멈춘다(되돌린 코드는
   계층을 몰라 WARM 객체를 읽지 못하고 사본이 고아가 된다).

버린 대안: (a) 제품 수명주기 전환·스토리지 클래스·제품 계층화 — HW-1 위반, 제품 미정. (b) CopyObject(서버 측 복사) — 공통 기능
목록 밖이고 저장소가 다르면 못 쓴다. GET 스트림 → PUT/멀티파트로 임시 디스크 없이 복사한다. (c) ETag로 사본 확인 — 멀티파트
ETag는 내용 해시가 아니고 제품마다 다르다. (d) 객체 키에 계층 접두사 — domain.md 9장 키 규칙이 바뀐다. (e) 전환과 동시에 원래
객체 삭제 — 직전에 발급한 URL·진행 중 읽기가 끊긴다. (f) 계층을 공통 메타데이터(asset-metadata)에 노출 — domain.md 8장 변경(사람
승인 필요)이고 저장 위치는 내부 사항이다. (g) 이동·되돌리기 운영 API — 요구사항에 없고 CLI·메트릭으로 충분하다(필요해지면 계약 먼저).

## 결과
- 좋아지는 점: 하드웨어가 오면 설정(`WARM_*`)과 정책 표 값만 바꿔 동작한다. 읽는 곳이 한 해석기를 쓰므로 계층이 늘어도 한 곳만
  고친다. 원래 객체를 지우기 전에는 언제나 같은 내용으로 확인된 사본이 있다.
- 감수할 점: 객체마다 원래 쪽 한 번·사본 한 번 전체를 읽는다(읽기 2배 + 쓰기 1배). 유예 동안은 두 벌이다. 정비 주기 간격·동시 이동
  수는 실측 뒤 조정한다(HW-8). 자주 여는 WARM 자산을 HOT으로 자동 승격하지 않는다(운영자 `--recall`만). 데이터셋 버전 사본
  (dataset_version_items)과 자산이 아닌 객체(보고서 스냅숏·외부 원 응답)는 옮기지 않는다. 같은 공간 확인용 표식 객체가 대상
  버킷에 잠깐(수 ms) 생긴다. 복사는 내용과 Content-Type만 옮긴다 — 지금은 객체 태그·사용자 메타데이터를 쓰지 않는다(쓰게 되면
  Tier_Mover도 함께 복사해야 한다).
- 확인 필요: 실제 WARM 장비·제품·엔드포인트·용량·대역폭, 운영기관이 정할 규칙 값, 보류 자산의 매체 보존 요구(법률 자문),
  WARM 저장소의 서버 측 암호화 지원.
- 되돌리는 방법: `TIERING_ENABLED=false`(진행 중 정책 이동은 취소) → `python -m app.cli tiers --recall` → 워커가 HOT으로 옮기고
  유예 삭제까지 끝나면 0022 down. WARM 버킷은 사람이 지운다.
