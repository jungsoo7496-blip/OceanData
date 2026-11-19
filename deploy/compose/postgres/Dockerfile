# 개발용 PostgreSQL 16 + pgvector + PostGIS (tech.md 2장)
# postgis/postgis 이미지는 bullseye 기반이라 pgdg 저장소에서 pgvector를 받을 수 없어, bookworm 기반 pgvector 이미지에 PostGIS를 더한다.
# pgdg 저장소의 PostGIS는 그 저장소의 최신 PostgreSQL 16 판에 맞춰 빌드된다. 2026-11에 PostGIS 3.7.0이 기본 이미지의
# 16.10에 없는 heap_datum_check를 불러 CREATE EXTENSION postgis가 실패했다. 그래서 서버(postgresql-16)도 같은 저장소의
# 판으로 함께 올린다.
FROM pgvector/pgvector:0.8.0-pg16
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16 postgresql-16-postgis-3 \
 && rm -rf /var/lib/apt/lists/*
COPY initdb/ /docker-entrypoint-initdb.d/
