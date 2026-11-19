# ADR-0082: 업로드 완료 호출의 저장소 장애는 503, 저장소가 파트 목록을 거부할 때만 409

- 상태: accepted
- 날짜: 2026-11-13
- 관련: 04번 요구사항 4.1(완료), 1.6(저장소 장애 503), 3.1(재개 조회), 1.1(파트 URL), ADR-0044(신고 크기 강제), ADR-0053(완료 재시도),
  ADR-0065(업로드 정리 저장소 오류), 계약 점검 S45·S46·S47(2026-11-12 업로드 묶음)

## 맥락

완료 요청(`POST /v1/uploads/{upload_id}/complete`, `IngestService.complete`)은 저장소의 파트 목록을 새로 읽어 크기를 확인하고
(`_check_part_sizes`), 요청의 파트 번호·ETag를 그 목록과 맞춘 뒤 저장소의 멀티파트 완료(`complete_multipart_upload`)를 부른다.
이 마지막 호출이 맨 `except Exception`으로 감싸여 있어, 어떤 예외든 409 `CONFLICT` '파트 목록이 저장소와 맞지 않습니다 (GET .../parts로
확인)'가 되고 `Retry-After`도 없었다(계약 점검 S46). 파트 목록은 바로 앞에서 저장소 목록과 맞춰 봤으므로, 실제로 이 except에 닿는 것은
대부분 연결 실패·시간 초과·저장소 내부 오류다. 계약은 409를 파트 목록 불일치에만 쓰고, 저장소 장애는 503 `STORAGE_UNAVAILABLE`과
`Retry-After`다(api-conventions.md 오류 표, 요구사항 1.6). 같은 서비스의 다른 저장소 호출(파트 목록 조회, 크기 확인, 정리 — ADR-0065)도
장애를 503으로 알린다. 예제 클라이언트(`examples/python/marine_client.py`)는 `Retry-After` 없는 409를 끝난 실패로 보므로 짧은 장애
하나로 업로드를 멈췄다. 에이전트는 CONFLICT도 자체 간격으로 다시 시도한다.

같은 묶음의 두 발견:
- S45: 재개 조회(`listUploadParts`)는 저장소에서 파트 목록을 읽지 못하면 503이었는데 계약에 없었다. 이 계약 문구는 검토 C4(ADR-0065
  결정 4)가 이미 넣었다 — 여기서는 시험으로 고정만 한다.
- S47: `parts`(createUpload)와 `pending_parts`(listUploadParts)는 응답마다 `max_part_urls_per_response`(기본 1000개)까지만 담는다.
  기본 파트 크기(64 MiB)로 약 62.5 GiB를 넘는 파일이면 목록이 잘리는데 응답에도 계약에도 그런 말이 없었다. 에이전트
  (`agent/internal/core/agent.go` UPLOAD_CREATED 분기), 예제 클라이언트, 웹(Uppy `signPart`)은 짧은 목록을 일부러 다시 받는다 — 동작은 의도된 것이다.

## 결정

