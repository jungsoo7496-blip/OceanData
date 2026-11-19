# ADR-0069: 클립 작업의 시간 한도·전용 워커·임대 갱신

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 09번 요구사항 7.2(구간 클립), 05번 요구사항 1.2·1.3(배정·임대 회수)·6.1(우선순위)·6.3(처리 지연이 업로드에 영향 없음),
  ADR-0014(자체 작업 큐), ADR-0033(클립 내보내기), ADR-0038(작업 정책 편집), 지시서 12번 클립·업로드 검토 C7

## 맥락

워커는 한 스레드로 작업을 하나씩 처리한다. 검토(C7)는 클립 작업(`clip_export`)이 이 스레드를 오래 쥘 수 있다고 했다 — ffmpeg 한도가
전체 한도 `FFMPEG_TIMEOUT_S`(기본 3600초)이고 구간은 `CLIP_MAX_S`(기본 3600초)까지다(검토 실측: 30초 클립 6.5초). 그동안 무결성 검증·
메타 추출·프록시 같은 수집 작업은 기다린다. 코드를 보고 확인한 것:

1. **시간 한도**: 30초 클립도 인코딩이 멈추면 한 시간을 기다린다. 시간 초과(`subprocess.TimeoutExpired`)를 잡지 않아 코드 `INTERNAL`로
   재시도한다(정책 최대 3회) — 한 시간씩 세 번.
2. **전용 워커**: 둘 방법이 없다. `WORKER_RESOURCE_CLASSES`는 GPU가 있는지만 본다. 배정 조건이
   `resource_class = 'CPU' OR 워커에 GPU OR gpu_fallback = 'CPU'`라, CPU가 아닌 이름(예: `CLIP`)을 워커에 줘도 CPU 작업을 모두
   가져가고, 그런 클래스의 작업은 대체 규칙(CPU)으로 아무 워커나 가져간다. 이 설정은 Compose·`.env.example`에도 없다.
3. **임대**: `clip_export` 임대는 2시간(0021)인데 작업 시간은 원본 받기 + 인코딩(최대 `FFMPEG_TIMEOUT_S`) + 결과 다시 읽기(최대
   `FFMPEG_TIMEOUT_S`) + 올리기라 임대 안에 끝난다는 보장이 없다. 워커가 둘 이상이면 다른 워커의 주기 정리(`reap_expired_leases`)가
   아직 도는 작업을 QUEUED로 돌리고 다른 워커가 다시 가져가 같은 클립을 두 번 인코딩한다(재현: 임대 3초 정책에서 5.5초 걸리는 클립 →
   회수 1회, 다른 워커가 가져감). `complete`는 누가 쥐었는지 보지 않아, 먼저 끝난 쪽이 다른 쪽이 도는 작업을 DONE으로 만든다.
4. **우선순위**: 정책 표(0005·0021)는 integrity_verify 100, media_probe 85, make_proxy 80, make_thumbnail 75, clip_export 70이다.
   대기 중인 수집 작업이 대기 중인 클립보다 먼저 배정된다(맞다). 정책 행이 없는 유형은 아래 '남은 것'.

### 측정 (배수의 근거)

개발 PC(Intel i7-12700, 논리 코어 20, 시험 컨테이너 안, 다른 시험이 함께 돌던 때), ffmpeg 7.1.5, 앱의 `clip_args` 그대로(libx264
veryfast, CRF 18, AAC 128k), 합성 testsrc2 30fps 원본(8 Mbps, 4K는 30 Mbps). 괄호는 구간 1초당 인코딩 초다.
스레드 4개는 작은 운영 장비를 흉내 낸 것이다.

| 원본 | 구간 | 스레드 자동 | 스레드 4개 |
|---|---|---|---|
| 640×360 | 60초 | 1.9초 (0.03) | 1.9초 (0.03) |
| 1280×720 | 60초 | 4.6초 (0.08) | 7.2초 (0.12) |
| 1920×1080 | 60초 | 6.2초 (0.10) | 12.7초 (0.21) |
| 3840×2160 | 30초 | 11.1초 (0.37) | 25.9초 (0.86) |

합성 영상은 실제 해상 영상(파도·잡음)보다 압축이 쉬워 실제로는 더 걸린다. 운영 하드웨어·실제 영상 실측은 확인 필요다.

