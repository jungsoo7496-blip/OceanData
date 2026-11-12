"""T12 부하 시나리오: 업로드 세션 생성·파트 URL·파트 전송·완료, 관측값 배치, 자산 검색. 합격 수치는 넣지 않는다."""

from __future__ import annotations

import hashlib
import os
import random
from datetime import UTC, datetime, timedelta

from locust import HttpUser, between, task

TOKEN = os.environ.get("LOAD_TOKEN", "")
SOURCE = os.environ.get("LOAD_SOURCE_ID", "dev_00000000000000000000000302")
SENSOR = os.environ.get("LOAD_SENSOR_ID", "dev_00000000000000000000000303")
SIZE = int(os.environ.get("LOAD_FILE_BYTES", str(1024 * 1024)))
BATCH = int(os.environ.get("LOAD_BATCH", "100"))


class PlatformUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.client.headers.update({"Authorization": f"Bearer {TOKEN}"})

    @task(2)
    def upload(self) -> None:
        data = os.urandom(SIZE)  # 합성 데이터만 (AGENTS.md 11)
        sha = hashlib.sha256(data).hexdigest()
        manifest = {"schema_version": "1.0.0", "source_system_id": SOURCE, "data_type": "DOCUMENT",
                    "variant": "ORIGINAL",
                    "file": {"original_filename": f"load-{sha[:8]}.bin", "byte_size": SIZE,
                             "mime_type": "application/octet-stream", "checksum_sha256": sha},
                    "timestamp_quality": {"source": "UNKNOWN", "verified": False},
                    "client": {"kind": "API", "version": "loadtest"}}
        with self.client.post("/v1/uploads", json=manifest, name="POST /v1/uploads", catch_response=True) as r:
            if r.status_code not in (200, 201):
                r.failure(f"{r.status_code}")
                return
            up = r.json()
        if up.get("status") == "ALREADY_REGISTERED":
            return
        parts = []
        size = up["part_size"]
        for p in up.get("parts", []):
            n = p["part_number"]
            put = self.client.put(p["url"], data=data[(n - 1) * size:n * size], name="PUT part (S3)")
            parts.append({"part_number": n, "etag": put.headers.get("etag", "")})
        self.client.post(f"/v1/uploads/{up['upload_id']}/complete", json={"parts": parts},
                         name="POST /v1/uploads/{id}/complete")

    @task(3)
    def observations(self) -> None:
        start = datetime.now(UTC) - timedelta(hours=1, seconds=random.randint(0, 86_400))
        items = [{"metric": "sea_water_temperature", "value": round(15 + random.random() * 5, 2),
                  "unit": "degC", "quality_flag": "GOOD",
                  "observed_at": (start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")} for i in range(BATCH)]
        self.client.post("/v1/observations", name="POST /v1/observations",
                         json={"schema_version": "1.0.0", "source_system_id": SENSOR, "observations": items})

    @task(5)
    def search(self) -> None:
        self.client.get("/v1/assets?limit=50", name="GET /v1/assets")
