"""계약(contracts/openapi.yaml)의 작업과 서버 라우트가 1:1인지 본다.

Schemathesis 탐색은 문서화된 404를 정상 응답으로 받아들이므로, 라우트가 아예 없는 작업은 잡지 못한다
(G1 계약의 `GET /v1/quality-reports`가 그렇게 빠져 있었다). 실제 앱(`create_app`)의 라우트를 쓰므로 새 라우터를
main.py에 붙이면 따로 목록을 고치지 않아도 검사된다. DB에는 연결하지 않는다.
"""

from __future__ import annotations

import yaml
from sqlalchemy import create_engine

from app.adapters.notifier import LogNotifier
from app.container import build_container
from app.main import create_app
from app.services.contracts import contracts_dir
from tests.conftest import FakeStorage, StaticIdentity
from tests.conftest import test_settings as make_settings

METHODS = frozenset({"get", "put", "post", "patch", "delete"})


def _ops(paths: dict) -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, ops in paths.items() for m in ops if m in METHODS}


def test_every_contract_operation_has_a_route_and_every_route_is_in_the_contract() -> None:
    url = "postgresql+pg8000://unused:unused@127.0.0.1:1/unused"  # 라우트만 보므로 연결하지 않는다
    container = build_container(make_settings(url), engine=create_engine(url), storage=FakeStorage(),
                                identity=StaticIdentity(), notifier=LogNotifier())
    server = _ops(create_app(container).openapi()["paths"])
    contract = _ops(yaml.safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf-8"))["paths"])
    assert sorted(contract - server) == [], "계약에는 있는데 서버 라우트가 없는 작업"
    assert sorted(server - contract) == [], "서버에는 있는데 계약에 없는 작업 (계약 먼저)"
