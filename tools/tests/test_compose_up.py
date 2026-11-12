"""01번 요구사항 1.2·1.3: make up(tools/compose_up.sh)이 헬스체크 실패 서비스 이름을 출력하고 비0으로 끝나는지.

실제 Docker 대신 PATH 앞에 둔 가짜 docker가 `compose up`의 결과와 `compose ps` 출력을 흉내 낸다.
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
# 가짜 docker: compose up은 UP_EXIT로 끝나고, compose ps는 PS_OUT을 출력한다
for a in "$@"; do
  if [ "$a" = "up" ]; then exit "${UP_EXIT:-0}"; fi
  if [ "$a" = "ps" ]; then printf '%b' "${PS_OUT:-}"; exit 0; fi
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


if __name__ == "__main__":
    unittest.main()