1. **저장소의 판정과 장애를 나눈다.** S3 어댑터(`S3Storage.complete_multipart_upload`)가 S3 오류 코드 `InvalidPart`·`InvalidPartOrder`·
   `EntityTooSmall`·`NoSuchUpload`(`PART_LIST_ERRORS`)만 포트 예외 `PartsRejected`로 바꾼다. 서비스는
   - `PartsRejected` → 지금처럼 409 `CONFLICT`(같은 detail, `Retry-After` 없음), 세션 유지. 로그 `complete_multipart_rejected`(업로드 ID, 저장소 오류 코드).
   - 그 밖의 모든 예외(botocore 연결·시간 초과 오류, `InternalError`·`SlowDown` 같은 다른 S3 오류, 어댑터 밖의 예외) → 503
     `STORAGE_UNAVAILABLE`, `Retry-After: 30`. 로그 `complete_multipart_failed`(업로드 ID, 예외 이름 — 저장소 주소·멀티파트 ID는 남기지 않는다).
   두 경우 모두 DB를 바꾸기 전이라 요청 트랜잭션이 되돌려져 세션(UPLOADING)·쿼터 예약·자산 기록·멀티파트가 그대로이고, 오류 응답은 멱등 기록으로
   남지 않아 같은 `Idempotency-Key`로 다시 보낼 수 있다. 저장소가 실제로는 완료했는데 응답만 잃었으면, 다시 보낸 완료가 `_completed_in_storage`
   (신고 크기의 완성 객체 확인)로 먼저 마친다(ADR-0053과 같은 경로).
   - 분류를 어댑터에 둔 까닭: 서비스는 botocore를 모른다. ADR-0065가 멀티파트 중단의 `NoSuchUpload`를 어댑터에서 처리한 것과 같은 자리다.
     다른 포트 구현은 '파트 목록 거부는 `PartsRejected`'만 지키면 된다. 시험용 `FakeStorage`는 완료 호출이 없어(그 픽스처로는 업로드를 열 수 없다) 바꿀 것이 없다.
   - `NoSuchUpload`는 409로 둔다(지금과 같다). 멀티파트가 저장소에 없다는 저장소의 판정이라 같은 요청을 다시 보내도 결과가 같다 — 503(다시 시도)은 맞지 않다.
     저장소가 이미 완료한 경우는 위처럼 다시 보낸 완료가 409든 503이든 `_completed_in_storage`로 먼저 끝난다. 저장소가 완료 뒤 같은 완료 요청에
     `NoSuchUpload`를 주는지 성공을 주는지는 저장소마다 다를 수 있어 **확인 필요**이고, 이 결정은 거기에 기대지 않는다.
   - `EntityTooSmall`도 409로 둔다(지금과 같다). 크기 확인이 계획과 같은 파트만 통과시키므로 이 오류는 계획한 파트 크기(`UPLOAD_PART_SIZE`)가
     저장소 최소보다 작게 설정됐을 때만 난다. 다시 보내도 같으므로 503은 맞지 않다. SeaweedFS가 최소 파트 크기를 강제하는지는 **확인 필요**.
2. **계약**: `completeUpload` 설명에 '멀티파트 완료 호출이 파트 목록 거부가 아닌 이유로 실패해도 503 + `Retry-After`, 세션·예약 유지, 같은 요청을 다시
   보낸다(저장소에서 이미 완료됐으면 그 객체로 마친다)'를 넣었다. 응답 코드 목록은 그대로다(503은 이미 있다).
3. **S45**: 구현은 그대로다. 계약의 503과 설명은 C4에서 들어왔고, 문서화된 동작(503·`Retry-After`·세션 유지, 다시 조회하면 200)을 시험으로 고정한다.
4. **S47**: 한도 동작은 그대로 두고 계약 설명만 고쳤다 — `createUpload` 설명, `UploadCreated.parts`, `PartList.pending_parts`에 '서버 한도(기본 1000개)까지,
   낮은 파트 번호부터, `part_count`보다 적으면 올린 뒤 `GET /v1/uploads/{upload_id}/parts`로 나머지를 받는다, 잘렸다는 표시는 없다'를,
   `PartList.completed_parts`에 '한도로 잘리지 않는다'(S3 어댑터가 ListParts를 끝까지 넘겨 읽는다)를 적었다. 스키마 모양은 바꾸지 않았다.
   `MAX_PART_URLS_PER_RESPONSE`는 새 설정이 아니어서 `.env.example`·Compose에 넣지 않았다(지금은 `config.py` 기본값만 쓰인다).

버린 대안:
- 서비스에서 예외의 `response["Error"]["Code"]`를 읽어 나누기 — 서비스가 botocore 예외 모양에 기대게 된다.
- 모든 `ClientError`는 409, 연결 오류·시간 초과만 503 — `InternalError`·`SlowDown`(S3 5xx)도 장애인데 '파트 목록이 틀렸다'가 된다.
- 위 네 코드 밖의 4xx(`AccessDenied`·`NoSuchBucket` 등)도 409로 — 클라이언트의 파트 목록과 관계없는 저장소·설정 문제이고, 같은 서비스의 다른 저장소
  호출(세션 생성·파트 목록 조회)도 모든 예외를 503으로 본다.
- 완료 호출이 실패하면 바로 `head_object`로 완성 객체를 확인해 202로 마치기 — 장애 중에 저장소 호출을 하나 더 하고, 다시 보낸 완료가 이미 같은
  확인을 한다. 발견의 범위를 넘는다.
- 파트 URL 한도를 없애거나 응답에 '잘림' 필드를 더하기 — 응답이 커지고(URL 최대 10000개) 계약 스키마가 바뀐다. 클라이언트는 이미 `part_count`와 비교한다.

## 결과

