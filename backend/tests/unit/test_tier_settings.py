"""08번 요구사항 9.1·9.3: 계층 설정과 유예 규칙 (DB·S3 없이).

HOT은 S3_*·BUCKET_* 그대로, WARM은 주소·자격증명을 비우면 같은 저장소를 쓰고 버킷만 다르다(<버킷>-warm).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.s3_storage import ChunkReader
from app.config import ConfigError, build_settings
from app.services import tiers
from app.services.tier_mover import grace

BASE = dict(database_url="x", s3_endpoint_url="http://s3.test:8333", s3_access_key_id="a", s3_secret_access_key="b",
            idp_issuer_url="http://idp.test", device_token_signing_key="k" * 40)


def test_warm_defaults_share_hot_storage() -> None:
    s = build_settings(**BASE)
    assert s.tier_buckets("WARM") == {"RAW": "raw-warm", "PROCESSED": "proc-warm", "EVIDENCE": "evidence-warm",
                                      "DATASET": "datasets-warm"}
    assert s.tier_buckets("HOT") == s.buckets and "QUARANTINE" not in s.tier_buckets("WARM")  # 격리는 HOT에만
    assert s.warm_shares_hot_storage and s.tiering_enabled is False
    with pytest.raises(ValueError):
        s.tier_buckets("COLD")
    # Compose가 넘기는 빈 값은 지정하지 않은 것과 같다
    blank = build_settings(**BASE, warm_s3_endpoint_url="", warm_s3_access_key_id="", warm_s3_secret_access_key="",
                           warm_s3_public_endpoint_url="", warm_bucket_raw="")
    assert blank.warm_shares_hot_storage and blank.tier_buckets("WARM")["RAW"] == "raw-warm"


def test_warm_bucket_must_differ_from_hot_on_same_storage() -> None:
    # 같은 저장소에서 같은 버킷이면 이동이 하나뿐인 사본을 지운다 → 기동하지 않는다 (키 이름만 알린다)
    with pytest.raises(ConfigError) as ei:
        build_settings(**BASE, bucket_raw="hot-raw-bkt", warm_bucket_raw="hot-raw-bkt", warm_bucket_proc="proc")
    assert ei.value.invalid == ["WARM_BUCKET_PROC", "WARM_BUCKET_RAW"] and "hot-raw-bkt" not in ei.value.message
    with pytest.raises(ConfigError):  # 자격증명만 달라도 같은 주소면 같은 버킷 공간이다
        build_settings(**BASE, warm_s3_access_key_id="w", warm_bucket_evidence="evidence")
    # 다른 저장소(주소)면 버킷 이름이 같아도 된다
    s = build_settings(**BASE, warm_s3_endpoint_url="http://warm.test:9000", warm_bucket_raw="raw")
    w = s.warm_s3_settings()
    assert not s.warm_shares_hot_storage and s.tier_buckets("WARM")["RAW"] == "raw"
    assert (w.s3_endpoint_url, w.s3_public_endpoint_url, w.s3_access_key_id.get_secret_value()) == (
        "http://warm.test:9000", None, "a")  # 사전 서명 주소는 WARM_S3_PUBLIC_ENDPOINT_URL로 따로 준다


def test_warm_credentials_on_same_endpoint_use_separate_connection() -> None:
    s = build_settings(**BASE, s3_public_endpoint_url="http://pub.test", warm_s3_access_key_id="wa",
                       warm_s3_secret_access_key="ws", warm_s3_force_path_style=False)
    w = s.warm_s3_settings()
    assert not s.warm_shares_hot_storage
    assert (w.s3_endpoint_url, w.s3_public_endpoint_url, w.s3_force_path_style) == (
        "http://s3.test:8333", "http://pub.test", False)
    assert (w.s3_access_key_id.get_secret_value(), w.s3_secret_access_key.get_secret_value()) == ("wa", "ws")
    assert s.s3_access_key_id.get_secret_value() == "a"  # HOT 설정은 그대로


def test_grace_is_at_least_download_url_ttl() -> None:
    # 원래 객체는 다운로드 URL 유효시간 이상 남긴다 (전환 직전에 발급한 URL이 끝까지 쓰인다)
    assert grace(build_settings(**BASE, tier_delete_grace_s=10, presign_download_ttl_s=300)).total_seconds() == 300
    assert grace(build_settings(**BASE, tier_delete_grace_s=3600)).total_seconds() == 3600


def test_location_defaults_and_chunk_reader() -> None:
    legacy = SimpleNamespace(storage_tier=None, storage_zone=None, storage_key="k", variant="ORIGINAL")
    assert tiers.where(legacy) == ("HOT", "RAW", "k")
    assert tiers.where(SimpleNamespace(storage_tier="WARM", storage_zone=None, storage_key="k",
                                       variant="PROXY")) == ("WARM", "PROCESSED", "k")
    r = ChunkReader(iter([b"", b"abc", b"", b"defgh", b"i"]))
    out = []
    while chunk := r.read(2):
        out.append(chunk)
    assert b"".join(out) == b"abcdefghi" and ChunkReader([]).read(10) == b""
