#!/usr/bin/env python3
"""해양 실증데이터 플랫폼 업로드 예제 클라이언트 (04번 요구사항 10.2).

contracts/openapi.yaml의 업로드 흐름을 그대로 따른다.

1. 토큰: ``MARINE_TOKEN``(사용자·기관 서버의 액세스 토큰)을 그대로 쓰거나,
   ``MARINE_CLIENT_ID``·``MARINE_CLIENT_SECRET``(API 키 ``key_…`` 또는 기기 자격증명 ``dcr_…``)을
   ``POST /v1/auth/token``(client_credentials)으로 단기 토큰과 바꾼다. 만료되면 다시 받는다.
2. ``POST /v1/uploads`` — 매니페스트 + ``Idempotency-Key`` → upload_id, 파트 크기·수, 파트별 사전 서명 URL
3. 파트를 사전 서명 URL로 스토리지에 직접 ``PUT``한다. 바이너리는 API 서버를 거치지 않고,
   스토리지 요청에는 API 토큰을 붙이지 않는다.
4. 재개: ``GET /v1/uploads/{id}``로 아직 진행 중인지 확인하고 ``GET /v1/uploads/{id}/parts``로 완료된 파트를 받아
   남은 파트만 보낸다. 끊긴 뒤 같은 파일로 다시 실행하면 된다 (상태 파일 또는 같은 Idempotency-Key 재응답).
5. ``POST /v1/uploads/{id}/complete`` → 202, ingest_status=RECEIVED (수신은 등록이 아니다)
6. ``GET /v1/uploads/{id}``를 등록·격리될 때까지 조회 → ``GET /v1/assets/{id}/receipt`` 영수증

의존성은 httpx 하나다. 비밀값(토큰·클라이언트 비밀)은 환경변수로만 받고 파일·로그에 남기지 않는다.
사전 서명 URL도 로그에 남기지 않는다 (contracts/openapi.yaml 설명).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

__all__ = ["ApiError", "MarineClient", "UploadResult", "build_manifest", "sha256_file"]

log = logging.getLogger("marine_client")

CLIENT_VERSION = "0.1.0"
# domain.md ingest_status: 진행 중(UPLOADING·RECEIVED·VALIDATING)이 아니면 결과가 정해진 것이다
IN_PROGRESS = frozenset({"UPLOADING", "RECEIVED", "VALIDATING"})
REGISTERED_OR_LATER = frozenset({"REGISTERED", "PROXIED", "READY_PRIVATE"})
RETRY_STATUSES = frozenset({429, 503})  # 계약: RATE_LIMITED·STORAGE_UNAVAILABLE에 Retry-After가 붙는다


def retryable(r: httpx.Response) -> bool:
    """다시 보내면 되는 응답인가. 409에 Retry-After가 붙으면 같은 Idempotency-Key의 앞선 요청이 처리 중이거나 동시 요청과 겹친 것이다
    (계약 Conflict 응답). Retry-After가 없는 409(IDEMPOTENCY_CONFLICT·업무 충돌)는 다시 보내도 같다."""
    return r.status_code in RETRY_STATUSES or (r.status_code == 409 and "retry-after" in r.headers)


class ApiError(Exception):
    """RFC 9457 problem+json 오류. 프로그램 분기는 ``code``로 한다 (계약 설명)."""

    def __init__(self, status: int, problem: dict[str, Any]) -> None:
        self.status = status
        self.problem = problem
        self.code = problem.get("code")
        super().__init__(f"{status} {self.code}: {problem.get('detail') or problem.get('title') or ''}".strip())


@dataclass
class UploadResult:
    status: str  # UPLOAD_COMPLETED | ALREADY_COMPLETED | ALREADY_REGISTERED | METADATA_ONLY_REGISTERED
    asset_id: str
    upload_id: str | None = None
    ingest_status: str | None = None
    parts_sent: list[int] = field(default_factory=list)  # 이번 실행에서 실제로 보낸 파트 번호


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def build_manifest(path: Path, *, source_system_id: str, data_type: str, variant: str, mime_type: str,
                   session_id: str | None = None, capture_start_at: str | None = None,
                   timestamp_source: str | None = None, timestamp_verified: bool = False,
                   **extra: Any) -> dict[str, Any]:
    """contracts/schemas/upload-manifest.schema.json 형식. organization_id는 넣지 않는다 (인증 정보로 정한다).

    촬영시각을 모르면 비워 둔다. 서버가 UNKNOWN으로 기록하고 품질 경고를 붙인다 (domain.md 6장).
    """
    source = timestamp_source or ("MANUAL" if capture_start_at else "UNKNOWN")
    m: dict[str, Any] = {
        "schema_version": "1.0.0",
        "source_system_id": source_system_id,
        "data_type": data_type,
        "variant": variant,
        "file": {"original_filename": path.name, "byte_size": path.stat().st_size,
                 "checksum_sha256": sha256_file(path), "mime_type": mime_type},
        "timestamp_quality": {"source": source, "verified": timestamp_verified},
        "client": {"kind": "API", "version": CLIENT_VERSION},
    }
    if session_id:
        m["session_id"] = session_id
    if capture_start_at:
        m["capture_start_at"] = capture_start_at
    m.update(extra)
    return m


def default_idempotency_key(manifest: dict[str, Any]) -> str:
    """같은 장비·같은 파일이면 같은 키 → 재시도·재실행 때 서버가 처음 응답(같은 upload_id)을 다시 준다 (24시간)."""
    local = (manifest.get("client") or {}).get("local_collection_id")
    if local:
        return str(local)
    return f"py-{manifest['source_system_id']}-{manifest['file']['checksum_sha256'][:40]}"


class MarineClient:
    """업로드 API 클라이언트.

    ``http``는 API용, ``storage``는 사전 서명 URL용 httpx 클라이언트다. 시험에서는 앱에 바로 붙은 클라이언트
    (예: FastAPI TestClient)를 넣을 수 있다. 둘을 나눈 이유: 스토리지 요청에 API 토큰이 새지 않게 한다.
    """

    def __init__(self, base_url: str | None = None, *, token: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, http: httpx.Client | None = None,
                 storage: httpx.Client | None = None, timeout_s: float = 60.0, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not token and not (client_id and client_secret):
            raise ValueError("토큰(MARINE_TOKEN) 또는 클라이언트 자격증명"
                             "(MARINE_CLIENT_ID·MARINE_CLIENT_SECRET)이 필요합니다")
        if http is None and not base_url:
            raise ValueError("API 주소(MARINE_API_URL)가 필요합니다")
        self._http = http or httpx.Client(base_url=base_url or "", timeout=timeout_s)
        self._storage = storage or httpx.Client(timeout=timeout_s)
        self._owns = (http is None, storage is None)
        self._static_token = token
        self._client_id, self._client_secret = client_id, client_secret
        self._token: str | None = token
        self._token_expires_at = float("inf") if token else 0.0
        self._max_retries, self._sleep = max_retries, sleep

    @classmethod
    def from_env(cls, **kw: Any) -> MarineClient:
        return cls(os.environ.get("MARINE_API_URL", "http://localhost:8000"),
                   token=os.environ.get("MARINE_TOKEN") or None,
                   client_id=os.environ.get("MARINE_CLIENT_ID") or None,
                   client_secret=os.environ.get("MARINE_CLIENT_SECRET") or None, **kw)

    def close(self) -> None:
        if self._owns[0]:
            self._http.close()
        if self._owns[1]:
            self._storage.close()

    def __enter__(self) -> MarineClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 인증
    def _bearer(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._fetch_token()
        assert self._token is not None
        return self._token

    def _fetch_token(self) -> None:
        if self._static_token:
            raise ApiError(401, {"code": "UNAUTHENTICATED", "detail": "MARINE_TOKEN이 만료되었거나 올바르지 않습니다"})
        r = self._http.post("/v1/auth/token", data={"grant_type": "client_credentials",
                                                     "client_id": self._client_id,
                                                     "client_secret": self._client_secret})
        body = self._json(r)
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + max(int(body["expires_in"]) - 30, 1)

    # ------------------------------------------------------------------ API 호출
    @staticmethod
    def _json(r: httpx.Response) -> Any:
        if r.status_code >= 400:
            try:
                problem = r.json()
            except ValueError:
                problem = {"title": r.text[:200]}
            raise ApiError(r.status_code, problem if isinstance(problem, dict) else {"title": str(problem)})
        return r.json() if r.content else None

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", None) or {})
        refreshed = False
        for attempt in range(self._max_retries + 1):
            headers["Authorization"] = f"Bearer {self._bearer()}"
            r = self._http.request(method, path, headers=headers, **kw)
            if r.status_code == 401 and not refreshed and not self._static_token:
                refreshed, self._token = True, None  # 단기 토큰 만료 → 한 번 다시 받는다
                continue
            if retryable(r) and attempt < self._max_retries:
                wait = min(float(r.headers.get("retry-after", "1")), 60.0)
                log.warning("재시도 대기 %s초 (%s %s → %s)", wait, method, path, r.status_code)
                self._sleep(wait)
                continue
            return r
        return r

    def create_upload(self, manifest: dict[str, Any], *, idempotency_key: str) -> tuple[dict[str, Any], bool]:
        """(응답, 재응답 여부). 같은 키·같은 본문이면 서버가 처음 응답을 다시 준다 (Idempotent-Replayed)."""
        r = self._request("POST", "/v1/uploads", json=manifest, headers={"Idempotency-Key": idempotency_key})
        return self._json(r), r.headers.get("idempotent-replayed") == "true"

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        return self._json(self._request("GET", f"/v1/uploads/{upload_id}"))

    def list_parts(self, upload_id: str) -> dict[str, Any]:
        return self._json(self._request("GET", f"/v1/uploads/{upload_id}/parts"))

    def complete_upload(self, upload_id: str, parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
        body = {"parts": sorted(({"part_number": p["part_number"], "etag": p["etag"]} for p in parts),
                                key=lambda p: p["part_number"])}
        r = self._request("POST", f"/v1/uploads/{upload_id}/complete", json=body,
                          headers={"Idempotency-Key": f"complete-{upload_id}"})
        return self._json(r)

    def abort_upload(self, upload_id: str) -> None:
        self._json(self._request("DELETE", f"/v1/uploads/{upload_id}"))

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._json(self._request("GET", f"/v1/assets/{asset_id}"))

    def get_receipt(self, asset_id: str) -> dict[str, Any]:
        return self._json(self._request("GET", f"/v1/assets/{asset_id}/receipt"))

    def put_part(self, url: str, data: bytes) -> str:
        """사전 서명 URL로 파트 하나를 보낸다. 완료 요청에 쓸 ETag를 돌려준다. URL은 로그에 남기지 않는다."""
        r = self._storage.put(url, content=data)
        r.raise_for_status()
        return r.headers["etag"]

    # ------------------------------------------------------------------ 높은 수준: 올리기·재개·기다리기
    def upload_file(self, path: str | Path, manifest: dict[str, Any], *, state_path: str | Path | None = None,
                    idempotency_key: str | None = None) -> UploadResult:
        """파일 하나를 올린다. 이미 시작한 업로드면 남은 파트만 보낸다.

        state_path가 있으면 upload_id를 거기 적어 두고(비밀값 없음) 다음 실행에서 이어 간다.
        없어도 같은 Idempotency-Key로 24시간 안에 다시 실행하면 서버가 같은 upload_id를 돌려줘 이어 간다.
        """
        path = Path(path)
        sha = manifest["file"]["checksum_sha256"]
        state = _load_state(state_path, sha)
        if state is not None:
            result = self._resume(path, state["upload_id"], state["asset_id"])
            if result is not None:
                _clear_state(state_path)
                return result
            _clear_state(state_path)  # 만료(410)·취소된 세션 → 새로 만든다
        key = idempotency_key or default_idempotency_key(manifest)
        for _ in range(2):
            created, replayed = self.create_upload(manifest, idempotency_key=key)
            if created["status"] != "UPLOAD_CREATED":  # ALREADY_REGISTERED·METADATA_ONLY_REGISTERED: 보낼 것이 없다
                return UploadResult(created["status"], created["asset_id"])
            upload_id, asset_id = created["upload_id"], created["asset_id"]
            _save_state(state_path, {"upload_id": upload_id, "asset_id": asset_id, "checksum_sha256": sha})
            if not replayed:
                result = self._send_and_complete(path, upload_id, asset_id, created["part_size"],
                                                 created["part_count"], {}, created["parts"])
                break
            resumed = self._resume(path, upload_id, asset_id)
            if resumed is not None:
                result = resumed
                break
            # 재응답받은 세션이 만료·취소됐다 → 그 세션에서 끌어낸 새 키로 한 번만 새 세션을 만든다
            _clear_state(state_path)
            key = f"{key[:100]}-renew-{upload_id[-8:]}"
        else:
            raise ApiError(410, {"code": "GONE", "detail": "업로드 세션을 새로 만들지 못했습니다"})
        _clear_state(state_path)
        return result

    def _resume(self, path: Path, upload_id: str, asset_id: str) -> UploadResult | None:
        """진행 중인 세션이면 남은 파트만 보내고 완료한다. 이미 완료됐으면 그 상태를, 만료·취소됐으면 None을 준다."""
        try:
            status = self.get_upload(upload_id)
            # 완료 요청까지 끝난 세션: 모든 파트를 받았고 더는 UPLOADING이 아니다 (만료·취소 세션은 parts_completed=0)
            if status["ingest_status"] != "UPLOADING" and status["parts_completed"] == status["part_count"]:
                return UploadResult("ALREADY_COMPLETED", asset_id, upload_id, status["ingest_status"])
            plist = self.list_parts(upload_id)  # 만료·취소면 410 GONE
        except ApiError as exc:
            if exc.status in (404, 410):
                log.warning("업로드 세션 %s를 이어 갈 수 없습니다 (%s)", upload_id, exc.code)
                return None
            raise
        done = {p["part_number"]: p["etag"] for p in plist["completed_parts"]}
        log.info("업로드 재개 %s: 완료 %d개, 남은 파트 %d개", upload_id, len(done), plist["part_count"] - len(done))
        return self._send_and_complete(path, upload_id, asset_id, plist["part_size"], plist["part_count"], done,
                                       plist["pending_parts"])

    def _send_and_complete(self, path: Path, upload_id: str, asset_id: str, part_size: int, part_count: int,
                           done: dict[int, str], pending: list[dict[str, Any]]) -> UploadResult:
        sent: list[int] = []
        refreshes = 0
        with path.open("rb") as f:
            while len(done) < part_count:
                if not pending:  # 응답의 URL 수가 한도로 잘렸거나 URL이 만료됐다 → 남은 파트 URL을 다시 받는다
                    refreshes += 1
                    if refreshes > max(3, part_count):
                        raise RuntimeError(f"파트 URL을 계속 쓸 수 없습니다 ({len(done)}/{part_count})")
                    plist = self.list_parts(upload_id)
                    done.update({p["part_number"]: p["etag"] for p in plist["completed_parts"]})
                    pending = [p for p in plist["pending_parts"] if p["part_number"] not in done]
                    if not pending and len(done) < part_count:
                        raise RuntimeError(f"남은 파트 URL을 받지 못했습니다 ({len(done)}/{part_count})")
                    continue
                part = pending.pop(0)
                n = part["part_number"]
                if n in done:
                    continue
                f.seek((n - 1) * part_size)
                try:
                    done[n] = self.put_part(part["url"], f.read(part_size))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 403:  # 403: 서명 만료 → URL만 다시 받는다
                        raise
                    pending = []
                    continue
                sent.append(n)
                log.info("파트 %d/%d 전송", n, part_count)
        status = self.complete_upload(upload_id, ({"part_number": n, "etag": e} for n, e in done.items()))
        return UploadResult("UPLOAD_COMPLETED", asset_id, upload_id, status["ingest_status"], sent)

    def wait_until_settled(self, upload_id: str, *, timeout_s: float = 600.0,
                           interval_s: float = 5.0) -> dict[str, Any]:
        """무결성 검증이 끝나 등록(REGISTERED 이후)되거나 격리·실패할 때까지 상태를 조회한다."""
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.get_upload(upload_id)
            if status["ingest_status"] not in IN_PROGRESS:
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{upload_id}: {timeout_s}초 안에 등록되지 않았습니다 ({status['ingest_status']})")
            self._sleep(interval_s)


# ---------------------------------------------------------------------- 재개용 로컬 상태 (비밀값 없음)
def _load_state(state_path: str | Path | None, sha: str) -> dict[str, Any] | None:
    if not state_path or not Path(state_path).exists():
        return None
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    return state if state.get("checksum_sha256") == sha else None


def _save_state(state_path: str | Path | None, state: dict[str, Any]) -> None:
    if state_path:
        Path(state_path).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _clear_state(state_path: str | Path | None) -> None:
    if state_path:
        Path(state_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------- 명령줄
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="해양 실증데이터 플랫폼 업로드 예제 (환경변수: MARINE_API_URL, "
                                             "MARINE_TOKEN 또는 MARINE_CLIENT_ID·MARINE_CLIENT_SECRET)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="파일 올리기 (끊겼으면 같은 명령으로 이어 올리기)")
    up.add_argument("file", type=Path)
    up.add_argument("--source", required=True, help="원천 장비 ID (dev_…)")
    up.add_argument("--session", help="실증회차 ID (ses_…). 없으면 장비의 회차 규칙으로 정한다")
    up.add_argument("--data-type", required=True, help="VIDEO, SENSOR, … (contracts common.schema.json)")
    up.add_argument("--variant", required=True, help="ORIGINAL, CLIP, … (장비 collection_mode가 허용하는 값)")
    up.add_argument("--mime", required=True, help="신고 MIME (서버가 내용으로 다시 판별한다)")
    up.add_argument("--capture-start", help="촬영 시작 시각 (RFC 3339 UTC, 예: 2026-11-01T01:00:00Z)")
    up.add_argument("--time-source", help="촬영시각 근거 (GPS, NTP, DEVICE, MANIFEST, MANUAL, UNKNOWN)")
    up.add_argument("--state", type=Path, help="재개용 상태 파일 (기본: <파일>.marine-upload.json)")
    up.add_argument("--wait", type=float, default=0, help="등록될 때까지 기다릴 초 (0이면 기다리지 않음)")
    st = sub.add_parser("status", help="업로드 상태")
    st.add_argument("upload_id")
    rc = sub.add_parser("receipt", help="등록 영수증")
    rc.add_argument("asset_id")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        with MarineClient.from_env() as client:
            if args.cmd == "upload":
                manifest = build_manifest(args.file, source_system_id=args.source, data_type=args.data_type,
                                          variant=args.variant, mime_type=args.mime, session_id=args.session,
                                          capture_start_at=args.capture_start, timestamp_source=args.time_source)
                state = args.state or args.file.with_name(args.file.name + ".marine-upload.json")
                result = client.upload_file(args.file, manifest, state_path=state)
                out: dict[str, Any] = {"status": result.status, "asset_id": result.asset_id,
                                       "upload_id": result.upload_id, "ingest_status": result.ingest_status,
                                       "parts_sent": result.parts_sent}
                if args.wait and result.upload_id:
                    out["ingest_status"] = client.wait_until_settled(result.upload_id,
                                                                     timeout_s=args.wait)["ingest_status"]
                    if out["ingest_status"] in REGISTERED_OR_LATER:
                        out["receipt"] = client.get_receipt(result.asset_id)
            elif args.cmd == "status":
                out = client.get_upload(args.upload_id)
            else:
                out = client.get_receipt(args.asset_id)
    except ApiError as exc:
        print(json.dumps({"error": exc.code, "status": exc.status, "problem": exc.problem}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except ValueError as exc:  # 환경변수 누락
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    except httpx.TransportError as exc:  # 연결 끊김 등: 같은 명령을 다시 실행하면 남은 파트부터 이어 간다
        print(f"전송 오류({type(exc).__name__}): 같은 명령을 다시 실행하면 이어서 올립니다", file=sys.stderr)
        return 3
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
