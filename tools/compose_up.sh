#!/usr/bin/env bash
# make up: 개발 환경 기동. 제한 시간 안에 헬스체크를 통과하지 못한 서비스 이름을 출력하고 실패한다 (01번 요구사항 1.3).
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env가 없습니다. cp .env.example .env 후 값을 바꾸세요." >&2; exit 2; }
COMPOSE=(docker compose --env-file .env -f deploy/compose/docker-compose.yml)
TIMEOUT="${UP_TIMEOUT_S:-420}"
if "${COMPOSE[@]}" up -d --build --wait --wait-timeout "$TIMEOUT"; then
  echo "개발 환경 준비됨: API http://localhost:8000/docs · 웹 http://localhost:5173 · Keycloak http://localhost:8080"
  exit 0
fi
echo "헬스체크를 통과하지 못한 서비스:" >&2
"${COMPOSE[@]}" ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' | \
  awk -F'|' '($2!="running" && !($1=="migrate" && $4=="0")) || ($3!="" && $3!="healthy") {print "  - " $1 " (" $2 " " $3 ")"}' >&2
exit 1
