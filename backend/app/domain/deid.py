"""비식별 가림 영역 계산 (14번 요구사항 2). 순수 함수 — 탐지 결과 → 시간 구간별 가림 영역, ffmpeg 필터 문자열.

탐지기는 InferencePort 뒤에 있고(실제 모델은 Model_Registry 라이선스 확인 후), 여기서는 그 결과만 다룬다.
"""

from __future__ import annotations

from dataclasses import dataclass

CLASSES = ("FACE", "LICENSE_PLATE", "VESSEL_ID_TEXT", "SCREEN_TEXT")
METHODS = ("BLUR", "MOSAIC", "FILL")


@dataclass(frozen=True)
class Region:
    """t0~t1초 동안 (x, y, w, h) 픽셀 영역. cls는 탐지 클래스 또는 MANUAL(검수자 추가)."""

    cls: str
    t0: float
    t1: float
    x: int
    y: int
    w: int
    h: int
    confidence: float = 1.0

    def clamp(self, width: int, height: int) -> Region | None:
        x, y = max(0, self.x), max(0, self.y)
        w, h = min(self.w, width - x), min(self.h, height - y)
        if w < 2 or h < 2 or self.t1 < self.t0:
            return None
        return Region(self.cls, self.t0, self.t1, x, y, w - w % 2, h - h % 2, self.confidence)


def track(regions: list[Region], max_gap_s: float, max_shift_px: int = 40) -> list[Region]:
    """추적 옵션 (요구사항 2.2): 같은 클래스·가까운 위치의 연속 탐지 사이 짧은 공백(깜빡임)을 이어 붙인다."""
    out: list[Region] = []
    for r in sorted(regions, key=lambda r: (r.cls, r.t0, r.x, r.y)):
        prev = next((p for p in reversed(out) if p.cls == r.cls and abs(p.x - r.x) <= max_shift_px
                     and abs(p.y - r.y) <= max_shift_px and 0 <= r.t0 - p.t1 <= max_gap_s), None)
        if prev is None:
            out.append(r)
            continue
        out.remove(prev)
        x0, y0 = min(prev.x, r.x), min(prev.y, r.y)
        x1, y1 = max(prev.x + prev.w, r.x + r.w), max(prev.y + prev.h, r.y + r.h)
        out.append(Region(r.cls, prev.t0, max(prev.t1, r.t1), x0, y0, x1 - x0, y1 - y0,
                          min(prev.confidence, r.confidence)))
    return sorted(out, key=lambda r: (r.t0, r.cls, r.x))


def filter_graph(regions: list[Region], method: str, width: int, height: int) -> str:
    """ffmpeg -filter_complex 문자열. 입력 [0:v] → 출력 [out]. 영역이 없으면 그대로 복사."""
    if method not in METHODS:
        raise ValueError(method)
    rs = [c for c in (r.clamp(width, height) for r in regions) if c is not None]
    if not rs:
        return "[0:v]null[out]"
    parts, cur = [], "[0:v]"
    for i, r in enumerate(rs):
        enable = f"between(t,{r.t0:.3f},{r.t1:.3f})"
        nxt = f"[v{i}]" if i < len(rs) - 1 else "[out]"
        if method == "FILL":
            parts.append(f"{cur}drawbox=x={r.x}:y={r.y}:w={r.w}:h={r.h}:color=black@1:t=fill:enable='{enable}'{nxt}")
        else:
            if method == "BLUR":
                effect = f"boxblur={max(2, min(r.w, r.h) // 8)}:1"
            else:  # MOSAIC: 크게 줄였다 늘려 블록으로
                bw, bh = max(1, r.w // 12), max(1, r.h // 12)
                effect = f"scale={bw}:{bh}:flags=area,scale={r.w}:{r.h}:flags=neighbor"
            parts.append(f"{cur}split[b{i}][c{i}];[c{i}]crop={r.w}:{r.h}:{r.x}:{r.y},{effect}[m{i}];"
                         f"[b{i}][m{i}]overlay={r.x}:{r.y}:enable='{enable}'{nxt}")
        cur = nxt
    return ";".join(parts)


def counts_by_class(regions: list[Region]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in regions:
        out[r.cls] = out.get(r.cls, 0) + 1
    return out
