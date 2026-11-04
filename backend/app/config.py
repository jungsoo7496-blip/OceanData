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

    # 센서·관측값 (07번, 정책값)
    observation_batch_max: int = 5000
    observation_future_tolerance_s: int = 600
    clock_offset_warn_ms: int = 2000
    sensor_file_max_bytes: int = 512 * 1024 * 1024

    # 운영
    log_level: str = "INFO"
    readiness_timeout_s: float = 2.0
    worker_poll_interval_s: float = 1.0
    worker_metrics_port: int = 9101
    idempotency_retention_s: int = 86400

    @field_validator("processing_steps", "report_formats", "external_sources", "idp_mfa_acr_values",
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
