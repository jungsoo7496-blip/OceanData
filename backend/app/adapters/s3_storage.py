"""StoragePort → S3 호환 스토리지 (개발: SeaweedFS). S3 공통 기능만 쓴다 (tech.md HW-1)."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Iterator

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.ports.storage import CompletedPart, ObjectInfo

log = logging.getLogger(__name__)


class ChunkReader(io.RawIOBase):
    """청크 이터레이터를 읽기 전용 파일처럼 (탐색 불가). upload_fileobj가 PUT 또는 멀티파트로 나눠 올린다."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._it = iter(chunks)
        self._buf = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
        while not len(self._buf):
            try:
                self._buf = memoryview(next(self._it))
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


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
        return self.buckets_ok(self.settings.buckets.values())

    def buckets_ok(self, buckets: Iterable[str]) -> bool:
        """버킷마다 HEAD (준비 상태·WARM 계층 확인)."""
        try:
            for bucket in buckets:
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

    # --- 서버 측 암호화 (08번 요구사항 1.3·1.4). 버킷 기본 암호화로 요청해 사전 서명 업로드에도 적용되게 한다
    def ensure_encryption(self, bucket: str) -> str:
        """SSE-S3(AES256) 기본 암호화를 요청한다. 'enabled' 또는 'unsupported'."""
        try:
            self.client.put_bucket_encryption(  # type: ignore[attr-defined]
                Bucket=bucket, ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        except (ClientError, BotoCoreError):
            return "unsupported"
        return "enabled" if self.encryption_status(bucket) == "enabled" else "unsupported"

    def encryption_status(self, bucket: str) -> str:
        """'enabled'(기본 암호화 있음) · 'disabled'(지원하지만 꺼짐) · 'unsupported'(미지원·확인 불가)."""
        try:
            r = self.client.get_bucket_encryption(Bucket=bucket)  # type: ignore[attr-defined]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            return "disabled" if code == "ServerSideEncryptionConfigurationNotFoundError" else "unsupported"
        except BotoCoreError:
            return "unsupported"
        rules = r.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        algs = {x.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") for x in rules}
        return "enabled" if algs & {"AES256", "aws:kms"} else "disabled"

    # --- 객체
    def put_object(self, bucket: str, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=bucket, Key=key, Body=data, **extra)  # type: ignore[attr-defined]

    def put_stream(self, bucket: str, key: str, chunks: Iterable[bytes], content_type: str | None = None) -> None:
        """청크 스트림을 PUT 또는 멀티파트로 올린다. 임시 디스크를 거치지 않는다 (계층 이동·저장소 사이 이동)."""
        extra = {"ContentType": content_type} if content_type else None
        self.client.upload_fileobj(ChunkReader(chunks), bucket, key, ExtraArgs=extra)  # type: ignore[attr-defined]

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

    def upload_file(self, bucket: str, key: str, path: str, content_type: str | None = None) -> None:
        """로컬 파일을 PUT 또는 멀티파트로 올린다 (파생 자산)."""
        extra = {"ContentType": content_type} if content_type else None
        self.client.upload_file(path, bucket, key, ExtraArgs=extra)  # type: ignore[attr-defined]

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