## 결정

1. **시간 한도는 구간 길이에 비례한다.** `clips.clip_timeout_s` =
   `min(FFMPEG_TIMEOUT_S, max(CLIP_TIMEOUT_MIN_S, ⌈구간 길이 × CLIP_TIMEOUT_FACTOR⌉))`, 기본 배수 4·최솟값 120초.
   배수 4는 위 측정에서 가장 느린 경우(4K·스레드 4개, 0.86)의 약 4.6배, 1080p 자동 스레드의 약 40배 여유다. 최솟값은 프로세스 시작과
   키프레임부터 디코딩하는 시간(키프레임 간격이 긴 원본)을 덮는다. 30초 클립은 120초, 60초는 240초, 900초부터는 전체 한도(3600초)다.
   결과 다시 읽기(ffprobe)도 같은 한도를 따로 받는다 — 그래서 클립 하나가 워커를 쥐는 시간은 최악에 이 한도의 두 배(인코딩 +
   다시 읽기)에 원본 받기·올리기 시간을 더한 것이다(통합 검토 CLIPS-5).
   - 한도를 넘으면 `subprocess.run`이 ffmpeg 프로세스를 끝내고, 작업은 `PermanentJobError("CLIP_TIMEOUT")`로 재시도 없이 FAILED
     (시도 1번)다 — 같은 구간·같은 장비는 다시 해도 같은 시간이 걸리고 재시도는 비용만 늘린다. 받아 둔 원본·임시 폴더는 지금처럼
     지운다. 클립 목록에 `error_code=CLIP_TIMEOUT`이 보이고, 화면에 문구를 더했다. 계약은 설명만 바뀐다(`ClipJob.error_code`의 예,
     클립 POST 설명 한 줄) — 계약 먼저 고쳤다.
   - ffprobe 시간 초과는 다시 읽기 실패와 같게 본다(길이 정보 없이 저장, 지금 종료 코드가 0이 아닐 때와 같다).
   - 설정 `CLIP_TIMEOUT_FACTOR`·`CLIP_TIMEOUT_MIN_S`는 `config.py`·`.env.example`·Compose(워커)에 넣었다. 배수는 유한한 양수만
     받고(inf·nan이면 기동하지 않는다), 곱이 전체 한도 이상이면 올림 전에 전체 한도로 자른다(통합 검토 CLIPS-3·4 — 전에는 inf나
     1e308 같은 배수가 기동을 통과하고 클립 작업마다 OverflowError로 INTERNAL 재시도 뒤 FAILED가 됐다).
   - 버린 대안: 전체 한도만 낮추기 — 프록시·썸네일 같은 다른 단계와 긴 클립이 함께 걸린다. 원본 해상도·비트레이트로 한도 계산 —
     측정이 합성 영상뿐이라 근거가 약하고 설정이 늘어난다. 배수 하나를 운영 실측 뒤 맞춘다.
