# 이 패키지 단계에서 쓰는 검사 명령. 개발 환경 명령(up, migrate, test 등)은 01번 스펙에서 Kiro가 추가한다.
PYTHON ?= python3

.PHONY: contracts-check traceability traceability-write

contracts-check:
	$(PYTHON) tools/validate_contracts.py

traceability:
	$(PYTHON) tools/check_traceability.py

traceability-write:
	$(PYTHON) tools/check_traceability.py --write
