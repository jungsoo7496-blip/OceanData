"""Detector (14번 요구사항 2.1) — InferencePort 위의 영상 단위 탐지기.

실제 모델(얼굴·번호판·선박 문자 탐지)은 라이선스를 확인해 Model_Registry에 등록한 뒤 붙인다 (확인 필요).
지금 있는 구현:
- SyntheticRegionDetector: 합성 시험 영상에 미리 적어 둔 영역(asset.ext["synthetic_regions"])을 그대로 돌려주는
  모의 탐지기. 파이프라인·검수·집계 흐름 시험용이며 실제 영상에서는 아무것도 찾지 않는다.
- OnnxRegionDetector: Model_Registry 모델을 InferencePort로 불러 쓰는 자리. 모델별 전처리·후처리는 모델 확정 후.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.deid import Region


@dataclass(frozen=True)
class ModelInfo:
    name: str
    version: str
    sha256: str | None
    license: str | None


class DetectorUnavailable(RuntimeError):
    pass


class DetectorPort(Protocol):
    model: ModelInfo

    def detect(self, asset: Any, video_path: str, classes: list[str], sample_fps: float) -> list[Region]: ...


class SyntheticRegionDetector:
    model = ModelInfo("synthetic-regions", "0", None, "N/A (모의)")

    def detect(self, asset: Any, video_path: str, classes: list[str], sample_fps: float) -> list[Region]:
        out = []
        for r in (asset.ext or {}).get("synthetic_regions", []):
            if r.get("cls") in classes:
                out.append(Region(r["cls"], float(r["t0"]), float(r["t1"]), int(r["x"]), int(r["y"]), int(r["w"]),
                                  int(r["h"]), float(r.get("confidence", 0.9))))
        return out


class OnnxRegionDetector:
    def __init__(self, inference: Any, name: str, version: str) -> None:
        self.inference, self.model = inference, ModelInfo(name, version, None, None)

    def detect(self, asset: Any, video_path: str, classes: list[str], sample_fps: float) -> list[Region]:
        # 모델 로드 시 Model_Registry 라이선스·sha256 검사 (05번 요구사항 8.3, adapters/onnx_inference.py)
        self.inference.load(self.model.name, self.model.version)
        raise DetectorUnavailable("모델별 전처리·후처리는 탐지 모델 확정 후 구현한다 (14번 design.md 확인 필요)")


def build_detector(settings: Any, inference: Any = None) -> DetectorPort:
    kind = getattr(settings, "deid_detector", "synthetic")
    if kind == "synthetic":
        return SyntheticRegionDetector()
    if kind.startswith("onnx:"):
        name, _, version = kind[5:].partition("@")
        return OnnxRegionDetector(inference, name, version or "1")
    raise DetectorUnavailable(f"알 수 없는 탐지기 설정: {kind}")