2. **전용 워커는 워커가 가져갈 작업 유형을 설정으로 나눠 만든다.** `WORKER_JOB_TYPES`(비우면 등록된 모든 유형) −
   `WORKER_EXCLUDE_JOB_TYPES`(`workers.main.claim_types`). 기본은 둘 다 비어 지금과 같다. 없는 유형 이름(오타)이나 빈 결과는 워커가
   기동하지 않는다 — 빈 목록을 큐에 넘기면 모든 유형을 가져가기 때문이다. Compose에 선택 서비스 `worker-clips`(프로필 `clip-worker`,
   기본 워커와 같은 환경 + `WORKER_JOB_TYPES=clip_export`)를 두었다. 켜는 법: `.env`에 `WORKER_EXCLUDE_JOB_TYPES=clip_export`와
   `COMPOSE_PROFILES=clip-worker`를 함께 두고 `make up`(runbook 07 5번). 프로필도 `.env`에 둔다 — 처음에는 프로필을 명령줄
   (`--profile clip-worker up -d worker-clips`)로만 켜게 적었는데, 그러면 제외 설정은 남고 프로필은 남지 않아 다음 `make up`
   (재부팅 뒤 기동 포함)에서 `worker-clips`만 빠지고 클립이 오류 없이 대기에 남았다(통합 검토 CLIPS-1).
   `make up`(`tools/compose_up.sh`)은 기본 워커가 `clip_export`를 가져가지 않는데 `worker-clips`가 꺼져 있으면 경고한다(기동은
   막지 않는다). 유형은 가져갈 때 나누므로 대기 중인 클립에도 바로 적용되고, 되돌리면(두 줄을 지우고 `worker`를 다시 띄운 뒤
   `worker-clips`를 멈춤) 그대로 기본 워커가 처리한다.
   - 버린 대안(검토 방향의 예시): 클립을 등록할 때 자원 클래스를 설정(예: `CLIP`)으로 정하고 `WORKER_RESOURCE_CLASSES`로 나누기 —
     (가) 지금 배정 조건이 CPU 이외 클래스를 걸러내지 않아 GPU 대체 규칙과 함께 배정 SQL의 의미를 바꿔야 한다, (나) 클래스가 등록 때
     작업 행에 찍혀 설정을 바꿔도 이미 대기 중인 클립은 옛 경로로 가고, 되돌리면 전용 클래스 작업을 아무도 가져가지 않는다,
     (다) 등록하는 API 쪽에도 설정이 필요하다, (라) 요구사항 1.1의 자원 클래스는 CPU/GPU 하드웨어 구분이다.
     작업 유형 나누기는 큐·마이그레이션·계약을 바꾸지 않는다.
   - 버린 대안: 클립 동시 실행 수 전역 한도 — 워커가 여럿일 때만 뜻이 있고, 한 작업의 비용은 시간 한도로, 기관당 몫은
     `CLIP_MAX_PENDING_PER_ORG`로 이미 묶인다. 필요하면 나중에.
3. **클립 작업이 도는 동안 워커가 임대를 다시 건다.** 처리기를 `register(..., renew_lease=True)`로 등록하면(지금은 `clip_export`만)
   워커가 처리기를 돌리는 동안 따로 도는 스레드가 임대의 1/3마다 `QueuePort.renew_lease(job_id, worker_id, lease_s)`로 임대를
   지금부터 정책 임대 시간만큼 다시 건다. 그 워커가 쥔 RUNNING 작업일 때만 바꾸고(회수된 뒤 다른 워커가 가져갔거나 끝났으면 False,
   경고 로그 `job_lease_lost`), `updated_at`은 두지 않는다(처리 시간 집계가 쓴다). `complete`·`fail` 전에 갱신을 멈춘다.
   워커 프로세스가 죽으면 갱신도 멈춰 임대 시간(2시간) 뒤 회수된다 — 요구사항 1.3 그대로다. 갱신하다 DB 오류가 나면 경고만 남기고
   다음 주기에 다시 한다(임대의 2/3가 남아 있다).
   - 버린 대안: 임대를 더 길게(예: 6시간) — 죽은 워커의 클립 복구가 그만큼 늦고, 원본 받기·올리기는 한도가 없어 여전히 넘을 수 있다.
     클립 작업 전체에 시간 한도 — 저장소 전송 시간은 원본 크기·저장 계층(WARM)에 달려 정할 근거가 없다. 모든 작업 유형에 갱신 —
     백업·계층 이동(임대 6시간) 같은 다른 유형의 동작이 바뀌어 이번 범위 밖이다(필요하면 같은 표시로 켠다). `complete`·`fail`에
     소유 확인 추가 — 두 번 실행은 막지 못하고 큐 시그니처가 바뀐다.
4. **우선순위는 바꾸지 않는다.** 대기 중인 integrity_verify·media_probe·make_proxy·make_thumbnail이 먼저 들어온 클립보다 먼저
   배정되는 것을 시험으로 고정했다.

## 결과

좋아지는 점: 30초 클립은 인코딩이 멈춰도 2분이면 워커를 놓는다(전에는 한 시간씩 세 번). 운영자가 클립 전용 워커를 띄우면 클립이
수집 처리를 막지 않는다(05번 요구사항 6.3). 임대보다 오래 걸리는 클립을 두 번 인코딩하지 않는다.

