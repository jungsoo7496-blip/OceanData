"""설정 로더 (01번 요구사항 2). 모든 하드웨어 의존 값은 환경변수로 받는다 (tech.md HW-1~HW-5)."""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", case_sensitive=False)

    # DB
    database_url: SecretStr

    # 오브젝트 스토리지 (S3 공통 기능만 쓴다)
    s3_endpoint_url: str
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr
    s3_secret_access_key: SecretStr
    s3_force_path_style: bool = True
    bucket_raw: str = "raw"
    bucket_proc: str = "proc"
    bucket_evidence: str = "evidence"
    bucket_datasets: str = "datasets"
    bucket_quarantine: str = "quarantine"

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
    proxy_short_side: int = 480
    proxy_max_bitrate: str = "1M"
    proxy_hw_encoder: str = "h264_nvenc"
    thumbnail_interval_s: float = 10.0
    thumbnail_max_frames: int = 20
    worker_resource_classes: CsvList = Field(default_factory=lambda: ["CPU"])
    job_lease_reap_interval_s: int = 60
    integrity_sample_size: int = 20

    # 악성코드 검사 (04번 요구사항 4.7, ADR-0031). 기관 정책(organizations.malware_scan_required)이 켜진 기관만 검사
    malware_scanner: str = "none"  # none | mock-eicar (제품 결정 후 어댑터 추가)

    # 저장·보존 (08번)
    backup_retention_days: int = 90
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
    webhook_timeout_s: float = 5.0
    webhook_dispatch_interval_s: int = 10
    webhook_allow_private_targets: bool = False  # 개발용. 운영에서는 사설·루프백 주소로 보내지 않는다 (SSRF)
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

    @property
    def buckets(self) -> dict[str, str]:
        return {
            "RAW": self.bucket_raw,
            "PROCESSED": self.bucket_proc,
            "EVIDENCE": self.bucket_evidence,
            "DATASET": self.bucket_datasets,
            "QUARANTINE": self.bucket_quarantine,
        }


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
    out = set()
    for err in errors:
        is_missing = err.get("type") == "missing"
        if (kind == "missing") == is_missing:
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