좋아지는 점: 완료 단계의 짧은 저장소 장애가 `Retry-After`가 붙은 503이 되어 예제 클라이언트·에이전트가 다시 보내 끝낸다. 409는 저장소가 파트 목록을
거부했을 때만이라 'GET .../parts로 확인하라'는 안내가 맞는 경우에만 나간다. 계약만 보고 만든 클라이언트도 파트 URL 한도와 나머지를 받는 방법을 안다.

감수할 점:
- 위 네 코드 밖의 영구 오류(권한·버킷 설정 등)도 503이라, 클라이언트는 고칠 때까지(또는 세션 유효 기간 동안) 다시 시도한다. 오류 로그
  `complete_multipart_failed`(예외 이름)로 드러난다. 전에는 409로 멈췄다.
- 503을 받은 클라이언트가 다시 보내지 않으면, 저장소가 실제로는 완료한 경우의 완성 객체는 만료 정리가 지운다(ADR-0065) — 완료 뒤 커밋 실패와 같다.
- 409 집합은 S3 오류 코드 이름에 기댄다. 다른 S3 호환 저장소가 같은 판정을 다른 코드로 주면 503이 된다(다시 보내도 같은 결과면 유효 기간 동안 503) —
  운영 저장소(SeaweedFS)의 완료 오류 코드는 **확인 필요**.
- `MAX_PART_URLS_PER_RESPONSE`는 배포 설정 파일에 없다. 운영에서 바꿀 수 있게 할지는 사람 결정이다.

되돌리는 방법: `IngestService.complete`의 except를 하나(모든 예외 409)로 되돌리고 어댑터의 `try`·`PART_LIST_ERRORS`와 포트의 `PartsRejected`를 지운다.
계약 설명 문장을 되돌리고 웹·에이전트 타입을 다시 만든다. 마이그레이션 없음.

## 시험

`tests/integration/test_upload_parts_complete_contract.py` 12개(실제 S3 API — moto 또는 TEST_S3_ENDPOINT, 합성 값, 사용자 102):
- S46 완료 호출 장애 4개(어댑터 밖 `ConnectionError`, botocore `EndpointConnectionError`·`ReadTimeoutError`, S3 `InternalError`): 503·`Retry-After: 30`·계약 대조,
  세션·예약·자산·멀티파트 그대로, 객체 없음, 멱등 기록 없음, 로그에 예외 이름만. 같은 `Idempotency-Key`로 다시 보내면 202 RECEIVED(재생 아님).
- 저장소가 완료하고 응답만 잃은 경우 1개: 503 뒤 다시 보낸 완료가 완성 객체로 202.
- 저장소의 파트 목록 거부 4개(`InvalidPart`·`InvalidPartOrder`·`EntityTooSmall`·`NoSuchUpload`): 409 `CONFLICT`·같은 detail·`Retry-After` 없음, 세션 유지,
  `complete_multipart_rejected` 로그에 저장소 코드. 저장소가 돌아오면 202.
- 저장소 목록과 다른 ETag는 완료 호출 전에 409(완료 호출 없음) 1개.
- S45 파트 목록 조회 실패 1개: 503·`Retry-After`·`STORAGE_UNAVAILABLE`·계약 대조, 세션·예약 유지, 다시 조회하면 200.
- S47 1개: 계약의 세 설명에 한도(설정 기본값과 같은 수)와 `GET .../parts` 안내, `completed_parts`는 잘리지 않음. 한도를 2로 줄이면 4파트 업로드의 `parts`는 [1, 2],
  1번만 올린 뒤 `pending_parts`는 [2, 3], 그다음 [4], 모두 올리면 완료 202.

수정 전 코드(기준 커밋 `81c7f08`)에서 10개가 실패하는 것을 확인했다 — 장애 4개와 응답 유실 1개는 409, 파트 목록 거부 4개는 상태(409)는 맞았지만 거부 로그가 없었고,
S47은 계약 설명이 없었다. 나머지 2개는 수정 전에도 맞던 동작을 지키는 시험이다: ETag 불일치 409, 그리고 S45 — 이 시험은 C4 반영 전 계약
(`origin/main` `b6bc039`의 `contracts/`를 `CONTRACTS_DIR`로 주고)에서 '계약에 503 응답이 없다'로 실패하는 것을 확인했다.
이웃 시험 `test_upload_release.py`·`test_complete_after_commit_failure.py`·`test_upload_cleanup_storage_errors.py`·`test_ingest.py`와 `tests/unit`·`tests/contract`도 통과한다.
