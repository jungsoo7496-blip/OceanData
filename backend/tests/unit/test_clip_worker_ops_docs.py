"""통합 검토 CLIPS-1·CLIPS-2: 클립 전용 워커(worker-clips, ADR-0069)를 켜는 법과 운영 절차가 다시 시작 뒤에도 맞는지.

- CLIPS-1: 기본 워커에서 clip_export를 빼는 설정(`.env`의 WORKER_EXCLUDE_JOB_TYPES)은 남는데 프로필은 명령줄에만 있어,
  `make up`·재부팅 뒤 worker-clips가 오르지 않고 클립이 오류 없이 대기(QUEUED)에 남았다. 켜는 법을 적은 곳은 프로필도
  `.env`(COMPOSE_PROFILES)에 함께 두라고 해야 한다.
- CLIPS-2: worker-clips도 정비 주기(정기 백업·보존 자동 실행·계층 이동)를 자기 환경으로 돌고 클립은 거기서 돈다.
  runbook에서 `worker`를 다시 시작하거나 다시 만드는 단계는 worker-clips도 함께 말해야 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUNBOOKS = sorted((REPO / "docs" / "runbook").glob("[0-9][0-9]-*.md"))
ENABLE_DOCS = [REPO / ".env.example", REPO / "deploy" / "compose" / "docker-compose.yml",
               REPO / "docs" / "adr" / "0069-clip-job-bounds.md", *RUNBOOKS]
# `worker`를 다시 시작하거나 다시 만드는 Compose 명령 (worker-clips만 말하는 명령은 제외)
RESTARTS_WORKER = re.compile(r"\b(?:restart|up)\b[^`]*(?<![\w-])worker(?![\w-])")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _steps(text: str) -> list[str]:
    """번호 단계(`1. …`와 들여 쓴 이어지는 줄)를 한 덩어리로, 나머지 줄은 한 줄씩."""
    blocks: list[str] = []
    in_step = False
    for line in text.splitlines():
        if re.match(r"\d+\. ", line):
            blocks.append(line)
            in_step = True
        elif in_step and line.startswith(" ") and line.strip():
            blocks[-1] += "\n" + line
        else:
            blocks.append(line)
            in_step = False
    return blocks


def test_clip_worker_profile_is_kept_in_env_file_next_to_the_exclusion() -> None:
    compose = _read(REPO / "deploy" / "compose" / "docker-compose.yml")
    assert re.search(r"^  worker-clips:\n    profiles: \[clip-worker\]$", compose, re.M)
    told = [p for p in ENABLE_DOCS if "WORKER_EXCLUDE_JOB_TYPES=clip_export" in _read(p)]
    assert len(told) >= 4  # .env.example·Compose 주석·ADR-0069·runbook 07
    missing = [str(p.relative_to(REPO)) for p in told if "COMPOSE_PROFILES=clip-worker" not in _read(p)]
    assert missing == []


def test_runbook_steps_that_restart_worker_also_name_worker_clips() -> None:
    found, missing = 0, []
    for path in RUNBOOKS:
        for block in _steps(_read(path)):
            if any(RESTARTS_WORKER.search(code) for code in re.findall(r"`([^`]+)`", block)):
                found += 1
                if "worker-clips" not in block:
                    missing.append(f"{path.name}: {block.splitlines()[0][:60]}")
    assert found >= 5  # 03 5번·06 2번·07 2번·07 5번·11 2번
    assert missing == []
