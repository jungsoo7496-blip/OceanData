"""운영·개발 명령: python -m app.cli <명령>"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.container import build_container
from app.logging_setup import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-buckets", help="존별 버킷 생성 (01번 요구사항 1.5)")
    sub.add_parser("seed", help="시험 데이터 생성 (01번 요구사항 6)")
    sub.add_parser("verify-audit", help="감사 로그 해시 체인 검증 (02번 요구사항 7.4)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    c = build_container(settings)
    if args.cmd == "init-buckets":
        for zone, bucket in settings.buckets.items():
            c.storage.ensure_bucket(bucket)
            print(f"{zone}: {bucket} 준비됨")
        return 0
    if args.cmd == "seed":
        from app.seed import seed

        with c.sessionmaker.begin() as db:
            seed(db)
        print("시험 데이터 준비됨 (기관 5, 사용자 9, 사업 2, 장비 5, 회차 2)")
        return 0
    if args.cmd == "verify-audit":
        report = c.audit.verify_chain()
        if report.ok:
            print(f"감사 로그 체인 정상 ({report.checked}행)")
            return 0
        print(f"감사 로그 체인 끊김: seq={report.broken_at_seq} audit_id={report.broken_audit_id} "
              f"(앞의 {report.checked}행은 정상)", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
