"""05번 단위 시험: ffprobe 결과 해석, 모델 레지스트리 라이선스·해시 검사."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.onnx_inference import ModelRejected, check_model
from app.services.pipeline import CorruptMedia, parse_probe
from app.workers.main import backoff_s


def test_parse_probe() -> None:
    probe = {"format": {"format_name": "mov,mp4,m4a", "bit_rate": "1500000", "duration": "12.5",
                        "tags": {"creation_time": "2026-11-01T01:02:03.000000Z", "location": "+35.1000+129.0500/"}},
             "streams": [{"codec_type": "audio"},
                         {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
                          "codec_name": "h264", "tags": {"rotate": "90"}}]}
    out = parse_probe(probe)
    assert out["video_ext"] == {"resolution": "1920x1080", "fps": 29.97, "codec": "h264", "container": "mov",
                                "bitrate_mbps": 1.5, "duration_s": 12.5, "orientation_deg": 90.0}
    assert out["creation_time"].isoformat() == "2026-11-01T01:02:03+00:00"
    assert out["location"] == {"type": "Point", "coordinates": [129.05, 35.1]}
    # 1970년 같은 기본값·위경도 뒤바뀐 값은 근거로 쓰지 않는다
    bad = parse_probe({"format": {"tags": {"creation_time": "1970-01-01T00:00:00Z", "location": "+129.0+35.1/"}},
                       "streams": [{"codec_type": "video"}]})
    assert bad["creation_time"] is None and bad["location"] is None
    with pytest.raises(CorruptMedia):
        parse_probe({"format": {}, "streams": [{"codec_type": "audio"}]})


def test_model_registry_checks(tmp_path: Path) -> None:
    w = tmp_path / "m.onnx"
    w.write_bytes(b"weights")
    sha = hashlib.sha256(b"weights").hexdigest()
    check_model(SimpleNamespace(license="Apache-2.0", sha256=sha), w)  # type: ignore[arg-type]
    for lic in ("AGPL-3.0", None, "CC-BY-NC-4.0"):  # AGPL·비상업·미기록은 거부 (요구사항 8.4)
        with pytest.raises(ModelRejected):
            check_model(SimpleNamespace(license=lic, sha256=sha), w)  # type: ignore[arg-type]
    with pytest.raises(ModelRejected):
        check_model(SimpleNamespace(license="MIT", sha256="0" * 64), w)  # type: ignore[arg-type]
    with pytest.raises(ModelRejected):
        check_model(None, w)


def test_backoff() -> None:
    assert [backoff_s(n) for n in (1, 2, 3)] == [15, 30, 60] and backoff_s(30) == 3600
