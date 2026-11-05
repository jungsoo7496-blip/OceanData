"""계층형 백업과 복원 시험 (08번 요구사항 6·7, ADR-0021).

- 대상: backup_class=PROTECTED · EVIDENCE/DATASET 존 · variant=DEID 자산 + EVIDENCE·DATASET 버킷의 나머지 객체
  (보고서 스냅샷·매니페스트). backup_class=NONE인 RAW는 넣지 않는다 (domain.md 9장).
- 객체는 별도 S3(설정)에 `objects/<존>/<키>`로 증분 복사하고 백업 쪽을 다시 읽어 sha256을 확인한다.
- DB는 같은 스냅샷에서 표별 행 수를 세고 `pg_dump --snapshot`으로 덤프한다 → 복원본 행 수와 정확히 같아야 한다.
- 복원 시험: 임시 DB에 pg_restore → 행 수·감사 로그 체인, 객체는 임시 위치에 복원해 sha256과 원본 기록을 대조.

객체 복사(`_copy_objects`)와 DB 덤프(`_dump_db`)는 rclone·pgBackRest PoC 결과에 따라 바꿀 수 있게 따로 둔다.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess  # nosec B404 - 덤프·복원 도구를 고정 인자로 실행
import tempfile
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, func, or_, select, table, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from app.db.models import Asset, BackupRun, User
from app.domain.ids import new_id
from app.ports.notifier import Notification
from app.services.audit import AuditLogger
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container
    from app.ports.storage import StoragePort

log = logging.getLogger(__name__)
NOT_FROM_ASSETS = ("EVIDENCE", "DATASET")  # 자산이 아닌 객체도 통째로 백업하는 존


class BackupBusy(Exception):
    pass


def is_backup_target(a: Asset) -> bool:
    """독립 백업 대상인가 (요구사항 6.1·6.2, 삭제 증명의 백업 소멸일 판단과 같은 술어)."""
    return a.backup_class == "PROTECTED" or a.storage_zone in NOT_FROM_ASSETS or a.variant == "DEID"


def backup_storage(c: Container) -> StoragePort:
    s = c.settings
    if not s.backup_s3_endpoint_url:
        return c.storage  # 개발용: 같은 S3의 backup 버킷. 운영은 다른 장비·위치의 S3를 설정한다
    from app.adapters.s3_storage import S3Storage

    upd: dict[str, Any] = {"s3_endpoint_url": s.backup_s3_endpoint_url, "s3_public_endpoint_url": None}
    if s.backup_s3_access_key_id:
        upd["s3_access_key_id"] = s.backup_s3_access_key_id
    if s.backup_s3_secret_access_key:
        upd["s3_secret_access_key"] = s.backup_s3_secret_access_key
    return S3Storage(s.model_copy(update=upd))


def plan(c: Container, db: Session) -> list[dict[str, Any]]:
    """백업할 객체 목록. 자산 객체는 기록된 sha256으로 검증한다."""
    items: dict[tuple[str, str], dict[str, Any]] = {}
    q = select(Asset).where(Asset.deleted_at.is_(None), Asset.has_binary.is_(True), Asset.storage_key.is_not(None),
                            Asset.storage_zone.is_not(None), Asset.storage_zone != "QUARANTINE",
                            or_(Asset.backup_class == "PROTECTED", Asset.storage_zone.in_(NOT_FROM_ASSETS),
                                Asset.variant == "DEID"))
    for a in db.execute(q.order_by(Asset.asset_id)).scalars():
        zone, key = str(a.storage_zone), str(a.storage_key)
        items[(zone, key)] = {"zone": zone, "key": key, "sha256": a.checksum_sha256, "size": int(a.byte_size or 0),
                              "asset_id": a.asset_id}
    for zone in NOT_FROM_ASSETS:
        for o in c.storage.list_objects(c.settings.buckets[zone], ""):
            items.setdefault((zone, o.key), {"zone": zone, "key": o.key, "sha256": None, "size": o.size,
                                             "asset_id": None, "etag": o.etag})
    return [items[k] for k in sorted(items)]


def _download(storage: StoragePort, bucket: str, key: str) -> tuple[str, str, int]:
    fd, path = tempfile.mkstemp(prefix="bkp-")
    h, n = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in storage.iter_object(bucket, key):
                h.update(chunk)
                n += len(chunk)
                f.write(chunk)
    except Exception:
        os.unlink(path)
        raise
    return path, h.hexdigest(), n


def _sha_of(storage: StoragePort, bucket: str, key: str) -> str:
    h = hashlib.sha256()
    for chunk in storage.iter_object(bucket, key):
        h.update(chunk)
    return h.hexdigest()


def _copy_objects(c: Container, dst: StoragePort, items: list[dict[str, Any]], prev: dict[str, dict[str, Any]]
                  ) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, int]]:
    """증분 복사 + 복사 뒤 백업 쪽 sha256 확인. 원본 해시가 기록과 다르면 복사하지 않고 실패로 남긴다."""
    bb = c.settings.backup_bucket
    out: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    st = {"copied": 0, "skipped": 0, "copied_bytes": 0}
    for it in items:
        bkey = f"objects/{it['zone']}/{it['key']}"
        old = prev.get(bkey)
        same = old is not None and (old.get("sha256") == it["sha256"] if it["sha256"]
                                    else (old.get("etag") == it.get("etag") and old.get("size") == it["size"]))
        if same and old is not None:
            head = dst.head_object(bb, bkey)
            if head is not None and head.size == old.get("size"):
                out.append({**it, "backup_key": bkey, "sha256": old["sha256"]})
                st["skipped"] += 1
                continue
        try:
            path, sha, size = _download(c.storage, c.settings.buckets[it["zone"]], it["key"])
        except Exception as exc:  # noqa: BLE001 - 객체 하나의 실패는 기록하고 계속
            failed.append({"key": bkey, "reason": f"원본을 읽지 못함 ({type(exc).__name__})"})
            continue
        try:
            if it["sha256"] and sha != it["sha256"]:
                failed.append({"key": bkey, "reason": "원본 sha256이 기록과 다름 (손상 의심, 복사하지 않음)"})
                continue
            dst.upload_file(bb, bkey, path)
        finally:
            os.unlink(path)
        if _sha_of(dst, bb, bkey) != sha:
            failed.append({"key": bkey, "reason": "백업 사본 sha256 불일치"})
            continue
        out.append({**it, "backup_key": bkey, "sha256": sha, "size": size})
        st["copied"] += 1
        st["copied_bytes"] += size
    return out, failed, st


def _db_url(c: Container) -> URL:
    raw = c.settings.backup_database_url or c.settings.database_url
    return make_url(raw.get_secret_value())


def _pg_args(url: URL) -> tuple[list[str], dict[str, str]]:
    args = ["-h", url.host or "localhost", "-p", str(url.port or 5432), "-U", url.username or "postgres"]
    return args, {**os.environ, "PGPASSWORD": url.password or ""}


def _check_pg_version(binary: str, server_major: int, tool: str) -> None:
    """상위 버전 덤프를 하위 서버에 복원하지 않도록 도구·서버 주버전을 맞춘다."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [binary, "--version"], check=True, capture_output=True, text=True, timeout=10)
    match = re.search(r"\(PostgreSQL\)\s+(\d+)(?:\D|$)", result.stdout)
    if match is None:
        raise ValueError(f"{tool} 주버전을 확인하지 못했습니다")
    major = int(match.group(1))
    if major != server_major:
        raise ValueError(f"{tool} 주버전 {major}이 DB 서버 주버전 {server_major}과 다릅니다. "
                         "PG_DUMP_BIN·PG_RESTORE_BIN을 서버와 같은 주버전으로 설정하세요")


