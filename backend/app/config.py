"""설정 로더 (01번 요구사항 2). 모든 하드웨어 의존 값은 환경변수로 받는다 (tech.md HW-1~HW-5)."""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", case_sensitive=False)

    # DB
    database_url: SecretStr
    # 연결 풀과, 동시에 DB 작업을 하는 요청 수 (ADR-0062). 요청이 DB 작업에 들어가기 전에 비동기로 차례를 기다려
    # 연결 풀과 스레드 풀이 서로를 기다리지 않게 한다. 슬롯을 비우면 (풀 + 초과) // 2 — 요청 하나가 자기 세션과
    # 감사 기록 등 별도 연결을 함께 쥘 수 있다. 차례를 slot_wait_s 안에 못 받으면 503 SERVICE_UNAVAILABLE
    db_pool_size: int = Field(10, ge=1)
    db_max_overflow: int = Field(20, ge=0)
    db_pool_timeout_s: float = Field(30.0, gt=0)
    db_request_slots: int | None = Field(None, ge=1)
    db_request_slot_wait_s: float = Field(10.0, gt=0)

    # 오브젝트 스토리지 (S3 공통 기능만 쓴다)
    s3_endpoint_url: str
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr
    s3_secret_access_key: SecretStr
    s3_force_path_style: bool = True
    # 서버 측 암호화 (08번 요구사항 1.3·1.4): none | AES256(SSE-S3, 버킷 기본 암호화로 요청). 지원 여부는 기동 때 확인
    s3_sse: str = "none"
    bucket_raw: str = "raw"
    bucket_proc: str = "proc"
    bucket_evidence: str = "evidence"
    bucket_datasets: str = "datasets"
    bucket_quarantine: str = "quarantine"

    # Hot/Warm 계층 (08번 요구사항 9, ADR-0036). HOT은 위 S3_*·BUCKET_* 그대로다.
    # WARM 주소·자격증명을 비우면 HOT과 같은 저장소(같은 S3 연결)를 쓰고 버킷만 다르다. 격리 존은 HOT에만 있다.
    # 실제 WARM 장비·제품은 하드웨어 확정 후 — 확인 필요 (tech.md 결정 대기)
    tiering_enabled: bool = False  # Tier_Mover 자동 이동 (정책 표 tier_policies의 규칙도 enabled여야 옮긴다)
    warm_s3_endpoint_url: str | None = None
    # 사전 서명 주소 (비우면 WARM 주소, 저장소를 같이 쓰면 HOT 공개 주소)
    warm_s3_public_endpoint_url: str | None = None
    warm_s3_region: str | None = None
    warm_s3_access_key_id: SecretStr | None = None
    warm_s3_secret_access_key: SecretStr | None = None
    warm_s3_force_path_style: bool | None = None
    warm_bucket_raw: str | None = None  # 비우면 <BUCKET_RAW>-warm
    warm_bucket_proc: str | None = None
    warm_bucket_evidence: str | None = None
    warm_bucket_datasets: str | None = None
    # 원래 객체는 위치를 바꾼 뒤 이만큼 지나서 지운다. 실제 유예 = max(이 값, PRESIGN_DOWNLOAD_TTL_S) (요구사항 9.3)
    tier_delete_grace_s: int = 3600
    tier_max_in_flight: int = 2  # 동시에 복사 중인 이동 수 (처리량은 가정하지 않고 메트릭으로 보며 조정, HW-8)
    tier_failed_retry_after_s: int = 86400  # 실패한 이동을 정책이 다시 고르기까지 (원본 해시 불일치는 다시 고르지 않음)

    # IdP (Keycloak OIDC)
    idp_issuer_url: str
    idp_internal_url: str | None = Field(
        default=None, description="컨테이너 안에서 IdP에 접근할 주소 (발급자 주소와 다를 때)"
    )
    idp_audience: str = "marine-api"
    idp_mfa_acr_values: CsvList = Field(default_factory=lambda: ["mfa", "2"])

    # 기기·API 키 단기 토큰 (자체 발급 JWT, tech.md 결정 대기)
    device_token_signing_key: SecretStr
    device_token_ttl_s: int = 300
    enrollment_code_ttl_s: int = 86400
    break_glass_single_approval: bool = False

    # 처리·기능
    gpu_enabled: bool = False
    processing_steps: CsvList = Field(default_factory=lambda: ["META"])
    report_formats: CsvList = Field(default_factory=list)
    external_sources: CsvList = Field(default_factory=list)

    # 사전 서명 URL 유효시간 (security.md). URL 호스트는 클라이언트가 접근하는 주소여야 한다
    s3_public_endpoint_url: str | None = None
    presign_upload_ttl_s: int = 900
    presign_download_ttl_s: int = 300

    # 업로드·쿼터·보존 (HW-3: 수치는 설정값)
    upload_part_size: int = 64 * 1024 * 1024
    upload_session_ttl_s: int = 7 * 86400
    max_part_urls_per_response: int = 1000
    default_org_quota_bytes: int = 10 * 1024**4
    default_retention_days: int = 3650
    quarantine_retention_days: int = 30
    rate_limit_per_minute: int = 600

    # 처리 파이프라인 (05번). FFmpeg는 별도 프로세스, 하드웨어 인코더는 GPU_ENABLED일 때만
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    ffmpeg_timeout_s: int = 3600
    # 처리 작업 임시 디스크 (05번 작업 13): 받아 둘 원본 크기 × 배수 + 최소 여유가 없으면 작업을 미룬다
    work_dir: str | None = None  # 비우면 시스템 임시 폴더
    work_min_free_bytes: int = 2 * 1024**3
    work_size_factor: float = 2.5  # 원본 + 프록시·썸네일·비식별 출력
    work_stale_after_s: int = 6 * 3600  # 이보다 오래된 marine-* 임시 파일은 비정상 종료 잔여물로 보고 지운다
    proxy_short_side: int = 480
    proxy_max_bitrate: str = "1M"
    proxy_hw_encoder: str = "h264_nvenc"
    thumbnail_interval_s: float = 10.0
    thumbnail_max_frames: int = 20
    # 구간 클립 내보내기 (09번 요구사항 7.2): 프레임 단위로 정확히 자르려고 libx264로 다시 인코딩한다
    clip_max_s: float = 3600.0
    clip_crf: int = 18  # 낮을수록 고화질·큰 파일 (18 ≈ 눈으로 구별하기 어려운 수준)
    # 기관별 대기·실행 중 클립 작업 한도 — 넘으면 429 (긴 재인코딩으로 한 기관이 공용 작업 큐를 채우지 않게)
    clip_max_pending_per_org: int = Field(default=3, ge=1)
    worker_resource_classes: CsvList = Field(default_factory=lambda: ["CPU"])
    job_lease_reap_interval_s: int = 60
    integrity_sample_size: int = 20

    # 날씨 태그 관측값 보정 (14번 요구사항 6.3). 값은 정책 — 운영기관 확정 필요.
    # 강수 관측 항목이 없어 RAIN은 보정하지 않는다
    weather_fog_visibility_m: float = 1000.0  # 시정이 이보다 낮으면 FOG
    weather_clear_visibility_m: float = 5000.0  # 이보다 높으면 영상 모델의 FOG를 지운다
    weather_high_wave_m: float = 3.0  # 유의파고가 이 이상이면 HIGH_WAVE
    weather_calm_wave_m: float = 1.0  # 이보다 낮으면 영상 모델의 HIGH_WAVE를 지운다

    # 악성코드 검사 (04번 요구사항 4.7, ADR-0031). 기관 정책(organizations.malware_scan_required)이 켜진 기관만 검사
    malware_scanner: str = "none"  # none | mock-eicar (제품 결정 후 어댑터 추가)

    # 저장·보존 (08번)
    backup_retention_days: int = 90
    dataset_sync_max_items: int = 200  # 이보다 많은 항목의 데이터셋 버전은 작업 큐에서 만든다 (13번 작업 32)
    # 용량 예측 (08번 요구사항 8). 전체 용량은 하드웨어 확정 후 — 비우면 예측일 없이 '확인 필요'
    storage_capacity_bytes: int | None = None
    capacity_threshold_ratio: float = 0.7
    capacity_lead_time_days: int = 180
    capacity_window_days: int = 30
    # 계층형 백업 (08번 요구사항 6·7, ADR-0021). 대상 S3를 비우면 같은 S3의 backup_bucket (개발용)
    backup_enabled: bool = False  # 워커가 backup_interval_h마다 백업. 켜기 전에 pg_dump 주버전·대상 S3 확인
    backup_interval_h: int = 24
    backup_bucket: str = "backup"
    backup_s3_endpoint_url: str | None = None
    backup_s3_access_key_id: SecretStr | None = None
    backup_s3_secret_access_key: SecretStr | None = None
    backup_database_url: SecretStr | None = None  # pg_dump·복원 시험용 (소유자 계정). 비우면 DATABASE_URL
    pg_dump_bin: str = "pg_dump"
    pg_restore_bin: str = "pg_restore"
    retention_auto_execute: bool = False  # 기본은 운영자가 dry-run을 보고 실행 (요구사항 3.6)

    # 센서·관측값 (07번, 정책값)
    observation_batch_max: int = 5000
    observation_future_tolerance_s: int = 600
    clock_offset_warn_ms: int = 2000
    sensor_file_max_bytes: int = 512 * 1024 * 1024

    # 비식별·태깅 (14번). 탐지기: synthetic(합성 시험용 모의) 또는 onnx:<모델>@<버전> (Model_Registry 등록 후)
    deid_detector: str = "synthetic"
    deid_classes: CsvList = Field(default_factory=lambda: ["FACE", "LICENSE_PLATE", "VESSEL_ID_TEXT", "SCREEN_TEXT"])
    deid_method: str = "BLUR"  # BLUR / MOSAIC / FILL
    deid_tracking: bool = True
    deid_track_gap_s: float = 1.0
    deid_sample_fps: float = 2.0
    tag_sample_fps: float = 1.0
    embed_interval_s: float = 2.0
    night_luma_threshold: float = 50.0

    # 환경 정보 결합 (12번)
    env_join_radius_km: float = 30.0
    env_join_window_min: int = 30

    # 공유·배포 (13번)
    access_decision_days: int = 14  # 이용 신청 응답 기한, 지나면 거부 (요구사항 8.3)

    # 수집 모니터링·알림 (11번). 메일 서버가 없으면 알림은 로그로만 남는다
    smtp_host: str | None = None
    smtp_port: int = 25
    mail_from: str = "marine-platform@localhost"
    # 기관 웹훅 (04번 요구사항 8, ADR-0030). 비밀은 이 키 + 구독 salt로 만든다
    # (없으면 DEVICE_TOKEN_SIGNING_KEY에서 용도를 나눠 파생)
    webhook_secret_key: SecretStr | None = None
    webhook_max_attempts: int = 8  # 이만큼 실패하면 구독을 끄고 기관에 알린다
    webhook_backoff_base_s: int = 30
    webhook_backoff_max_s: int = 6 * 3600
    # 한 건 전체 기한: 이름 풀기·연결·TLS·요청·응답 헤더까지. 본문은 읽지 않는다 (ADR-0041)
    webhook_timeout_s: float = Field(default=5.0, gt=0)
    webhook_dispatch_interval_s: int = Field(default=10, ge=1)  # 보낼 것이 없을 때 다시 볼 간격 (전용 스레드)
    # 한 라운드: 구독당 1건, 기관당 WEBHOOK_MAX_PER_ORG건, 모두 WEBHOOK_BATCH_SIZE건까지 동시에 보낸다 (ADR-0041)
    webhook_batch_size: int = Field(default=20, ge=1)
    webhook_max_per_org: int = Field(default=4, ge=1)
    webhook_max_subscriptions_per_org: int = Field(default=10, ge=1)  # 지우지 않은 구독 수 (넘으면 409)
    webhook_allow_private_targets: bool = False  # 개발용. 운영에서는 공인(global) 주소로만 보낸다 (SSRF)
    alert_webhook_url: str | None = None  # 운영자 알림 웹훅 (메신저 연동은 결정 대기)
    alert_webhook_secret: SecretStr | None = None  # 있으면 X-Signature(HMAC-SHA256)를 붙인다
    watch_eval_interval_s: int = 60
    ops_timezone: str = "Asia/Seoul"  # 정기 점검 기한 계산 기준

    # 운영
    log_level: str = "INFO"
    readiness_timeout_s: float = 2.0
    worker_poll_interval_s: float = 1.0
    worker_metrics_port: int = 9101
    idempotency_retention_s: int = 86400

    @field_validator("processing_steps", "report_formats", "external_sources", "idp_mfa_acr_values", "deid_classes",
                     "worker_resource_classes",
                     mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("db_request_slots", "storage_capacity_bytes", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # .env.example·Compose는 선택 숫자를 빈 값(`KEY=`)으로 둔다 — 빈 값은 '설정 안 함'이다
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _warm_not_hot(self) -> Settings:
        """같은 저장소에서 WARM 버킷이 HOT 버킷과 같으면 이동이 하나뿐인 사본을 지운다 — 기동을 막는다."""
        if (self.warm_s3_endpoint_url or self.s3_endpoint_url) == self.s3_endpoint_url:
            hot = set(self.buckets.values())
            clash = [f"WARM_BUCKET_{name}" for name, zone in (("RAW", "RAW"), ("PROC", "PROCESSED"),
                                                               ("EVIDENCE", "EVIDENCE"), ("DATASETS", "DATASET"))
                     if self.tier_buckets("WARM")[zone] in hot]
            if clash:
                raise SettingConflict(*clash)
        return self

    @property
    def buckets(self) -> dict[str, str]:
        """HOT 계층의 존별 버킷 (domain.md 9장). 새 객체는 모두 여기에 쓴다."""
        return {
            "RAW": self.bucket_raw,
            "PROCESSED": self.bucket_proc,
            "EVIDENCE": self.bucket_evidence,
            "DATASET": self.bucket_datasets,
            "QUARANTINE": self.bucket_quarantine,
        }

    def tier_buckets(self, tier: str) -> dict[str, str]:
        """계층의 존별 버킷. WARM에는 격리 존이 없다 (격리 객체는 옮기지 않는다)."""
        if tier == "HOT":
            return self.buckets
        if tier == "WARM":
            return {"RAW": self.warm_bucket_raw or f"{self.bucket_raw}-warm",
                    "PROCESSED": self.warm_bucket_proc or f"{self.bucket_proc}-warm",
                    "EVIDENCE": self.warm_bucket_evidence or f"{self.bucket_evidence}-warm",
                    "DATASET": self.warm_bucket_datasets or f"{self.bucket_datasets}-warm"}
        raise ValueError(f"알 수 없는 계층: {tier}")

    @property
    def warm_shares_hot_storage(self) -> bool:
        """WARM이 HOT과 같은 S3 연결을 쓰는가 (주소·자격증명·지역·주소 방식을 따로 주지 않았다)."""
        # 빈 값(Compose가 넘기는 "")은 지정하지 않은 것으로 본다
        return (self.warm_s3_endpoint_url in (None, "", self.s3_endpoint_url) and not self.warm_s3_access_key_id
                and not self.warm_s3_secret_access_key and self.warm_s3_region in (None, "", self.s3_region)
                and self.warm_s3_force_path_style in (None, self.s3_force_path_style)
                and self.warm_s3_public_endpoint_url in (None, "", self.s3_public_endpoint_url))

    def warm_s3_settings(self) -> Settings:
        """WARM 저장소 연결용 설정 사본 (S3Storage가 읽는 S3_* 값만 바꾼다)."""
        update: dict[str, object] = {
            "s3_endpoint_url": self.warm_s3_endpoint_url or self.s3_endpoint_url,
            "s3_public_endpoint_url": self.warm_s3_public_endpoint_url or (
                None if self.warm_s3_endpoint_url else self.s3_public_endpoint_url),
            "s3_region": self.warm_s3_region or self.s3_region,
            "s3_force_path_style": (self.s3_force_path_style if self.warm_s3_force_path_style is None
                                    else self.warm_s3_force_path_style)}
        if self.warm_s3_access_key_id:
            update["s3_access_key_id"] = self.warm_s3_access_key_id
        if self.warm_s3_secret_access_key:
            update["s3_secret_access_key"] = self.warm_s3_secret_access_key
        return self.model_copy(update=update)


class SettingConflict(ValueError):
    """설정 값끼리 맞지 않는다. 키 이름만 담는다 (값은 담지 않는다)."""

    def __init__(self, *keys: str) -> None:
        super().__init__("설정 값이 서로 맞지 않습니다: " + ", ".join(keys))
        self.keys = keys


class ConfigError(Exception):
    def __init__(self, missing: list[str], invalid: list[str]) -> None:
        self.missing = missing
        self.invalid = invalid
        super().__init__(self.message)

    @property
    def message(self) -> str:
        parts = []
        if self.missing:
            parts.append("필수 설정 누락: " + ", ".join(self.missing))
        if self.invalid:
            parts.append("설정 형식 오류: " + ", ".join(self.invalid))
        return " / ".join(parts)


def _keys(errors: list, kind: str | None) -> list[str]:
    out: set[str] = set()
    for err in errors:
        is_missing = err.get("type") == "missing"
        if (kind == "missing") == is_missing:
            conflict = (err.get("ctx") or {}).get("error")
            if not err.get("loc") and isinstance(conflict, SettingConflict):  # 설정 사이 충돌: 키 이름만
                out.update(conflict.keys)
                continue
            loc = err.get("loc") or ("?",)
            out.add(str(loc[0]).upper())
    return sorted(out)


def build_settings(**overrides: object) -> Settings:
    """설정을 읽는다. 실패하면 키 이름만 담은 ConfigError (값은 절대 담지 않는다)."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        errs = exc.errors(include_input=False, include_url=False)
        raise ConfigError(_keys(errs, "missing"), _keys(errs, None)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return build_settings()
    except ConfigError as exc:
        print(exc.message, file=sys.stderr)
        raise SystemExit(2) from None
