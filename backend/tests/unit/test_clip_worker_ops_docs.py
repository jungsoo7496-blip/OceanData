"""통합 검토 CLIPS-1: 클립 전용 워커(worker-clips, ADR-0069)를 켜는 법이 다시 시작 뒤에도 맞는지.

- CLIPS-1: 기본 워커에서 clip_export를 빼는 설정(`.env`의 WORKER_EXCLUDE_JOB_TYPES)은 남는데 프로필은 명령줄에만 있어,
  `make up`·재부팅 뒤 worker-clips가 오르지 않고 클립이 오류 없이 대기(QUEUED)에 남았다. 켜는 법을 적은 곳은 프로필도
  `.env`(COMPOSE_PROFILES)에 함께 두라고 해야 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUNBOOKS = sorted((REPO / "docs" / "runbook").glob("[0-9][0-9]-*.md"))
ENABLE_DOCS = [REPO / ".env.example", REPO / "deploy" / "compose" / "docker-compose.yml",
               REPO / "docs" / "adr" / "0069-clip-job-bounds.md", *RUNBOOKS]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_clip_worker_profile_is_kept_in_env_file_next_to_the_exclusion() -> None:
    compose = _read(REPO / "deploy" / "compose" / "docker-compose.yml")
    assert re.search(r"^  worker-clips:\n    profiles: \[clip-worker\]$", compose, re.M)
    told = [p for p in ENABLE_DOCS if "WORKER_EXCLUDE_JOB_TYPES=clip_export" in _read(p)]
    assert len(told) >= 4  # .env.example·Compose 주석·ADR-0069·runbook 07
    missing = [str(p.relative_to(REPO)) for p in told if "COMPOSE_PROFILES=clip-worker" not in _read(p)]
    assert missing == []
