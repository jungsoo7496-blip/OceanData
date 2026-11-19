"""01번 요구사항 1.2·1.3: make up(tools/compose_up.sh)이 헬스체크 실패 서비스 이름을 출력하고 비0으로 끝나는지.

실제 Docker 대신 PATH 앞에 둔 가짜 docker가 `compose up`의 결과와 `compose ps`·`compose config` 출력을 흉내 낸다.
통합 검토 CLIPS-1: 기본 워커가 clip_export를 빼는데 클립 전용 워커(프로필 clip-worker)가 꺼져 있으면 경고한다 (ADR-0069).
스크립트는 임시 폴더로 복사해 .env 유무를 시험마다 정한다 (저장소의 .env를 건드리지 않는다).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "compose_up.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
# 가짜 docker: compose up은 UP_EXIT로 끝나고, compose ps는 PS_OUT을, compose config --services는 SERVICES_OUT을,
# 그 밖의 compose config(예: config worker)는 CONFIG_OUT을 출력한다
for a in "$@"; do
  if [ "$a" = "up" ]; then exit "${UP_EXIT:-0}"; fi
  if [ "$a" = "ps" ]; then printf '%b' "${PS_OUT:-}"; exit 0; fi
  if [ "$a" = "config" ]; then
    case " $* " in
      *" --services "*) printf '%b' "${SERVICES_OUT:-}" ;;
      *) printf '%b' "${CONFIG_OUT:-}" ;;
    esac
    exit 0
  fi
done
exit 0
"""


@unittest.skipIf(shutil.which("bash") is None, "bash 필요")
class ComposeUpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="compose-up-"))
        (self.tmp / "tools").mkdir()
        shutil.copy(SCRIPT, self.tmp / "tools" / "compose_up.sh")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake = bindir / "docker"
        fake.write_text(FAKE_DOCKER, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}", "UP_TIMEOUT_S": "5"}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_up(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.tmp / "tools" / "compose_up.sh")], capture_output=True, text=True,
                              env={**self.env, **env}, timeout=30, check=False)

    def test_missing_env_file(self) -> None:
        r = self.run_up()
        self.assertEqual(r.returncode, 2)
        self.assertIn(".env", r.stderr)

    def test_unhealthy_services_are_named_and_exit_nonzero(self) -> None:
        (self.tmp / ".env").write_text("X=1\n", encoding="utf-8")
        ps = ("postgres|running|healthy|0\\n" "api|running|unhealthy|0\\n" "keycloak|running|starting|0\\n"
              "migrate|exited||0\\n" "worker|exited||1\\n" "web|running||0\\n")
        r = self.run_up(UP_EXIT="1", PS_OUT=ps)
        self.assertEqual(r.returncode, 1, r.stderr)
        lines = [x.strip() for x in r.stderr.splitlines() if x.strip().startswith("- ")]
        self.assertEqual(lines, ["- api (running unhealthy)", "- keycloak (running starting)", "- worker (exited )"])
        self.assertNotIn("postgres", r.stderr)  # 정상 서비스와 정상 종료한 migrate는 나열하지 않는다
        self.assertNotIn("migrate", "\n".join(lines))

    def test_success_when_all_healthy(self) -> None:
        (self.tmp / ".env").write_text("X=1\n", encoding="utf-8")
        r = self.run_up(UP_EXIT="0")
        self.assertEqual(r.returncode, 0)
        self.assertIn("개발 환경 준비됨", r.stdout)

    def _worker_config(self, include: str, exclude: str) -> str:
        return ("services:\n  worker:\n    environment:\n"
                f"      WORKER_EXCLUDE_JOB_TYPES: {exclude}\n      WORKER_JOB_TYPES: {include}\n")

    def test_warns_when_clips_are_excluded_without_clip_worker(self) -> None:
        """runbook 07대로 .env에 제외만 두고 프로필을 명령줄로 켰다면, 다음 make up에서 worker-clips는 오르지 않는다."""
        (self.tmp / ".env").write_text("WORKER_EXCLUDE_JOB_TYPES=clip_export\n", encoding="utf-8")
        services = "postgres\nworker\napi\n"
        for include, exclude in (('""', "clip_export"), ('""', '"media_probe,clip_export"'),
                                 ("integrity_verify,media_probe", '""')):
            with self.subTest(include=include, exclude=exclude):
                r = self.run_up(UP_EXIT="0", CONFIG_OUT=self._worker_config(include, exclude), SERVICES_OUT=services)
                self.assertEqual(r.returncode, 0, r.stderr)  # 경고만 한다 (스택은 올라왔다)
                self.assertIn("worker-clips", r.stderr)
                self.assertIn("COMPOSE_PROFILES=clip-worker", r.stderr)
        r = self.run_up(UP_EXIT="1", PS_OUT="worker|running|healthy|0\n",
                        CONFIG_OUT=self._worker_config('""', "clip_export"), SERVICES_OUT=services)
        self.assertEqual(r.returncode, 1)
        self.assertIn("COMPOSE_PROFILES=clip-worker", r.stderr)  # 헬스체크 실패 때도 함께 알린다

    def test_no_clip_warning_when_clip_worker_runs_or_default_worker_takes_clips(self) -> None:
        (self.tmp / ".env").write_text("X=1\n", encoding="utf-8")
        cases = (("clip_export", "postgres\nworker\nworker-clips\n"),  # 전용 워커 프로필이 켜져 있다
                 ('""', "postgres\nworker\n"),  # 기본: 기본 워커가 클립도 가져간다
                 ("noop", "postgres\nworker\n"))
        for exclude, services in cases:
            with self.subTest(exclude=exclude):
                r = self.run_up(UP_EXIT="0", CONFIG_OUT=self._worker_config('""', exclude), SERVICES_OUT=services)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("worker-clips", r.stderr)
        r = self.run_up(UP_EXIT="0", CONFIG_OUT=self._worker_config('"clip_export,noop"', '""'),
                        SERVICES_OUT="worker\n")
        self.assertNotIn("worker-clips", r.stderr)  # 기본 워커가 고른 유형에 clip_export가 있다

if __name__ == "__main__":
    unittest.main()
