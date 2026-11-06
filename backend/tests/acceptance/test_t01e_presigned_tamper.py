"""T01-e: 사전 서명 URL의 객체 키·만료·서명을 바꾸면 저장소가 거부하고 원본이 드러나지 않는다 (docs/ACCEPTANCE.md, 04·13번).

모의 S3(moto)는 서명을 검사하지 않으므로, 운영 compose와 같은 SeaweedFS(chrislusf/seaweedfs:3.80)를 일회용 컨테이너로 띄우고
플랫폼의 사전 서명 다운로드 URL 변조와 멀티파트 업로드 크기를 검증한다. docker나 이미지가 없으면 건너뛴다 (값은 모두 합성).
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
import httpx
import pytest
from botocore.config import Config

from app.adapters.s3_storage import S3Storage
from tests.conftest import test_settings as make_settings

pytestmark = [pytest.mark.acceptance,
              pytest.mark.skipif(shutil.which("docker") is None, reason="docker가 필요합니다")]
IMAGE = "chrislusf/seaweedfs:3.80"  # deploy/compose/docker-compose.yml과 같은 판
BUCKET = "t01e-raw"
KEY_A = "org_00000000000000000000000001/prj_x/ses_x/dev_x/ast_a/ORIGINAL"
KEY_B = "org_00000000000000000000000002/prj_y/ses_y/dev_y/ast_b/ORIGINAL"
BODY_A, BODY_B = b"synthetic-object-of-org-A", b"SECRET-synthetic-object-of-org-B"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def seaweed() -> Iterator[tuple[str, str, str]]:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False).returncode != 0:
        pytest.skip(f"{IMAGE} 이미지가 없습니다 (docker pull 뒤 다시)")
    port, name = _free_port(), f"t01e-{int(time.time() * 1000) % 10**9}"
    access, secret = "t01e-access", "t01e-secret-synthetic-0123456789"
    config = json.dumps({"identities": [{"name": "t01e", "credentials": [{"accessKey": access, "secretKey": secret}],
                                         "actions": ["Admin", "Read", "Write", "List"]}]})
    script = (f"printf '%s' '{config}' > /tmp/s3.json && exec weed server -dir=/data -ip.bind=0.0.0.0 -s3 "
              "-s3.port=8333 -s3.config=/tmp/s3.json -volume.max=0")
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8333",
                    "--entrypoint", "/bin/sh", IMAGE, "-c", script], check=True, capture_output=True)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id=access,
                              aws_secret_access_key=secret,
                              config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"))
        deadline, last = time.time() + 90, ""
        while True:  # S3 API와 볼륨 서버가 다 뜰 때까지 버킷·객체 쓰기를 다시 시도한다
            try:
                if BUCKET not in [b["Name"] for b in client.list_buckets().get("Buckets", [])]:
                    client.create_bucket(Bucket=BUCKET)
                client.put_object(Bucket=BUCKET, Key=KEY_A, Body=BODY_A)
                client.put_object(Bucket=BUCKET, Key=KEY_B, Body=BODY_B)
                break
            except Exception as exc:  # noqa: BLE001 - 기동 중 오류는 다시 시도
                last = repr(exc)[:200]
                if time.time() > deadline:
                    pytest.fail(f"SeaweedFS를 준비하지 못했습니다: {last}")
                time.sleep(1.5)
        yield endpoint, access, secret
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _with_query(url: str, **changes: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(changes)
    return urlunsplit(parts._replace(query=urlencode(query)))


@pytest.mark.covers("T01-e")
def test_t01e_tampered_presigned_urls_are_rejected(seaweed: tuple[str, str, str]) -> None:
    endpoint, access, secret = seaweed
    storage = S3Storage(make_settings(s3_endpoint_url=endpoint, s3_access_key_id=access, s3_secret_access_key=secret))
    url = storage.presign_get(BUCKET, KEY_A, 300)  # 플랫폼이 다운로드 URL로 내주는 것과 같은 서명
    ok = httpx.get(url, timeout=10)
    assert ok.status_code == 200 and ok.content == BODY_A

    tampered = {
        "객체 키(다른 기관)": url.replace(KEY_A, KEY_B),
        "만료 늘리기": _with_query(url, **{"X-Amz-Expires": "604800"}),
        "서명 바꾸기": _with_query(url, **{"X-Amz-Signature": "0" * 64}),
        "서명 빼기": urlunsplit(urlsplit(url)._replace(query=urlencode(
            [(k, v) for k, v in parse_qsl(urlsplit(url).query) if k != "X-Amz-Signature"]))),
        "서명 시각 바꾸기": _with_query(url, **{"X-Amz-Date": "20991231T000000Z"}),
        "다른 자격증명": _with_query(url, **{"X-Amz-Credential": "AKIAOTHER/20261105/us-east-1/s3/aws4_request"}),
    }
    for what, bad in tampered.items():
        r = httpx.get(bad, timeout=10)
        assert r.status_code in (400, 403), (what, r.status_code, r.text[:200])
        assert BODY_B not in r.content and BODY_A not in r.content, what  # 원본 노출 0

    # 만료된 URL (1초짜리를 만료 뒤에 쓴다)
    short = storage.presign_get(BUCKET, KEY_A, 1)
    time.sleep(2.5)
    r = httpx.get(short, timeout=10)
    assert r.status_code in (400, 403) and BODY_A not in r.content, r.text[:200]


@pytest.mark.parametrize("body", [b"synthetic-planned-part", b""], ids=["nonempty", "empty"])
def test_presigned_upload_part_enforces_content_length(seaweed: tuple[str, str, str], body: bytes) -> None:
    endpoint, access, secret = seaweed
    storage = S3Storage(make_settings(s3_endpoint_url=endpoint, s3_access_key_id=access, s3_secret_access_key=secret))
    key = "synthetic-multipart/part-size"
    upload_id = storage.create_multipart_upload(BUCKET, key)
    try:
        url = storage.presign_upload_part(BUCKET, key, upload_id, 1, 300, content_length=len(body))
        with httpx.Client(timeout=10) as client:
            valid = client.put(url, content=body, headers={"Content-Length": str(len(body))})
            assert valid.is_success, (valid.status_code, valid.text[:240])
            parts = storage.list_parts(BUCKET, key, upload_id)
            assert [part.part_number for part in parts] == [1]

            wrong_bodies = (body + b"x", body[:-1]) if body else (b"x",)
            wrong_responses = [client.put(url, content=wrong_body) for wrong_body in wrong_bodies]
            for wrong_body, response in zip(wrong_bodies, wrong_responses, strict=True):
                assert response.request.headers["Content-Length"] == str(len(wrong_body))
                assert response.status_code in (400, 403), (len(wrong_body), response.status_code, response.text[:240])
            assert storage.list_parts(BUCKET, key, upload_id) == parts
    finally:
        storage.abort_multipart_upload(BUCKET, key, upload_id)
