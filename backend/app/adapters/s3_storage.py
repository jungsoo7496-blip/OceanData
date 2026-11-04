"""StoragePort → S3 호환 스토리지 (개발: SeaweedFS). S3 공통 기능만 쓴다 (tech.md HW-1)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.ports.storage import CompletedPart, ObjectInfo

log = logging.getLogger(__name__)


class S3Storage:
    def __init__(self, settings: Settings, client: object | None = None,
                 presign_client: object | None = None) -> None:
        self.settings = settings
        self.client = client or self._make_client(settings, settings.s3_endpoint_url)
        # 사전 서명은 클라이언트가 접근하는 주소로 서명해야 한다 (컨테이너 내부 주소와 다를 수 있다)
        self.presigner = presign_client or (
            self._make_client(settings, settings.s3_public_endpoint_url)
            if settings.s3_public_endpoint_url else self.client)

    @staticmethod
    def _make_client(settings: Settings, endpoint: str) -> object:
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            config=Config(
                s3={"addressing_style": "path" if settings.s3_force_path_style else "virtual"},
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=settings.readiness_timeout_s,
                read_timeout=30,
            ),
        )

    # --- 상태
    def check(self) -> bool:
        try:
            for bucket in self.settings.buckets.values():
                self.client.head_bucket(Bucket=bucket)  # type: ignore[attr-defined]
            return True
        except (BotoCoreError, ClientError) as exc:
            log.warning("storage_check_failed", extra={"error_type": type(exc).__name__})
            return False

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self.client.head_bucket(Bucket=bucket)  # type: ignore[attr-defined]
        except ClientError:
            self.client.create_bucket(Bucket=bucket)  # type: ignore[attr-defined]

    # --- 객체
    def put_object(self, bucket: str, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=bucket, Key=key, Body=data, **extra)  # type: ignore[attr-defined]

    def get_object(self, bucket: str, key: str) -> bytes:
        return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()  # type: ignore[attr-defined]

    def iter_object(self, bucket: str, key: str, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        body = self.client.get_object(Bucket=bucket, Key=key)["Body"]  # type: ignore[attr-defined]
        try:
            yield from body.iter_chunks(chunk_size)
        finally:
            body.close()

    def move_object(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        """GET 스트림 → PUT/멀티파트 (CopyObject는 공통 기능 목록 밖이라 쓰지 않는다, HW-1)."""
        body = self.client.get_object(Bucket=src_bucket, Key=src_key)["Body"]  # type: ignore[attr-defined]
        try:
            self.client.upload_fileobj(body, dst_bucket, dst_key)  # type: ignore[attr-defined]
        finally:
            body.close()
        self.client.delete_object(Bucket=src_bucket, Key=src_key)  # type: ignore[attr-defined]

    def head_object(self, bucket: str, key: str) -> ObjectInfo | None:
        try:
            r = self.client.head_object(Bucket=bucket, Key=key)  # type: ignore[attr-defined]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(key=key, size=int(r["ContentLength"]), etag=r.get("ETag"))

    def delete_object(self, bucket: str, key: str) -> None:
        self.client.delete_object(Bucket=bucket, Key=key)  # type: ignore[attr-defined]

    def list_objects(self, bucket: str, prefix: str) -> Iterator[ObjectInfo]:
        paginator = self.client.get_paginator("list_objects_v2")  # type: ignore[attr-defined]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                yield ObjectInfo(key=o["Key"], size=int(o["Size"]), etag=o.get("ETag"))

    def put_tags(self, bucket: str, key: str, tags: dict[str, str]) -> None:
        self.client.put_object_tagging(  # type: ignore[attr-defined]
            Bucket=bucket, Key=key,
            Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]},
        )

    # --- 멀티파트
    def create_multipart_upload(self, bucket: str, key: str, content_type: str | None = None) -> str:
        extra = {"ContentType": content_type} if content_type else {}
        r = self.client.create_multipart_upload(Bucket=bucket, Key=key, **extra)  # type: ignore[attr-defined]
        return str(r["UploadId"])

    def presign_upload_part(self, bucket: str, key: str, upload_id: str, part_number: int,
                            expires_s: int) -> str:
        return str(self.presigner.generate_presigned_url(  # type: ignore[attr-defined]
            "upload_part",
            Params={"Bucket": bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=expires_s,
        ))

    def list_parts(self, bucket: str, key: str, upload_id: str) -> list[CompletedPart]:
        parts: list[CompletedPart] = []
        marker = 0
        while True:
            r = self.client.list_parts(  # type: ignore[attr-defined]
                Bucket=bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker
            )
            parts += [CompletedPart(int(p["PartNumber"]), p["ETag"]) for p in r.get("Parts", [])]
            if not r.get("IsTruncated"):
                return parts
            marker = int(r["NextPartNumberMarker"])

    def complete_multipart_upload(self, bucket: str, key: str, upload_id: str,
                                  parts: list[CompletedPart]) -> None:
        self.client.complete_multipart_upload(  # type: ignore[attr-defined]
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": p.part_number, "ETag": p.etag}
                                       for p in sorted(parts, key=lambda p: p.part_number)]},
        )

    def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        self.client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)  # type: ignore[attr-defined]

    # --- 사전 서명 (URL은 로그에 남기지 않는다)
    def presign_put(self, bucket: str, key: str, expires_s: int, content_type: str | None = None) -> str:
        params: dict = {"Bucket": bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return str(self.presigner.generate_presigned_url(  # type: ignore[attr-defined]
            "put_object", Params=params, ExpiresIn=expires_s))

    def presign_get(self, bucket: str, key: str, expires_s: int) -> str:
        return str(self.presigner.generate_presigned_url(  # type: ignore[attr-defined]
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_s
        ))
