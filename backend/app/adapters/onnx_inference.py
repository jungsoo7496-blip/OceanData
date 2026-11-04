"""InferencePort → ONNX Runtime (MIT, OSS-CANDIDATES 채택). 기본 CPU, GPU_ENABLED면 CUDA (05번 요구사항 8).

Model_Registry에 이름·버전·sha256·허용 라이선스가 기록된 모델만 로드한다. onnxruntime은 선택 의존성이다
(`pip install onnxruntime` 또는 `onnxruntime-gpu`). 14번 스펙에서 실제 탐지 모델을 붙인다.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ModelRegistryEntry

log = logging.getLogger(__name__)
# tech.md 3장 허용 목록. 모델 가중치도 라이선스 검토 대상이다 (AGPL·비상업 전용은 승인 없이 불가)
ALLOWED_MODEL_LICENSES = frozenset({"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "MPL-2.0",
                                    "CC0-1.0", "CC-BY-4.0"})


class ModelRejected(Exception):
    pass


def check_model(entry: ModelRegistryEntry | None, weights: Path) -> None:
    if entry is None:
        raise ModelRejected("Model_Registry에 없는 모델입니다")
    if not entry.license or entry.license not in ALLOWED_MODEL_LICENSES:
        raise ModelRejected(f"허용되지 않았거나 기록되지 않은 모델 라이선스: {entry.license!r}")
    h = hashlib.sha256()
    with weights.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != entry.sha256:
        raise ModelRejected("모델 가중치 sha256이 Model_Registry 기록과 다릅니다")


class OnnxInference:
    def __init__(self, sm: sessionmaker[Session], gpu_enabled: bool, model_dir: str) -> None:
        self.sm, self.gpu_enabled, self.model_dir = sm, gpu_enabled, Path(model_dir)
        self._sessions: dict[str, Any] = {}

    def load(self, name: str, version: str) -> Any:
        key = f"{name}:{version}"
        if key in self._sessions:
            return self._sessions[key]
        with self.sm() as db:
            entry = db.get(ModelRegistryEntry, (name, version))
            weights = self.model_dir / Path(entry.weights_uri).name if entry else Path("/nonexistent")
            check_model(entry, weights)
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("onnxruntime이 설치되지 않았습니다 (선택 의존성)") from exc
        providers = ["CPUExecutionProvider"]
        if self.gpu_enabled and "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")
        sess = ort.InferenceSession(str(weights), providers=providers)
        log.info("model_loaded", extra={"model": key, "providers": providers})
        self._sessions[key] = sess
        return sess

    def detect(self, model_id: str, frame: bytes) -> list[dict[str, Any]]:
        raise NotImplementedError("탐지 전처리·후처리는 14번 스펙에서 구현한다")
