# 개발용 PostgreSQL 16 + pgvector + PostGIS (tech.md 2장)
# postgis/postgis 이미지는 bullseye 기반이라 pgdg 저장소에서 pgvector를 받을 수 없어, bookworm 기반 pgvector 이미지에 PostGIS를 더한다.
FROM pgvector/pgvector:0.8.0-pg16
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-postgis-3 \
 && rm -rf /var/lib/apt/lists/*
COPY initdb/ /docker-entrypoint-initdb.d/
