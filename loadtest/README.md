# 부하 시험 (17번 요구사항 3, T12)

[Locust](https://locust.io)(MIT, OSS-CANDIDATES 채택)로 동시 업로드·관측값 배치·자산 검색을 돌린다.
**합격 수치는 코드에 넣지 않는다** (tech.md HW-8) — 결과 JSON을 운영 하드웨어 실측과 비교한다.

```bash
pip install -r loadtest/requirements.txt          # 개발 도구 (런타임 의존성 아님)
export LOAD_TOKEN="$(…ORG 사용자 또는 API 키 토큰…)"   # deploy/compose/keycloak/README.md의 개발 토큰
export LOAD_SOURCE_ID=dev_00000000000000000000000302  # 업로드 대상 원천 장비 (시드: A 선수 카메라)
export LOAD_SENSOR_ID=dev_00000000000000000000000303  # 관측값 원천 장비 (시드: A 수질 센서)
locust -f loadtest/locustfile.py --host http://localhost:8000 --headless -u 20 -r 5 -t 2m \
       --json > var/loadtest.json
```

설정값: 사용자 수(`-u`), 증가율(`-r`), 시간(`-t`), 파일 크기(`LOAD_FILE_BYTES`, 기본 1MiB), 관측값 배치 크기(`LOAD_BATCH`, 기본 100).
대상 주소만 바꾸면 운영 환경에서 같은 시나리오로 실측한다 (T12 실측, T18 노드·디스크 추가 전후 비교).
작업 큐 워커를 멈춘 상태(`docker compose stop worker`)에서 돌려도 업로드·관측값 수신은 계속되어야 한다 (T12-c, 자동 시험은
`backend/tests/acceptance/test_t12_concurrency.py`).
