"""처리량 벤치마크 (05번 요구사항 9.2, T16): python -m app.bench [--seconds 30] [--size 1920x1080] [--out 파일]

합성 영상(testsrc2)을 만들어 파이프라인과 같은 ffmpeg 인자로 메타 추출·프록시·썸네일을 실행하고
단계별 시간·fps를 JSON으로 남긴다.
GPU 워크스테이션에서는 GPU_ENABLED=true로 하드웨어 인코더를 함께 잰다. 수치는 코드에 가정하지 않는다 (HW-8).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess  # nosec B404
import tempfile
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from app.config import Settings
from app.domain.deid import Region, filter_graph
from app.services.pipeline import _run, proxy_args, thumbnail_args, tool_version


def defaults() -> SimpleNamespace:
    f = Settings.model_fields
    vals = {k: os.environ.get(k.upper(), f[k].default) for k in (
        "ffmpeg_path", "ffprobe_path", "proxy_short_side", "proxy_max_bitrate", "proxy_hw_encoder",
        "thumbnail_interval_s", "thumbnail_max_frames", "ffmpeg_timeout_s")}
    vals["gpu_enabled"] = os.environ.get("GPU_ENABLED", "false").lower() == "true"
    for k in ("proxy_short_side", "thumbnail_max_frames", "ffmpeg_timeout_s"):
        vals[k] = int(vals[k])
    vals["thumbnail_interval_s"] = float(vals["thumbnail_interval_s"])
    return SimpleNamespace(**vals)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", default="var/bench.json")
    a = ap.parse_args(argv)
    s = defaults()
    frames = a.seconds * a.fps
    report: dict = {"created_at": datetime.now(UTC).isoformat(), "host": platform.node(),
                    "cpu_count": os.cpu_count(), "ffmpeg": tool_version(s.ffmpeg_path), "gpu_enabled": s.gpu_enabled,
                    "input": {"seconds": a.seconds, "size": a.size, "fps": a.fps, "frames": frames}, "steps": {}}
    with tempfile.TemporaryDirectory(prefix="marine-bench-") as tmp:
        src = os.path.join(tmp, "src.mp4")
        subprocess.run([s.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",  # noqa: S603
                        f"testsrc2=size={a.size}:rate={a.fps}", "-t", str(a.seconds), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", src], check=True)  # nosec B603
        steps = {
            "META": [s.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src],
            "PROXY_CPU": proxy_args(s, src, os.path.join(tmp, "p.mp4"), "libx264"),
            "THUMBNAIL": thumbnail_args(s, src, os.path.join(tmp, "t.jpg"), float(a.seconds)),
        }
        # 14번 비식별 가림 렌더링 (탐지 시간 제외 — 탐지 모델 확정 후 모델별로 더한다). 화면 1/4 영역 3곳
        w, h = (int(x) for x in a.size.split("x"))
        regions = [Region("FACE", 0, float(a.seconds), w // 8 + i * w // 4, h // 4, w // 6, h // 4) for i in range(3)]
        for method in ("BLUR", "MOSAIC"):
            steps[f"DEID_{method}_CPU"] = [s.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                                           "-filter_complex", filter_graph(regions, method, w, h), "-map", "[out]",
                                           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                                           os.path.join(tmp, f"d{method}.mp4")]
        if s.gpu_enabled and s.proxy_hw_encoder:
            steps["PROXY_GPU"] = proxy_args(s, src, os.path.join(tmp, "pg.mp4"), s.proxy_hw_encoder)
        for name, args in steps.items():
            t0 = time.perf_counter()
            r = _run(args, s.ffmpeg_timeout_s)
            el = time.perf_counter() - t0
            report["steps"][name] = {"ok": r.returncode == 0, "seconds": round(el, 3),
                                     "frames_per_s": round(frames / el, 1) if el > 0 else None,
                                     "realtime_factor": round(a.seconds / el, 2) if el > 0 else None}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report["steps"], ensure_ascii=False))
    return 0 if all(v["ok"] for v in report["steps"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
