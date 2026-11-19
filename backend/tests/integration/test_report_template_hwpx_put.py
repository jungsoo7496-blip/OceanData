"""보고서 양식 HWPX 첨부의 경로 변수·본문 크기 (계약 점검 S32·S33, 10번 요구사항 4.6).

- S32: 계약은 version을 1 이상 정수로 정하는데, versions/0은 200으로 최신 버전에 파일을 붙이고 감사도 그 버전으로
  남겼다(versions/-1은 404). 0과 음수는 400 VALIDATION_ERROR이고 아무 버전도 바뀌지 않는다.
- S33: 10MiB를 넘는 본문의 413 PAYLOAD_TOO_LARGE가 계약에 없었다. 계약에 적은 413과 맞는지 확인한다.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
OP = 106
PUT = "/v1/report-templates/{template_code}/versions/{version}/hwpx"
LIMIT = 10 * 1024 * 1024  # 계약 requestBody maxLength 10485760


def _hwpx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/section0.xml", "<s>{{report.title}}</s>")
    return buf.getvalue()


def _put(ctx: Ctx, code: str, version: int, data: bytes) -> Any:
    return ctx.client.put(PUT.format(template_code=code, version=version), content=data,
                          headers={**ctx.as_user(OP), "Content-Type": "application/octet-stream"})


def _has_hwpx(ctx: Ctx, code: str) -> dict[int, bool]:
    r = ctx.client.get("/v1/report-templates", headers=ctx.as_user(OP))
    return {t["version"]: t["has_hwpx"] for t in r.json()["items"] if t["code"] == code}


def test_hwpx_put_rejects_versions_below_one(ictx: Ctx) -> None:
    ctx = ictx
    tpl = {"code": "HWPX_VER", "name": "버전 시험", "scope": "SESSION",
           "sections": [{"kind": "TEXT", "title": "개요", "text": "{{report.title}}"}]}
    for _ in range(2):
        assert ctx.client.post("/v1/report-templates", json=tpl, headers=ctx.as_user(OP)).status_code == 201

    for version in (0, -1):
        r = _put(ctx, "HWPX_VER", version, _hwpx())
        # 수정 전: 0은 200으로 최신 버전(v2)에 붙고, -1은 404
        assert r.status_code == 400, (version, r.status_code, r.text)
        check(r, "PUT", PUT)
        assert r.json()["code"] == "VALIDATION_ERROR"
        assert [e["pointer"] for e in r.json()["errors"]] == ["/path/version"]
    assert _has_hwpx(ctx, "HWPX_VER") == {1: False, 2: False}
    assert "REPORT_TEMPLATE_HWPX_SET" not in ctx.audit_actions()

    ok = _put(ctx, "HWPX_VER", 1, _hwpx())  # 이름 붙인 버전에만 붙는다
    assert ok.status_code == 200, ok.text
    check(ok, "PUT", PUT)
    assert ok.json()["version"] == 1
    assert _has_hwpx(ctx, "HWPX_VER") == {1: True, 2: False}
    assert ctx.audit_actions().count("REPORT_TEMPLATE_HWPX_SET") == 1


def test_hwpx_put_over_the_size_limit_is_the_documented_413(ctx: Ctx) -> None:
    over = _put(ctx, "EXAMPLE_SESSION_LOG", 1, b"\0" * (LIMIT + 1))
    assert over.status_code == 413, over.text
    assert over.json()["code"] == "PAYLOAD_TOO_LARGE"
    check(over, "PUT", PUT)  # 수정 전: '계약에 413 응답이 없다'

    at_limit = _put(ctx, "EXAMPLE_SESSION_LOG", 1, b"\0" * LIMIT)  # 한도와 같은 크기는 크기 검사를 지난다
    assert at_limit.status_code == 400 and at_limit.json()["code"] == "VALIDATION_ERROR", at_limit.text
    check(at_limit, "PUT", PUT)
    assert _has_hwpx(ctx, "EXAMPLE_SESSION_LOG") == {1: False}
