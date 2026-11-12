#!/usr/bin/env python3
"""T02-g 실제 브라우저 업로드 재개 시험 → 공통 수용시험 JSON Lines.

전용 Compose DB·모의 S3·합성 서명 IdP 어댑터를 만들고 실제 React·API 멀티파트 경로를 시험한다.
실패·환경 준비 실패도 JSON Lines에 남기며 성공으로 처리하지 않는다. 제품 서버 설정은 바꾸지 않는다.

    python tools/web_acceptance.py --out var/acceptance-results.jsonl

준비: web에서 npm ci, node node_modules/playwright-core/cli.js install chromium
설치된 Chrome을 쓰려면 E2E_BROWSER_CHANNEL=chrome. PLAYWRIGHT_BROWSERS_PATH도 사용할 수 있다.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import subprocess  # nosec B404 - 고정된 시험 명령을 실행한다
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_ID = "web/e2e/upload-resume.mjs::T02_g_tab_close_and_resume"


def scrub(text: str) -> str:
    text = re.sub(r"https?://[^\s\"'<>]+", lambda m: re.split(r"[?#]", m.group(0))[0], text)
    text = re.sub(r"([^\s?\"'<>]+)\?[^\s\"'<>]+", r"\1", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.I)
    return re.sub(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def record(out: Path, outcome: str, log: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"nodeid": NODE_ID, "covers": ["T02-g"], "outcome": outcome, "log": scrub(log),
                            "reproduce": "python tools/web_acceptance.py --out var/acceptance-results.jsonl"},
                           ensure_ascii=False) + "\n")


def run(out: Path, timeout: int) -> int:
    docker, node = shutil.which("docker"), shutil.which("node")
    if not docker or not node:
        record(out, "failed", "Docker·Node가 없어 브라우저 수용시험을 실행하지 못했습니다")
        print("웹 수용시험 실패: Docker·Node가 필요합니다")
        return 1
    if not (ROOT / "web/node_modules/playwright-core").is_dir():
        record(out, "failed", "web 의존성이 없습니다. cd web && npm ci 후 다시 실행하세요")
        print("웹 수용시험 실패: 먼저 cd web && npm ci를 실행하세요")
        return 1
    run_id = secrets.token_hex(5)
    project = f"marine-web-e2e-{run_id}"
    run_dir = ROOT / "var" / project
    run_dir.mkdir(parents=True)
    api_port, s3_port = free_port(), free_port()
    while s3_port == api_port:
        s3_port = free_port()
    env = {**os.environ, "E2E_RESULTS_DIR": str(run_dir.resolve()), "E2E_API_PORT": str(api_port),
           "E2E_S3_PORT": str(s3_port), "PYTHONUNBUFFERED": "1"}
    if hasattr(os, "getuid"):
        env.update(E2E_UID=str(os.getuid()), E2E_GID=str(os.getgid()))
    compose = [docker, "compose", "-p", project, "-f", str(ROOT / "web/e2e/docker-compose.yml")]
    log_path = run_dir / "run.log"
    session = run_dir / "session.json"
    node_result = run_dir / "result.jsonl"
    api_url = f"http://127.0.0.1:{env['E2E_API_PORT']}"
    error = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            prepared = subprocess.run(  # noqa: S603  # nosec B603
                [*compose, "up", "-d", "--build"], cwd=ROOT, env=env, stdout=log,
                stderr=subprocess.STDOUT, timeout=timeout, check=False)
            if prepared.returncode:
                raise RuntimeError("전용 브라우저 시험 환경을 준비하지 못했습니다")
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if session.exists():
                    try:
                        # 이 도구가 고른 루프백 포트만 조회한다.
                        with urllib.request.urlopen(api_url + "/healthz", timeout=2) as response:  # noqa: S310  # nosec B310
                            if response.status == 200:
                                break
                    except (OSError, http.client.HTTPException):
                        # 기동 중인 API는 연결을 받았다가 끊기도 한다(ConnectionResetError 등). 기한까지 다시 묻는다.
                        pass
                time.sleep(0.25)
            else:
                raise TimeoutError("전용 브라우저 시험 API 기동 시간 초과")
            browser_env = {**env, "E2E_API_URL": api_url, "E2E_SESSION_FILE": str(session),
                           "E2E_OUTPUT": str(node_result)}
            result = subprocess.run(  # noqa: S603  # nosec B603
                [node, str(ROOT / "web/e2e/upload-resume.mjs")], cwd=ROOT,
                env=browser_env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
            if result.returncode:
                error = "브라우저 업로드 재개 시험 실패"
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        error = f"웹 수용시험 실행 실패: {type(exc).__name__}: {exc}"
    finally:
        # 고유 프로젝트의 로그·컨테이너·볼륨만 다룬다. 실행 중인 다른 프로젝트는 건드리지 않는다.
        try:
            logs = subprocess.run(  # noqa: S603  # nosec B603
                [*compose, "logs", "--no-color", "api"], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(scrub(logs.stdout + logs.stderr))
            cleaned = subprocess.run(  # noqa: S603  # nosec B603
                [*compose, "down", "--volumes", "--remove-orphans"], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
            if cleaned.returncode:
                error = (error + "; " if error else "") + "전용 시험 환경 정리 실패"
        except (OSError, subprocess.SubprocessError) as exc:
            error = (error + "; " if error else "") + f"전용 시험 환경 정리 실패: {type(exc).__name__}"
        # 시험 토큰은 결과 산출물에 보관하지 않는다.
        session.unlink(missing_ok=True)
    if node_result.exists():
        try:
            rows = [json.loads(line) for line in node_result.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            rows = []
        if len(rows) == 1 and rows[0].get("nodeid") == NODE_ID and rows[0].get("outcome") in ("passed", "failed"):
            row = rows[0]
            if error:
                row.update(outcome="failed", log=scrub(f"{row.get('log', '')}\n{error}"))
            row["log_path"] = log_path.relative_to(ROOT).as_posix()
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            ok = row["outcome"] == "passed"
            print(f"웹 수용시험 T02-g {'통과' if ok else '실패'} — {log_path.relative_to(ROOT)}")
            return 0 if ok else 1
        error = "브라우저 결과가 누락되었거나 올바르지 않습니다"
    tail = scrub(log_path.read_text(encoding="utf-8", errors="replace")[-2000:]) if log_path.exists() else ""
    record(out, "failed", f"{error or '브라우저 시험 결과가 없습니다'}\n로그: {log_path.relative_to(ROOT)}\n{tail}")
    print(f"웹 수용시험 T02-g 실패 — {log_path.relative_to(ROOT)}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "var/acceptance-results.jsonl")
    parser.add_argument("--timeout", type=int, default=600, help="환경 준비·브라우저 시험의 각 최대 시간(초)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout은 양수여야 합니다")
    return run(args.out.resolve(), args.timeout)


if __name__ == "__main__":
    sys.exit(main())
