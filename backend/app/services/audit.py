"""감사 로그 (02번 요구사항 7). 추가 전용 + 해시 체인. 주 트랜잭션과 별도 연결로 기록한다."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.context import request_id_var
from app.domain.ids import new_id
from app.domain.principal import Principal
from app.ports.clock import ClockPort

GENESIS = "0" * 64
_LOCK_KEY = 7_202_611  # pg_advisory_xact_lock 키 (감사 로그 체인 직렬화)


def _canonical(row: dict[str, Any]) -> str:
    def conv(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return v

    return json.dumps({k: conv(v) for k, v in sorted(row.items())}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def chain_hash(prev_hash: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + "|" + _canonical(row)).encode()).hexdigest()


HASHED_FIELDS = ("audit_id", "occurred_at", "actor_kind", "actor_id", "actor_organization_id",
                 "organization_id", "action", "target_type", "target_id", "result", "source_ip",
                 "request_id", "reason", "details")


@dataclass
class ChainReport:
    checked: int
    broken_at_seq: int | None
    broken_audit_id: str | None

    @property
    def ok(self) -> bool:
        return self.broken_at_seq is None


class AuditLogger:
    def __init__(self, engine: Engine, clock: ClockPort) -> None:
        self.engine = engine
        self.clock = clock

    def record(self, action: str, *, result: str = "SUCCESS", principal: Principal | None = None,
               actor_kind: str | None = None, actor_id: str | None = None,
               organization_id: str | None = None, target_type: str | None = None,
               target_id: str | None = None, source_ip: str | None = None, reason: str | None = None,
               details: dict[str, Any] | None = None) -> str:
        now = self.clock.now()
        row = {
            "audit_id": new_id("audit"),
            "occurred_at": now.replace(microsecond=(now.microsecond // 1000) * 1000),
            "actor_kind": actor_kind or (principal.kind if principal else "ANONYMOUS"),
            "actor_id": actor_id or (principal.principal_id if principal else None),
            "actor_organization_id": principal.organization_id if principal else None,
            "organization_id": organization_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "result": result,
            "source_ip": source_ip,
            "request_id": request_id_var.get(),
            "reason": reason,
            "details": details or {},
        }
        with self.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
            prev = conn.execute(text("SELECT row_hash FROM audit_log ORDER BY seq DESC LIMIT 1")).scalar()
            prev_hash = prev or GENESIS
            row_hash = chain_hash(prev_hash, row)
            conn.execute(
                text("""INSERT INTO audit_log (audit_id, occurred_at, actor_kind, actor_id, actor_organization_id,
                        organization_id, action, target_type, target_id, result, source_ip, request_id, reason,
                        details, prev_hash, row_hash)
                        VALUES (:audit_id, :occurred_at, :actor_kind, :actor_id, :actor_organization_id,
                        :organization_id, :action, :target_type, :target_id, :result, :source_ip, :request_id,
                        :reason, CAST(:details AS JSONB), :prev_hash, :row_hash)"""),
                {**row, "details": json.dumps(row["details"], ensure_ascii=False), "prev_hash": prev_hash,
                 "row_hash": row_hash},
            )
        return str(row["audit_id"])

    def verify_chain(self) -> ChainReport:
        """해시 체인이 끊긴 첫 위치를 보고한다 (02번 요구사항 7.4)."""
        prev_hash = GENESIS
        checked = 0
        cols = ", ".join(HASHED_FIELDS)
        with self.engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(
                text(f"SELECT seq, {cols}, prev_hash, row_hash FROM audit_log ORDER BY seq")  # noqa: S608  # nosec B608 - 열 이름은 고정 상수다
            ).mappings()
            for r in result:
                row = {k: r[k] for k in HASHED_FIELDS}
                if r["prev_hash"] != prev_hash or chain_hash(prev_hash, row) != r["row_hash"]:
                    return ChainReport(checked, int(r["seq"]), str(r["audit_id"]))
                prev_hash = r["row_hash"]
                checked += 1
        return ChainReport(checked, None, None)