def _tables(conn: Any) -> list[str]:
    return list(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_type = 'BASE TABLE' ORDER BY table_name")).scalars())


def _counts(conn: Any) -> dict[str, int]:
    return {t: int(conn.execute(select(func.count()).select_from(table(t, schema="public"))).scalar_one())
            for t in _tables(conn)}


def _dump_db(c: Container, dst: StoragePort, run_id: str) -> tuple[str, str, dict[str, int]]:
    """같은 스냅샷에서 표별 행 수 + pg_dump(사용자 정의 형식). 덤프 키·sha256·행 수."""
    url = _db_url(c)
    eng = create_engine(url)
    fd, path = tempfile.mkstemp(prefix="bkp-db-", suffix=".dump")
    os.close(fd)
    try:
        with eng.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
            major = int(conn.execute(text("SHOW server_version_num")).scalar_one()) // 10000
            _check_pg_version(c.settings.pg_dump_bin, major, "pg_dump")
            snap = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
            counts = _counts(conn)
            args, env = _pg_args(url)
            cmd = [c.settings.pg_dump_bin, "-Fc", f"--snapshot={snap}", *args, "-f", path, url.database or ""]
            subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=6 * 3600)  # noqa: S603  # nosec B603
        key = f"runs/{run_id}/db.dump"
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(chunk)
        dst.upload_file(c.settings.backup_bucket, key, path)
        return key, h.hexdigest(), counts
    finally:
        os.unlink(path)
        eng.dispose()


