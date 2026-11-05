"""RFC 9457 오류 응답 (01번 요구사항 10, api-conventions.md)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.context import request_id_var
from app.domain.errors import InvalidStateTransition, RuleViolation

log = logging.getLogger(__name__)
PROBLEM_JSON = "application/problem+json"

TITLES = {
    "VALIDATION_ERROR": "입력값이 올바르지 않습니다",
    "UNAUTHENTICATED": "인증이 필요합니다",
    "FORBIDDEN": "권한이 없습니다",
    "MFA_REQUIRED": "다중 인증이 필요합니다",
    "NOT_FOUND": "대상을 찾을 수 없습니다",
    "CONFLICT": "요청이 현재 상태와 충돌합니다",
    "IDEMPOTENCY_CONFLICT": "같은 Idempotency-Key로 다른 요청이 들어왔습니다",
    "INVALID_STATE_TRANSITION": "허용되지 않는 상태 전이입니다",
    "QUOTA_EXCEEDED": "저장 쿼터를 초과합니다",
    "GONE": "만료되었거나 더 이상 사용할 수 없습니다",
    "PAYLOAD_TOO_LARGE": "요청 크기가 한도를 넘습니다",
    "RULE_VIOLATION": "정책 조건을 충족하지 않습니다",
    "UNSUPPORTED_SCHEMA_VERSION": "지원하지 않는 스키마 버전입니다",
    "RATE_LIMITED": "호출 한도를 넘었습니다",
    "INTERNAL": "서버 오류가 발생했습니다",
    "STORAGE_UNAVAILABLE": "스토리지를 사용할 수 없습니다",
    "SERVICE_UNAVAILABLE": "서비스를 일시적으로 사용할 수 없습니다",
}
STATUS = {
    "VALIDATION_ERROR": 400, "UNAUTHENTICATED": 401, "FORBIDDEN": 403, "MFA_REQUIRED": 403,
    "NOT_FOUND": 404, "CONFLICT": 409, "IDEMPOTENCY_CONFLICT": 409, "INVALID_STATE_TRANSITION": 409,
    "QUOTA_EXCEEDED": 409, "GONE": 410, "PAYLOAD_TOO_LARGE": 413, "RULE_VIOLATION": 422,
    "UNSUPPORTED_SCHEMA_VERSION": 422, "RATE_LIMITED": 429, "INTERNAL": 500, "STORAGE_UNAVAILABLE": 503,
    "SERVICE_UNAVAILABLE": 503,
}


class ProblemError(Exception):
    def __init__(self, code: str, detail: str | None = None, *, title: str | None = None,
                 errors: list[dict[str, str]] | None = None,
                 unmet_conditions: list[dict[str, str | None]] | None = None,
                 headers: dict[str, str] | None = None, extra: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = STATUS[code]
        self.title = title or TITLES[code]
        self.detail = detail
        self.errors = errors
        self.unmet_conditions = unmet_conditions
        self.headers = headers or {}
        self.extra = extra or {}


def problem_body(code: str, *, instance: str | None, title: str | None = None, detail: str | None = None,
                 errors: list | None = None, unmet_conditions: list | None = None,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "/problems/" + code.lower().replace("_", "-"),
        "title": title or TITLES[code],
        "status": STATUS[code],
        "code": code,
        "request_id": request_id_var.get() or "",
    }
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errors is not None or code == "VALIDATION_ERROR":
        body["errors"] = errors or []
    if unmet_conditions is not None or code == "RULE_VIOLATION":
        body["unmet_conditions"] = unmet_conditions or []
    if extra:
        body.update(extra)
    return body


def instance_of(path: str) -> str:
    """problem+json instance는 URI 참조여야 한다 — 디코딩된 경로를 다시 퍼센트 인코딩한다 (Schemathesis M3)."""
    return quote(path, safe="/:@!$&'()*+,;=")


def problem_response(code: str, request: Request | None = None, **kw: Any) -> JSONResponse:
    headers = kw.pop("headers", None) or {}
    body = problem_body(code, instance=instance_of(request.url.path) if request else None, **kw)
    return JSONResponse(body, status_code=STATUS[code], media_type=PROBLEM_JSON, headers=headers)


def _pointer(loc: tuple | list) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] == "body":
        parts = parts[1:]
    return "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.code, request, title=exc.title, detail=exc.detail, errors=exc.errors,
                                unmet_conditions=exc.unmet_conditions, headers=exc.headers, extra=exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"pointer": _pointer(e.get("loc", ())), "code": str(e.get("type", "invalid")),
                   "message": str(e.get("msg", ""))} for e in exc.errors()]
        return problem_response("VALIDATION_ERROR", request, errors=errors)

    @app.exception_handler(RuleViolation)
    async def _rule(request: Request, exc: RuleViolation) -> JSONResponse:
        return problem_response("RULE_VIOLATION", request, detail=exc.detail,
                                unmet_conditions=[{"condition": u.condition, "actual": u.actual} for u in exc.unmet])

    @app.exception_handler(InvalidStateTransition)
    async def _transition(request: Request, exc: InvalidStateTransition) -> JSONResponse:
        return problem_response("INVALID_STATE_TRANSITION", request, detail=str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "NOT_FOUND",
                413: "PAYLOAD_TOO_LARGE", 429: "RATE_LIMITED"}.get(exc.status_code, "INTERNAL")
        if exc.status_code == 400:
            code = "VALIDATION_ERROR"
        return problem_response(code, request)

    @app.exception_handler(DBAPIError)
    async def _db(request: Request, exc: DBAPIError) -> JSONResponse:
        # 문자열에 PostgreSQL이 저장할 수 없는 문자(NUL 등)가 있으면 입력 오류다 (Schemathesis M8, 그 밖은 500)
        code = _pg_code(exc)
        if code in ("22021", "22P05"):
            return problem_response("VALIDATION_ERROR", request, errors=[{
                "pointer": "", "code": "invalid_character", "message": "문자열에 쓸 수 없는 문자(NUL 등)가 있습니다"}])
        if code in ("22003", "22007", "22008", "22015"):  # 숫자·시각·간격 범위 초과 (예: 32비트를 넘는 version)
            return problem_response("VALIDATION_ERROR", request, errors=[{
                "pointer": "", "code": "out_of_range", "message": "숫자나 시각이 저장할 수 있는 범위를 벗어났습니다"}])
        raise exc

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 스택·SQL·내부 경로는 로그에만 (01번 요구사항 10.2)
        log.error("unhandled_exception", exc_info=exc, extra={"path": request.url.path})
        return problem_response("INTERNAL", request)


def _pg_code(exc: DBAPIError) -> str | None:
    """pg8000 오류의 SQLSTATE (args[0]['C'])."""
    args = getattr(exc.orig, "args", ())
    return args[0].get("C") if args and isinstance(args[0], dict) else None
