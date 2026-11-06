"""기기·API 키 토큰 교환 경로의 IP 단위 호출 한도."""

import pytest

from app.api import auth
from app.ratelimit import Limiter
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration


def test_token_exchange_limits_one_ip_even_when_client_ids_rotate(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_auth_limiter", Limiter(limit=2, window_s=60))
    path = "/v1/auth/token"
    for number in range(3):
        response = ctx.client.post(path, data={"grant_type": "client_credentials",
                                              "client_id": f"synthetic-unknown-{number}",
                                              "client_secret": "synthetic-invalid"})
        if number < 2:
            assert response.status_code == 401 and response.json()["code"] == "UNAUTHENTICATED", response.text
            check(response, "POST", path)
    assert response.status_code == 429, response.text
    check(response, "POST", path)
    assert response.json()["code"] == "RATE_LIMITED"
    assert 1 <= int(response.headers["Retry-After"]) <= 61
