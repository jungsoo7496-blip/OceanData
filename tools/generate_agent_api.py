#!/usr/bin/env python3
"""06번: 고정 버전 oapi-codegen으로 계약 SDK를 생성하거나 변경 여부를 검사한다."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from bundle_agent_contract import bundle

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = "github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.8.2"
OUTPUT = ROOT / "agent/internal/contractapi/client.gen.go"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="소스를 바꾸지 않고 생성 결과 일치 여부 확인")
    args = parser.parse_args()
    go = shutil.which("go")
    if not go:
        print("Go가 없어 계약 SDK를 생성하지 못했습니다", file=sys.stderr)
        return 1
    scratch = ROOT / "var"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-codegen-", dir=scratch) as directory:
        temp = Path(directory)
        spec = temp / "openapi.bundle.json"
        document = bundle(ROOT / "contracts/openapi.yaml")
        # 에이전트는 외부 수신자용 웹훅 서버를 구현하지 않는다. 생성 대상은 발신 API 클라이언트다.
        # v2.8.2는 사용하지 않는 webhooks를 남기면서 매개변수 타입을 제거하므로 대상에서 제외한다.
        document.pop("webhooks", None)
        spec.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        generated = temp / "client.gen.go"
        result = subprocess.run(  # noqa: S603  # nosec B603
            [go, "run", GENERATOR, "--config", str(ROOT / "agent/oapi-codegen.yaml"),
             "--o", str(generated), str(spec)], cwd=ROOT / "agent", check=False, timeout=900)
        if result.returncode:
            return 1
        data = generated.read_bytes()
        if args.check:
            if not OUTPUT.exists() or OUTPUT.read_bytes().replace(b"\r\n", b"\n") != data:
                print("생성 SDK가 계약과 다릅니다. python tools/generate_agent_api.py를 실행하세요", file=sys.stderr)
                return 1
            print("Go 계약 SDK 재생성 일치 검사 통과")
        else:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_bytes(data)
            print("Go 계약 SDK 생성 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
