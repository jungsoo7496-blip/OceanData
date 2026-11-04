# ADR-0004: 에이전트는 Go 단일 실행파일로 만든다

- 상태: accepted
- 날짜: 2026-11-04
- 관련: 06·15번

## 맥락
기업 PC·선내 PC·엣지 박스에 설치한다. 설치 권한·보안 정책이 제각각이다. GPT v0.05는 Python 에이전트였다.

## 결정
Go로 만들고 Windows·Linux 단일 실행파일로 배포한다. 부품은 fsnotify, modernc.org/sqlite, kardianos/service, 업데이트 라이브러리(PoC) 같은 허용 라이선스 라이브러리를 쓴다.

## 결과
런타임 설치가 필요 없고 서비스 등록·서명 업데이트가 쉽다. 서버(Python)와 언어가 둘이 되지만 API 계약(contracts/)에서 Go 클라이언트를 생성해 차이를 줄인다.