def _previous(c: Container, db: Session, dst: StoragePort) -> dict[str, dict[str, Any]]:
    last = db.execute(select(BackupRun).where(BackupRun.status == "SUCCEEDED")
                      .order_by(BackupRun.started_at.desc()).limit(1)).scalar_one_or_none()
    if last is None or not last.manifest_key:
        return {}
    try:
        m = json.loads(dst.get_object(c.settings.backup_bucket, last.manifest_key))
    except Exception:  # noqa: BLE001 - 이전 매니페스트가 없으면 전체 복사
        return {}
    return {o["backup_key"]: o for o in m.get("objects", [])}


def _err(exc: BaseException) -> str:
    detail = ""
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        detail = exc.stderr.decode(errors="replace").strip()[-300:] if isinstance(exc.stderr, bytes) else ""
    return f"{type(exc).__name__}: {detail or str(exc)[:300]}"


def _notify_failure(c: Container, db: Session, r: BackupRun) -> None:
    to = tuple(sorted({e for e in db.execute(select(User.contact_email).where(
        User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("PLATFORM_OPERATOR"))).scalars() if e}))  # type: ignore[arg-type]
    c.notifier.send(Notification(
        kind="backup.failed", organization_id=None, subject=f"[백업 실패] {r.backup_run_id}",
        body="\n".join([f"백업 회차 {r.backup_run_id}가 실패했습니다.", f"오류: {r.error}",
                        f"실패 객체: {len(r.failed_objects or [])}개", "",
                        "조치 안내: docs/runbook/03-backup-restore.md"]),
        recipients=to, data={"backup_run_id": r.backup_run_id}))


def run(c: Container) -> str:
    """백업 한 회차 (요구사항 6.1~6.3, 6.5). 결과는 backup_runs, 실패하면 운영자에게 알림."""
    now = c.clock.now()
    bb = c.settings.backup_bucket
    dst = backup_storage(c)
    with c.sessionmaker.begin() as db:
        busy = db.execute(select(BackupRun.backup_run_id).where(
            BackupRun.status == "RUNNING", BackupRun.started_at > now - timedelta(hours=12))).first()
        if busy:
            raise BackupBusy(busy[0])
        r = BackupRun(backup_run_id=new_id("backup_run"), status="RUNNING", started_at=now, failed_objects=[])
        db.add(r)
        rid = r.backup_run_id
        items = plan(c, db)
        prev = _previous(c, db, dst)
    result: dict[str, Any] = {"object_count": len(items), "total_bytes": sum(int(i["size"]) for i in items)}
    try:
        dst.ensure_bucket(bb)
        objects, failed, st = _copy_objects(c, dst, items, prev)
        dump_key, dump_sha, counts = _dump_db(c, dst, rid)
        manifest = {"schema_version": "1.0.0", "backup_run_id": rid, "created_at": iso(now),
                    "db": {"key": dump_key, "sha256": dump_sha, "tables": counts}, "objects": objects,
                    "excluded": "backup_class=NONE인 RAW 객체 (domain.md 9장)"}
        mkey = f"runs/{rid}/manifest.json"
        dst.put_object(bb, mkey, json.dumps(manifest, ensure_ascii=False, indent=1).encode(), "application/json")
        result.update(status="FAILED" if failed else "SUCCEEDED", copied_count=st["copied"],
                      skipped_count=st["skipped"], copied_bytes=st["copied_bytes"], failed_objects=failed,
                      table_count=len(counts), manifest_key=mkey, db_dump_key=dump_key, db_dump_sha256=dump_sha,
                      error=f"객체 {len(failed)}개를 백업하지 못했습니다" if failed else None)
    except Exception as exc:  # noqa: BLE001 - 실패는 기록하고 알린다
        log.warning("backup_failed", extra={"backup_run_id": rid, "error_type": type(exc).__name__})
        result.update(status="FAILED", error=_err(exc))
    with c.sessionmaker.begin() as db:
        r2 = db.get(BackupRun, rid)
        assert r2 is not None  # noqa: S101 - 방금 만든 행
        for k, v in result.items():
            setattr(r2, k, v)
        r2.finished_at = c.clock.now()
        if r2.status == "FAILED":
            _notify_failure(c, db, r2)
    return rid


def due(c: Container, db: Session) -> bool:
    """정기 백업 시각이 됐는가 (워커 정비 주기에서 확인)."""
    if not c.settings.backup_enabled:
        return False
    last = db.execute(select(func.max(BackupRun.started_at))).scalar()
    return last is None or c.clock.now() - last >= timedelta(hours=c.settings.backup_interval_h)


# ------------------------------------------------------------------ 복원 시험 (요구사항 7)


