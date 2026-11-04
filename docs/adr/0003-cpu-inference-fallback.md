# ADR-0003: GPU가 없으면 CPU로 느리게라도 실행한다

- 상태: accepted
- 날짜: 2026-11-04
- 관련: tech.md HW-2, 05·14번

## 맥락
GPU 장비는 나중에 산다. GPT v0.05는 GPU가 없으면 비식별 작업을 UNAVAILABLE로 두는 안이었다.

## 결정
추론은 InferencePort(ONNX Runtime) 뒤에 두고 기본은 CPU 실행 공급자다. GPU_ENABLED=true면 CUDA를 쓴다. 작업은 정책에 따라 CPU로 실행하거나 대기한다.

## 결과
하드웨어 전에도 비식별 흐름(탐지 → 가림 → 검수 → 공유 조건)을 끝까지 개발·시험할 수 있다. 처리 속도는 make bench로 재고 GPU 도입 후 다시 잰다.
