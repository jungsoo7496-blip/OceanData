"""T02-g 시험 전용 API: 실제 DB·S3 업로드 + 합성 서명 IdP 어댑터.

제품 서버에는 이 시험 어댑터나 인증 우회 설정을 넣지 않는다.
브라우저 시험이 끝나면 전용 Compose 프로젝트와 DB를 함께 정리한다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import jwt
import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from moto.server import ThreadedMotoServer

from app.adapters.keycloak_identity import KeycloakIdentity
from app.adapters.notifier import LogNotifier
from app.adapters.s3_storage import S3Storage
from app.config import build_settings
from app.container import build_container
from app.main import create_app
from app.seed import seed, sub
from app.services.ingest import IntegrityVerifier

ROOT = Path(__file__).resolve().parents[2]
ISSUER = "http://localhost:8080/realms/marine"


def main() -> None:
    # 접근 로그에는 사전 서명 URL이 들어갈 수 있으므로 시험 결과에도 남기지 않는다.
    logging.getLogger("werkzeug").disabled = True
    logging.getLogger("moto").setLevel(logging.ERROR)
    url = os.environ["E2E_DATABASE_URL"]
    os.environ["MIGRATION_DATABASE_URL"] = url
    cfg = Config(str(ROOT / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend/app/db/migrations"))
    command.upgrade(cfg, "head")
    # Alembic이 로깅 설정을 다시 읽으므로 그 뒤에 차단한다. 모의 S3 접근 로그도 서명 쿼리를 남기지 않는다.
    logging.disable(logging.CRITICAL)
    moto = ThreadedMotoServer(ip_address="0.0.0.0", port=8333, verbose=False)
    moto.start()
    settings = build_settings(
        database_url=url, s3_endpoint_url="http://127.0.0.1:8333",
        s3_public_endpoint_url=os.environ["E2E_S3_PUBLIC_URL"], s3_access_key_id="test-access",
        s3_secret_access_key="test-secret-value", idp_issuer_url=ISSUER,
        device_token_signing_key="test-signing-key-" + "x" * 32,
        upload_part_size=5 * 1024 * 1024, processing_steps=[], report_formats=[],
    )
    storage = S3Storage(settings)
    for bucket in settings.buckets.values():
        storage.ensure_bucket(bucket)
        storage.client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [{
            "AllowedHeaders": ["*"], "AllowedMethods": ["PUT", "GET", "HEAD"],
            "AllowedOrigins": ["*"], "ExposeHeaders": ["ETag"],
        }]})
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": "e2e", "use": "sig"}
    identity = KeycloakIdentity(ISSUER, "marine-api", jwks_provider=lambda: {"keys": [jwk]})
    c = build_container(settings, storage=storage, identity=identity, notifier=LogNotifier())
    with c.sessionmaker.begin() as db:
        seed(db)
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "aud": "marine-api", "sub": sub(102), "iat": now, "exp": now + 3600},
                       key, algorithm="RS256", headers={"kid": "e2e"})
    session = {"authority": ISSUER, "client_id": "marine-web", "user": {
        "access_token": token, "token_type": "Bearer", "scope": "openid profile", "expires_at": now + 3600,
        "profile": {"sub": sub(102), "iss": ISSUER, "aud": "marine-web"},
    }}
    session_file = Path(os.environ["E2E_SESSION_FILE"])
    fd = os.open(session_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(session, f)

    stopping = threading.Event()

    def verify_received() -> None:
        while not stopping.is_set():
            job = c.queue.claim(["integrity_verify"], "web-e2e")
            if job is None:
                stopping.wait(0.1)
                continue
            try:
                IntegrityVerifier(c).verify(job.payload["asset_id"])
                c.queue.complete(job.job_id)
            except Exception:
                # 상세 예외·서명 URL을 노출하지 않고 시험에서 영수증 미발급으로 실패시킨다.
                logging.getLogger(__name__).error("web_e2e_integrity_failed")
                stopping.set()

    worker = threading.Thread(target=verify_received, daemon=True)
    worker.start()
    try:
        uvicorn.run(create_app(c), host="0.0.0.0", port=8000, access_log=False, log_level="error")
    finally:
        stopping.set()
        worker.join(timeout=5)
        moto.stop()
        c.engine.dispose()


if __name__ == "__main__":
    main()