감수할 점:
- 운영 장비가 개발 PC보다 많이 느리거나 실제 영상이 무거우면 정상 클립이 `CLIP_TIMEOUT`으로 실패할 수 있다 — 배수를 실측에 맞춘다.
  900초 이상 구간은 인코딩과 다시 읽기가 각각 전체 한도(3600초)를 받아, 기본 워커 하나 배치에서는 긴 구간 하나가 최악에
  `FFMPEG_TIMEOUT_S`의 두 배(기본 2시간)에 원본 받기·올리기 시간을 더한 만큼 워커를 쥔다(통합 검토 CLIPS-5). 전용 워커를 켜야
  수집 처리와 나뉜다.
- 원본 받기·올리기에는 여전히 한도가 없다(ADR-0033 감수: 짧은 구간도 원본 전체를 받는다).
- 살아 있는 워커 안에서 멈춘 클립(예: 응답 없는 저장소)은 임대가 계속 갱신돼 다른 워커가 회수하지 않는다. 그 클립을 쥔 워커
  (전용 워커를 쓰면 `worker-clips`)를 다시 시작하면(runbook 07 2번) 갱신이 멈추고 임대 시간 뒤 회수된다.
- 전용 워커도 주기 정리·감시·웹훅 전송을 함께 돈다(여러 워커용 잠금으로 겹치지 않는다). 그래서 기본 워커와 같은 환경(알림 서명 비밀
  등)을 받는다. Prometheus 수집 대상(`deploy/compose/prometheus/prometheus.yml`)에는 넣지 않았다.
  잠금은 겹침만 막고 두 워커의 환경을 맞추지는 않는다 — 정기 백업(`BACKUP_*`)·보존 자동 실행(`RETENTION_AUTO_EXECUTE`)·계층
  이동(`TIERING_ENABLED`)·외부 수집은 먼저 도는 워커가 자기 컨테이너를 만들 때의 환경으로 정한다. 예를 들어 `.env`에서 보존
  자동 실행을 끄고 `worker`만 다시 만들면 옛 환경의 `worker-clips`가 그날 자동 삭제를 할 수 있다. 그래서 전용 워커를 쓰는 동안
  `.env`를 바꾸거나 워커를 다시 시작하는 절차는 두 워커를 함께 다룬다(runbook 03·06·07에 적었다, 통합 검토 CLIPS-2). 전용 워커가
  주기 작업을 건너뛰게 하는 설정은 이번에 두지 않았다 — 멈춘 클립 풀기·클립 설정 반영에는 어차피 두 워커를 함께 다뤄야 하고,
  기본 워커만 멈춘 동안(runbook 01 `stop worker`)에는 임대 회수·정비를 하는 워커가 없어진다.
- 기본 워커에서 `clip_export`를 빼고 전용 워커를 띄우지 않으면 클립이 대기에 남는다(runbook에 적었고 `make up`이 경고한다).

남은 것(사람 결정): `sensor_parse`(07번)·`tag_scenes`·`env_join`·`deidentify`(14번)·`collect_source`(12번)는 정책 행이 없어 기본
우선순위 0으로 클립(70)보다 뒤에 배정된다. 0005의 `deid`(90, GPU→CPU)·`tag`(60, GPU 대기) 행은 실제 작업 유형 이름
(`deidentify`·`tag_scenes`)과 달라 쓰이지 않는다. 고치려면 마이그레이션이 필요하고(정책 편집 API는 있는 행만 바꾼다), 이름을 맞추면
`tag_scenes`가 GPU 없는 배치에서 대기로 바뀌는 등 동작이 달라져 우선순위·자원 클래스 값을 정해야 한다.

되돌리는 방법: `clips.make_clip`의 한도를 `s.ffmpeg_timeout_s`로 되돌리고 시간 초과 처리를 지운다. `register("clip_export", ...)`의
`renew_lease=True`를 지우면 갱신하지 않는다(갱신 코드는 남아도 쓰이지 않는다). 작업 유형 설정은 비워 두면 지금과 같다.
마이그레이션 없음. 계약은 설명 두 곳(되돌리면 웹 타입을 다시 만든다).

## 시험

