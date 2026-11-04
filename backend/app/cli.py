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
    sp = sub.add_parser("set-policy", help="수집기 정책 등록 (device-policy.schema.json 검증, 새 버전으로 저장)")
    sp.add_argument("device_id")
    sp.add_argument("policy_file")
    rr = sub.add_parser("retention", help="보존 만료 dry-run (--execute면 실제 삭제, 운영자 확인 후)")
    rr.add_argument("--execute", action="store_true")
    sm = sub.add_parser("set-mapping", help="관측값 매핑 프로필 새 버전 저장 (감사, 화면은 09번)")
    sm.add_argument("device_id")
    sm.add_argument("mapping_file")
    vi = sub.add_parser("verify-integrity", help="무결성 로그 체인·표본 객체 해시 검증 (05번 요구사항 5.3)")
    vi.add_argument("--sample", type=int, default=None)
    ec = sub.add_parser("enrollment-code", help="운영자 대리 등록 코드 발급 (감사 기록, 화면은 09번)")
    ec.add_argument("device_id")
    ag = sub.add_parser("agreement", help="기관 약정 기록 (13번 공유 조건 '2단 약정 유효'용, 운영자. API는 03번)")
    ag.add_argument("organization_id")
    ag.add_argument("--tier", default="TIER2", choices=["TIER1", "TIER2"])
    ag.add_argument("--reference", required=True)
    ag.add_argument("--valid-until", default=None)
    ag.add_argument("--purposes", default="", help="쉼표로 구분한 purpose 코드")
    sp2 = sub.add_parser("share-processed", help="공유 심사 PROCESSING → QUALITY_REVIEW (14번 대신, 개발용)")
    sp2.add_argument("asset_id")
    sub.add_parser("watch", help="감시 규칙을 한 번 평가하고 열린 경보를 출력 (11번, 워커가 주기적으로 한다)")
    hw = sub.add_parser("hardware-signal", help="모의 하드웨어 신호 넣기 (11번 요구사항 7.2, 실제 어댑터 전 시험용)")
    hw.add_argument("--host", default="storage-01")
    hw.add_argument("--kind", default="DISK", choices=["DISK", "SMART", "TEMPERATURE", "POWER", "UPS", "OTHER"])
    hw.add_argument("--component", default="")
    hw.add_argument("--status", default="WARNING", choices=["OK", "WARNING", "CRITICAL"])
    hw.add_argument("--value", type=float, default=None)
    hw.add_argument("--unit", default=None)
    sub.add_parser("backup", help="계층형 백업 한 회차 (08번 요구사항 6, ADR-0021)")
    rt = sub.add_parser("restore-test", help="최근 백업을 임시 DB·위치에 복원해 행 수·해시 대조 (T10-a)")
    rt.add_argument("--run", default=None, help="백업 회차 ID (기본: 최근 성공)")
    rt.add_argument("--sample", type=int, default=None, help="객체는 앞에서 N개만 (기본: 전부)")
    rt.add_argument("--out", default=None, help="결과 JSON 파일")
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
    if args.cmd == "set-policy":
        return _set_policy(c, args.device_id, args.policy_file)
    if args.cmd == "enrollment-code":
        return _enrollment_code(c, args.device_id)
    if args.cmd == "retention":
        import json as _json

        from app.services import retention as _ret

        with c.sessionmaker.begin() as db:
            if args.execute:
                certs = _ret.execute_expired(c, db, c.clock.now())
                print(f"삭제 증명 {len(certs)}건: " + ", ".join(x.certificate_id for x in certs))
            else:
                print(_json.dumps(_ret.dry_run(db, c.clock.now()), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "set-mapping":
        return _set_mapping(c, args.device_id, args.mapping_file)
    if args.cmd == "backup":
        import json

        from app.db.models import BackupRun
        from app.services import backup

        rid = backup.run(c)
        with c.sessionmaker() as db:
            r = db.get(BackupRun, rid)
            print(json.dumps(backup.run_view(r), ensure_ascii=False, indent=1) if r else rid)
            return 0 if r is not None and r.status == "SUCCEEDED" else 1
    if args.cmd == "restore-test":
        import json
        from pathlib import Path

        from app.services import backup

        rtr = backup.restore_test(c, args.run, args.sample)
        text = json.dumps(rtr, ensure_ascii=False, indent=1)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
        print(text)
        return 0 if rtr["ok"] else 1
    if args.cmd == "verify-integrity":
        from app.services.integrity import anchor_previous_days, verify_integrity

        anchor_previous_days(c)
        rep = verify_integrity(c, args.sample)
        print(f"체인 {rep.checked_entries}건 확인, 표본 {rep.sampled}건 재계산")
        if rep.broken_at_seq is not None:
            print(f"체인 끊김: seq={rep.broken_at_seq}", file=sys.stderr)
        for a in rep.mismatched_assets:
            print(f"해시 불일치: {a}", file=sys.stderr)
        for a in rep.missing_objects:
            print(f"객체 없음: {a}", file=sys.stderr)
        return 0 if rep.ok else 1
    if args.cmd == "agreement":
        from app.db.models import Agreement
        from app.domain.ids import new_id
        from app.domain.normalize import parse_rfc3339

        now = c.clock.now()
        with c.sessionmaker.begin() as db:
            row = Agreement(agreement_id=new_id("agreement"), organization_id=args.organization_id, tier=args.tier,
                            reference=args.reference, valid_from=now,
                            valid_until=parse_rfc3339(args.valid_until) if args.valid_until else None,
                            allowed_purposes=[x for x in args.purposes.split(",") if x], status="ACTIVE",
                            created_by="cli", created_at=now)
            db.add(row)
        c.audit.record("AGREEMENT_RECORDED", actor_kind="SYSTEM", actor_id="cli", organization_id=args.organization_id,
                       target_type="Agreement", target_id=row.agreement_id, details={"tier": args.tier})
        print(row.agreement_id)
        return 0
    if args.cmd == "share-processed":
        from app.db.models import Asset
        from app.services import sharing

        with c.sessionmaker.begin() as db:
            target = db.get(Asset, args.asset_id, with_for_update=True)
            if target is None:
                print("자산이 없습니다", file=sys.stderr)
                return 1
            sharing.mark_processed(c, db, target)
        print(f"{args.asset_id}: QUALITY_REVIEW")
        return 0
    if args.cmd == "watch":
        from app.services import monitoring

        with c.sessionmaker.begin() as db:
            res = monitoring.evaluate(c, db)
        print(f"새 경보 {len(res.opened)}건, 재알림 {len(res.reminded)}건, 해제 {len(res.resolved)}건")
        for code, n in sorted(res.active_by_rule.items()):
            print(f"  {code}: {n}")
        return 0
    if args.cmd == "hardware-signal":
        from app.api.monitoring import Signal, store_signals
        from app.timefmt import iso

        sig = Signal(host=args.host, kind=args.kind, component=args.component or None, status=args.status,
                     value=args.value, unit=args.unit, detail=None, observed_at=iso(c.clock.now()) or "")
        with c.sessionmaker.begin() as db:
            store_signals(db, [sig], c.clock.now())
        print(f"신호 저장: {args.host} {args.kind} {args.status} (다음 감시 평가 때 반영)")
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


def _set_policy(c, device_id: str, policy_file: str) -> int:  # type: ignore[no-untyped-def]
    import json
    from pathlib import Path

    from sqlalchemy import func, select

    from app.db.models import Device, DevicePolicy
    from app.services.contracts import schema_problems

    doc = json.loads(Path(policy_file).read_text(encoding="utf-8"))
    problems = schema_problems("device-policy", doc)
    if problems:
        for p in problems:
            print(f"{p.pointer}: {p.message}", file=sys.stderr)
        return 1
    with c.sessionmaker.begin() as db:
        dev = db.get(Device, device_id)
        if dev is None or dev.kind != "COLLECTOR" or doc["collector_device_id"] != device_id:
            print("수집기 장비가 아니거나 collector_device_id가 다릅니다", file=sys.stderr)
            return 1
        for src in doc["sources"]:  # 정책의 원천 장비는 그 수집기에 매핑된 같은 기관 장비여야 한다
            s_dev = db.get(Device, src["device_id"])
            if s_dev is None or s_dev.organization_id != dev.organization_id or \
                    (s_dev.collector_device_id != device_id and s_dev.device_id != device_id):
                print(f"매핑되지 않은 원천 장비: {src['device_id']}", file=sys.stderr)
                return 1
        ver = (db.execute(select(func.max(DevicePolicy.version)).where(DevicePolicy.device_id == device_id))
               .scalar() or 0) + 1
        db.add(DevicePolicy(device_id=device_id, version=ver, organization_id=dev.organization_id, policy=doc,
                            created_by="cli", created_at=c.clock.now()))
    c.audit.record("POLICY_CHANGED", actor_kind="SYSTEM", actor_id="cli", organization_id=dev.organization_id,
                   target_type="Device", target_id=device_id, details={"version": ver,
                                                                       "policy_version": doc["policy_version"]})
    print(f"정책 저장: {device_id} v{ver} ({doc['policy_version']})")
    return 0


def _set_mapping(c, device_id: str, mapping_file: str) -> int:  # type: ignore[no-untyped-def]
    import json
    from pathlib import Path

    from pydantic import ValidationError

    from app.api.observations import MappingBody
    from app.db.models import Device, ObservationMapping
    from app.services.observations import current_mapping, next_mapping_version

    try:
        body = MappingBody.model_validate(json.loads(Path(mapping_file).read_text(encoding="utf-8")))
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    with c.sessionmaker.begin() as db:
        dev = db.get(Device, device_id)
        if dev is None:
            print("장비가 없습니다", file=sys.stderr)
            return 1
        before = current_mapping(db, device_id)
        m = ObservationMapping(device_id=device_id, version=next_mapping_version(db, device_id),
                               organization_id=dev.organization_id, mapping=body.model_dump(exclude_none=True),
                               created_by="cli", created_at=c.clock.now())
        db.add(m)
        ver, org = m.version, dev.organization_id
    c.audit.record("OBSERVATION_MAPPING_CHANGED", actor_kind="SYSTEM", actor_id="cli", organization_id=org,
                   target_type="Device", target_id=device_id,
                   details={"before": before.mapping if before else None, "version": ver})
    print(f"매핑 저장: {device_id} v{ver}")
    return 0


def _enrollment_code(c, device_id: str) -> int:  # type: ignore[no-untyped-def]
    import secrets
    from datetime import timedelta

    from app.db.models import Device, EnrollmentCode
    from app.services.tokens import hash_secret

    code = secrets.token_urlsafe(18)
    now = c.clock.now()
    with c.sessionmaker.begin() as db:
        dev = db.get(Device, device_id)
        if dev is None or dev.kind != "COLLECTOR":
            print("수집기 장비가 아닙니다", file=sys.stderr)
            return 1
        db.add(EnrollmentCode(code_hash=hash_secret(code), device_id=device_id, organization_id=dev.organization_id,
                              expires_at=now + timedelta(seconds=c.settings.enrollment_code_ttl_s),
                              created_by="cli", created_at=now))
    c.audit.record("ENROLLMENT_CODE_ISSUED", actor_kind="SYSTEM", actor_id="cli", organization_id=dev.organization_id,
                   target_type="Device", target_id=device_id)
    print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
