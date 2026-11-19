#!/usr/bin/env bash
# make up: 개발 환경 기동. 제한 시간 안에 헬스체크를 통과하지 못한 서비스 이름을 출력하고 실패한다 (01번 요구사항 1.3).
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env가 없습니다. cp .env.example .env 후 값을 바꾸세요." >&2; exit 2; }
COMPOSE=(docker compose --env-file .env -f deploy/compose/docker-compose.yml)
TIMEOUT="${UP_TIMEOUT_S:-420}"

# 클립 전용 워커 (ADR-0069, 통합 검토 CLIPS-1): 기본 워커가 clip_export를 가져가지 않는데 worker-clips(프로필 clip-worker)가
# 꺼져 있으면 클립이 오류 없이 대기(QUEUED)에 남는다. 프로필을 명령줄로만 켜면 make up·재부팅 뒤 이렇게 된다 — 경고만 한다
clip_worker_warning() {
  local cfg jt ex services
  cfg=$("${COMPOSE[@]}" config worker 2>/dev/null) || return 0
  jt=$(sed -n 's/^ *WORKER_JOB_TYPES: *//p' <<<"$cfg" | head -n 1 | tr -d "\"' ")
  ex=$(sed -n 's/^ *WORKER_EXCLUDE_JOB_TYPES: *//p' <<<"$cfg" | head -n 1 | tr -d "\"' ")
  if [[ ",$ex," != *,clip_export,* ]] && { [ -z "$jt" ] || [[ ",$jt," == *,clip_export,* ]]; }; then
    return 0  # 기본 워커가 클립도 가져간다
  fi
  services=$("${COMPOSE[@]}" config --services 2>/dev/null) || return 0
  grep -qx 'worker-clips' <<<"$services" && return 0
  echo "경고: 기본 워커(worker)가 구간 클립(clip_export)을 가져가지 않는데 클립 전용 워커(worker-clips)가 꺼져 있습니다." >&2
  echo "      클립 요청이 대기(QUEUED)에 남습니다. .env에 COMPOSE_PROFILES=clip-worker를 두고 make up 하거나," >&2
  echo "      전용 워커를 쓰지 않으려면 WORKER_EXCLUDE_JOB_TYPES에서 clip_export를 지우세요 (runbook 07 5번)." >&2
}

if "${COMPOSE[@]}" up -d --build --wait --wait-timeout "$TIMEOUT"; then
  echo "개발 환경 준비됨: API http://localhost:8000/docs · 웹 http://localhost:5173 · Keycloak http://localhost:8080"
  clip_worker_warning
  exit 0
fi
echo "헬스체크를 통과하지 못한 서비스:" >&2
"${COMPOSE[@]}" ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' | \
  awk -F'|' '($2!="running" && !($1=="migrate" && $4=="0")) || ($3!="" && $3!="healthy") {print "  - " $1 " (" $2 " " $3 ")"}' >&2
clip_worker_warning
exit 1
