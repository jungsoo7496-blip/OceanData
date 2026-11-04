"""contracts/ 읽기 (단일 기준). 스키마 버전 목록 등."""

from __future__ import annotations

import os
from pathlib import Path


def contracts_dir() -> Path:
    env = os.environ.get("CONTRACTS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "contracts"
        if (cand / "openapi.yaml").exists():
            return cand
    raise FileNotFoundError("contracts 디렉터리를 찾을 수 없습니다 (CONTRACTS_DIR 설정)")


# 서버가 아는 공통 메타데이터 스키마 버전 (domain.md 8장 v1.0). 새 부버전을 받으면 여기에 더한다.
SUPPORTED_SCHEMA_VERSIONS = ("1.0.0",)


def supported_schema_versions() -> list[str]:
    return list(SUPPORTED_SCHEMA_VERSIONS)
