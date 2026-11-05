# ADR-0022: 백업 실행 환경의 PostgreSQL 16 도구와 권한 연결

- 상태: accepted
- 날짜: 2026-11-04
- 관련: 08번 요구사항 6.1·6.3·7.1·7.2, ADR-0021, T10-a·T10-b

## 맥락

API·워커 공용 이미지에 `pg_dump`·`pg_restore`가 없고, Compose가 백업 설정을 컨테이너에 전달하지 않았다.
`make backup`·`make restore-test`는 `migrate` 서비스를 실행하지만 백업용 소유자 연결이 없어 앱 계정으로 실행됐다.
GitHub Actions에서도 T10 복원이 실패했다. 기존 로그는 결과 딕셔너리가 축약되어 원인 문자열을 확인할 수 없었다.
러너의 기본 클라이언트 주버전과 DB 서버(16)가 달라질 수 있으므로 버전까지 고정해야 한다.

## 결정

1. 공용 이미지에 PGDG 서명 APT 저장소의 `postgresql-client-16`을 설치하고 도구 경로를 명시한다.
   기반 Debian의 실제 코드명을 사용한다. 패치 버전 보안 갱신은 허용한다.
   설치 방식은 [PostgreSQL 공식 Debian 안내](https://www.postgresql.org/download/linux/debian/)를 따른다.
   PostgreSQL License는 기존 허용 목록 안에 있다. FFmpeg·PDF 라이브러리의 기존 승인 상태는 바꾸지 않는다.
2. 덤프·복원 전에 도구의 주버전을 서버와 대조한다. 다르면 기존 실패 결과(`FAILED` 또는 복원 시험 `ok: false`)에
   한국어 조치 안내를 남긴다. 더 최신 도구를 쓰는 것만으로 하위 서버 복원 호환성을 보장하지 않는다.
   근거: [pg_dump 호환성 설명](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-NOTES).
3. Compose는 백업 주기·버킷·별도 S3 설정을 전달한다. `BACKUP_DATABASE_URL`은 `migrate`·`worker`에만 전달하고
   개발 기본값은 `MIGRATION_DATABASE_URL`이다. API에는 소유자 연결을 전달하지 않는다.
   운영에서는 백업·임시 DB 생성에 필요한 권한을 갖춘 별도 계정의 연결을 명시한다.
4. backend CI를 Ubuntu 24.04로 고정해 기본 APT 저장소의 PG16 클라이언트를 설치하고 경로를 명시한다.
   실제 공용 이미지도 빌드해 비루트 사용자로 두 도구를 실행한다. T10 실패는 JSON 전체를 출력한다.
   사용자 지정 `PG_DUMP_BIN`·`PG_RESTORE_BIN`이 있으면 시험도 그 경로로 도구 존재 여부를 확인한다.
5. API·JSON 스키마·도메인 코드값과 DB 구조는 바꾸지 않는다. 기존 계약을 선행 검사하고 T10의 응답 계약 검사를 유지한다.
   DB 변경이 없으므로 새 마이그레이션과 down은 해당 없음이다.

버린 대안: 배포마다 도구를 수동 설치하면 이미지에서 CLI가 실패하는 회귀를 잡기 어렵다.
배포판 기본 `postgresql-client`만 설치하면 서버와 다른 주버전이 선택될 수 있다.

## 결과

백업·복원을 같은 이미지와 PG16 도구로 실행하고, 잘못된 도구 경로는 실제 덤프·복원 전에 실패한다.
DB 서버 주버전을 바꿀 때는 이미지 패키지·경로·CI도 함께 갱신해야 한다.
기존 API·테이블 변경이 없어 되돌릴 때 데이터 마이그레이션은 필요하지 않다.
PITR·암호화·백업 회차 정리 및 운영 도구 PoC는 ADR-0021의 후속 작업으로 남는다.