def restore_test(c: Container, backup_run_id: str | None = None, sample: int | None = None) -> dict[str, Any]:
    """임시 DB·임시 위치에 복원해 행 수·감사 로그 체인·객체 sha256·원본 기록을 대조한다 (T10-a)."""
    bb = c.settings.backup_bucket
    dst = backup_storage(c)
    with c.sessionmaker() as db:
        q = select(BackupRun).where(BackupRun.status == "SUCCEEDED")
        q = q.where(BackupRun.backup_run_id == backup_run_id) if backup_run_id else q
        run_row = db.execute(q.order_by(BackupRun.started_at.desc()).limit(1)).scalar_one_or_none()
        if run_row is None or not run_row.manifest_key or not run_row.db_dump_key:
            raise LookupError("복원할 성공한 백업 회차가 없습니다")
        rid, mkey, dkey, dsha = run_row.backup_run_id, run_row.manifest_key, run_row.db_dump_key, run_row.db_dump_sha256
    manifest = json.loads(dst.get_object(bb, mkey))
    url = _db_url(c)
    tmp = f"restore_{rid.lower()}_{secrets.token_hex(2)}"
    if not re.fullmatch(r"[a-z0-9_]+", tmp):  # 식별자는 이 형식만 (아래 DDL에 그대로 들어간다)
        raise ValueError(tmp)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    out: dict[str, Any] = {"backup_run_id": rid, "tested_at": iso(c.clock.now()), "temp_database": tmp,
                           "table_mismatches": [], "object_mismatches": [], "audit_chain_ok": None}
    restored_keys: list[str] = []
    dump_path = None
    try:
        dump_path, sha, _ = _download(dst, bb, dkey)
        if sha != dsha:
            out["table_mismatches"].append({"table": "*", "reason": "덤프 sha256 불일치"})
        with admin.connect() as conn:
            major = int(conn.execute(text("SHOW server_version_num")).scalar_one()) // 10000
            _check_pg_version(c.settings.pg_restore_bin, major, "pg_restore")
            conn.execute(text(f'CREATE DATABASE "{tmp}"'))
        args, env = _pg_args(url)
        cmd = [c.settings.pg_restore_bin, "--no-owner", "--no-privileges", "--exit-on-error", *args, "-d", tmp,
               dump_path]
        subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=6 * 3600)  # noqa: S603  # nosec B603
        eng = create_engine(url.set(database=tmp))
        try:
            with eng.connect() as conn:
                got = _counts(conn)
                checksums = dict(conn.execute(select(Asset.asset_id, Asset.checksum_sha256)).tuples().all())
            want = manifest["db"]["tables"]
            out["tables_checked"] = len(want)
            for t in sorted(set(want) | set(got)):
                if want.get(t) != got.get(t):
                    out["table_mismatches"].append({"table": t, "expected": want.get(t), "restored": got.get(t)})
            out["audit_chain_ok"] = AuditLogger(eng, c.clock).verify_chain().ok
        finally:
            eng.dispose()
        objs = manifest.get("objects", [])
        objs = objs[:sample] if sample else objs
        for o in objs:
            tkey = f"restore-tests/{rid}/{o['zone']}/{o['key']}"
            p, s1, _ = _download(dst, bb, o["backup_key"])
            try:
                dst.upload_file(bb, tkey, p)
            finally:
                os.unlink(p)
            restored_keys.append(tkey)
            s2 = _sha_of(dst, bb, tkey)
            recorded = checksums.get(o["asset_id"]) if o.get("asset_id") else o["sha256"]
            if not (s1 == s2 == o["sha256"] == recorded):
                out["object_mismatches"].append({"key": o["backup_key"], "manifest": o["sha256"], "restored": s2,
                                                 "recorded": recorded})
        out["objects_checked"] = len(objs)
    except Exception as exc:  # noqa: BLE001 - 시험 실패로 보고
        out["error"] = _err(exc)
    finally:
        if dump_path:
            os.unlink(dump_path)
        for k in restored_keys:
            with contextlib.suppress(Exception):
                dst.delete_object(bb, k)
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{tmp}" WITH (FORCE)'))
        admin.dispose()
    out["ok"] = (not out.get("error") and not out["table_mismatches"] and not out["object_mismatches"]
                 and out["audit_chain_ok"] is True)
    with c.sessionmaker.begin() as db:
        r = db.get(BackupRun, rid)
        if r is not None:
            r.restore_test = {k: v for k, v in out.items() if k != "temp_database"}
    return out


def run_view(r: BackupRun) -> dict[str, Any]:
    return {"backup_run_id": r.backup_run_id, "status": r.status, "started_at": iso(r.started_at),
            "finished_at": iso(r.finished_at) if r.finished_at else None, "object_count": r.object_count,
            "copied_count": r.copied_count, "skipped_count": r.skipped_count, "total_bytes": r.total_bytes,
            "copied_bytes": r.copied_bytes, "table_count": r.table_count, "db_dump_sha256": r.db_dump_sha256,
            "failed_objects": r.failed_objects or [], "error": r.error, "restore_test": r.restore_test}
