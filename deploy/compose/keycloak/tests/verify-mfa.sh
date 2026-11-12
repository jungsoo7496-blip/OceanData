#!/usr/bin/env bash
# 02번 요구사항 4.1 확인 (ADR-0034): realm-marine.json을 일회용 Keycloak 컨테이너에 올리고 실제 브라우저로
# 로그인해 ORG_ADMIN·PLATFORM_OPERATOR만 OTP를 거치는지, 토큰 acr/amr을 API가 MFA로 판정하는지 본다.
#
#   deploy/compose/keycloak/tests/verify-mfa.sh
#
# 필요: Docker, Node 18+, playwright(-core)와 chromium (web에서 npm ci 후 node node_modules/playwright-core/cli.js install chromium).
# API 판정 확인은 backend/.venv가 있으면 함께 돌린다 (make venv). 컨테이너와 임시 파일은 끝나면 지운다.
# 환경변수: KC_TEST_PORT(기본 18080), KEYCLOAK_IMAGE(기본 docker-compose.yml의 keycloak 이미지), BACKEND_PYTHON
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
REALM_JSON="$ROOT/deploy/compose/keycloak/realm-marine.json"
IMAGE=${KEYCLOAK_IMAGE:-$(sed -n 's#^ *image: *\(quay.io/keycloak/keycloak:[^ ]*\) *$#\1#p' "$ROOT/deploy/compose/docker-compose.yml" | head -n1)}
PORT=${KC_TEST_PORT:-18080}
NAME="marine-kc-mfa-check-$$"
ADMIN_PASSWORD=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
WORK=$(mktemp -d)
BACKEND_PYTHON=${BACKEND_PYTHON:-$ROOT/backend/.venv/bin/python}

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "Keycloak 시험 컨테이너 시작: $NAME ($IMAGE, 127.0.0.1:$PORT)"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=check-admin -e KC_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  -v "$REALM_JSON:/opt/keycloak/data/import/realm-marine.json:ro" \
  "$IMAGE" start-dev --import-realm >/dev/null

for _ in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:$PORT/realms/marine/.well-known/openid-configuration" >/dev/null; then break; fi
  if ! docker ps -q --filter "name=^$NAME$" | grep -q .; then
    echo "Keycloak 컨테이너가 멈췄습니다 (realm 가져오기 실패일 수 있음):" >&2
    docker logs "$NAME" 2>&1 | tail -n 40 >&2
    exit 1
  fi
  sleep 2
done
curl -sf "http://127.0.0.1:$PORT/realms/marine/.well-known/openid-configuration" >/dev/null \
  || { echo "Keycloak이 준비되지 않았습니다" >&2; docker logs "$NAME" 2>&1 | tail -n 40 >&2; exit 1; }

status=0
KC_URL="http://127.0.0.1:$PORT" KC_ADMIN_USER=check-admin KC_ADMIN_PASSWORD="$ADMIN_PASSWORD" EVIDENCE="$WORK/evidence.json" \
  node "$HERE/mfa-login.cjs" || status=$?

if [ -x "$BACKEND_PYTHON" ] && [ -f "$WORK/evidence.json" ]; then
  echo
  echo "API(KeycloakIdentity) 판정 확인"
  "$BACKEND_PYTHON" "$HERE/check_backend_mfa.py" "$WORK/evidence.json" || status=1
else
  echo "API 판정 확인 건너뜀: $BACKEND_PYTHON 없음 (make venv)"
fi

if [ "$status" -ne 0 ]; then
  echo "MFA 확인 실패" >&2
  docker logs "$NAME" 2>&1 | grep -E 'WARN|ERROR' | grep -v '^\s*at ' | tail -n 20 >&2 || true
fi
exit "$status"
