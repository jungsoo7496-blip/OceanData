#!/bin/bash
# 소유자(마이그레이션) 계정과 애플리케이션 계정을 나눈다. 애플리케이션 계정은 감사 로그를 수정·삭제할 수 없다 (02번 요구사항 7.2).
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_DB_USER:-marine_app}') THEN
    CREATE ROLE ${APP_DB_USER:-marine_app} LOGIN PASSWORD '${APP_DB_PASSWORD:?APP_DB_PASSWORD 필요}';
  END IF;
END \$\$;
GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO ${APP_DB_USER:-marine_app};
GRANT USAGE ON SCHEMA public TO ${APP_DB_USER:-marine_app};
SQL