`tests/integration/test_clip_job_bounds.py` 7개.
- `test_clip_ffmpeg_limit_follows_clip_length`: 1·2.5·4초 클립의 ffmpeg·ffprobe 한도가 12·25·35초(최솟값 12·배수 10·전체 한도 35).
- `test_clip_timeout_kills_ffmpeg_and_fails_without_retry`: 20초 자는 가짜 ffmpeg가 1초 한도에서 끝나고(프로세스 없음) 작업은
  FAILED(`CLIP_TIMEOUT`, 시도 1번), 목록에 원인 코드, 클립 자산·임시 파일 없음.
- `test_clip_probe_timeout_keeps_clip_without_video_ext`: 20초 자는 가짜 ffprobe가 4초 한도에서 끝나고(프로세스 없음) 작업은
  DONE(시도 1번, 원인 코드 없음), 클립 자산은 `video_ext` 없이 저장된다.
- `test_worker_refuses_to_start_with_bad_job_types`: `WORKER_JOB_TYPES` 오타(`clip_exprot`)·빈 결과(포함 = 제외)면 `main()`이
  컨테이너를 만들기 전에 `SystemExit`("워커 설정 오류: …")로 멈춘다.
- `test_dedicated_clip_worker_split`: 기본 워커는 클립을 남기고 다른 작업을 하고, 전용 워커는 클립만 한다. 오타·빈 결과는 ValueError,
  Compose 모양의 환경변수(빈 값·쉼표)를 읽는다.
- `test_long_clip_job_keeps_its_lease`: 임대 3초 정책에서 5.5초 걸리는 클립이 도는 동안 다른 워커의 회수·배정이 0회이고, 끝난 작업에
  임대가 남지 않는다. 회수 뒤 옛 워커는 임대를 늘리지 못한다.
- `test_ingestion_jobs_are_claimed_before_queued_clips`: 지금 순서를 고정하는 시험이라 수정 전에도 통과한다.

수정 전 코드에서 처음 만든 네 시험(한도·ffmpeg 시간 초과·전용 워커·임대)이 실패하는 것을 확인했다 — 한도 `[35, 35, 35]`,
가짜 ffmpeg를 20초 기다린 뒤 실패,
기본 워커가 클립을 먼저 가져감(`'QUEUED' == 'DONE'`), 회수 1회·다른 워커가 가져감(`(1, ['job_…'], 1) == (0, [], 1)`).
검토 뒤 더한 두 시험도 수정 전 코드에서 실패한다 — 가짜 ffprobe를 20초 기다림(`< 15` 실패), 잘못된 설정으로 컨테이너를 만듦.
또 이 변경에서 해당 처리만 지우면 실패하는 것을 확인했다 — ffprobe 시간 초과를 잡지 않으면 `('QUEUED', 1, 'INTERNAL')`(재시도),
`main()`의 작업 유형 확인을 지우면 컨테이너를 만든다.

통합 검토(2026-11-20) 뒤 더한 시험 — 모두 수정 전 코드·문서에서 실패하는 것을 확인했다.
- `tests/unit/test_clip_timeout_factor.py`(CLIPS-3·4): `CLIP_TIMEOUT_FACTOR`가 inf·nan이면 `ConfigError(CLIP_TIMEOUT_FACTOR)`로
  기동하지 않고, 1e308처럼 곱이 넘치는 배수는 전체 한도(3600초)를 받는다. 수정 전: `DID NOT RAISE ConfigError`,
  `OverflowError: cannot convert float infinity to integer`.
- `tools/tests/test_compose_up.py`(CLIPS-1): 기본 워커가 `clip_export`를 가져가지 않는데 `worker-clips`가 활성 서비스에 없으면
  `make up`이 경고한다(가짜 docker). 수정 전: `'worker-clips' not found in ''`.
- `tests/unit/test_clip_worker_ops_docs.py`(CLIPS-1·2): 켜는 법을 적은 곳이 `COMPOSE_PROFILES=clip-worker`도 `.env`에 두라고
  하는지, `worker`를 restart·up 하는 runbook 단계가 같은 단계에서 `worker-clips`를 말하는지. 수정 전: 네 곳·네 단계가 빠짐.
- CLIPS-5(최악 시간 문장)는 문장만 고쳤다. ffprobe가 같은 한도를 받는 것은 `test_clip_ffmpeg_limit_follows_clip_length`가 고정한다.
