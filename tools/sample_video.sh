#!/usr/bin/env bash
# 합성 시험 영상 생성 (01번 요구사항 6.3~6.5). 사람 얼굴이 없는 testsrc2 패턴만 쓴다.
# 사용: tools/sample_video.sh [출력폴더] [길이초] [해상도] [코덱] [비트레이트]
#       LARGE_GB=10 tools/sample_video.sh   → 지정 크기의 대용량 시험 파일도 만든다
set -euo pipefail
OUT="${1:-var/samples}"; DUR="${2:-10}"; SIZE="${3:-1280x720}"; CODEC="${4:-libx264}"; RATE="${5:-2M}"
command -v ffmpeg >/dev/null || { echo "ffmpeg가 필요합니다" >&2; exit 2; }
mkdir -p "$OUT"
common=(-hide_banner -loglevel error -y -f lavfi -i "testsrc2=size=${SIZE}:rate=30" -t "$DUR"
        -c:v "$CODEC" -b:v "$RATE" -pix_fmt yuv420p)
# 촬영시각 메타데이터가 있는 영상 (creation_time)
ffmpeg "${common[@]}" -metadata creation_time="2026-11-01T01:02:03.000000Z" "$OUT/with_capture_time.mp4"
# 촬영시각 메타데이터가 없는 영상 (촬영시각 미상 처리 시험, domain.md 6장)
ffmpeg "${common[@]}" -map_metadata -1 -fflags +bitexact "$OUT/without_capture_time.mp4"
echo "생성: $OUT/with_capture_time.mp4, $OUT/without_capture_time.mp4"
if [ -n "${LARGE_GB:-}" ]; then
  # 재개·무결성 시험용 대용량 파일 (의사난수 내용, 디스크를 실제로 채운다)
  head -c "$((LARGE_GB * 1024 * 1024 * 1024))" /dev/urandom > "$OUT/large_${LARGE_GB}GB.bin"
  echo "생성: $OUT/large_${LARGE_GB}GB.bin"
fi
