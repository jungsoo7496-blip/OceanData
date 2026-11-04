# ADR-0018: 보고서 내보내기 — PDF(WeasyPrint)·XLSX(openpyxl)·HWPX(양식 XML 치환)

- 상태: proposed (Pillow·Pyphen 라이선스 예외 승인 대기)
- 날짜: 2026-11-04
- 관련: 10번 요구사항 4.5, tech.md 2장(보고서), docs/OSS-CANDIDATES.md(WeasyPrint 채택, docxtpl 안 씀, pyhwpxlib 안 씀)

## 맥락
10번은 보고서를 PDF·XLSX·HWPX로 내보내야 한다. OSS-CANDIDATES.md는 PDF에 WeasyPrint를 채택했고, HWPX는
"템플릿 XML 치환을 스파이크로 확정"이라고 적었다. WeasyPrint를 실제로 설치해 보니 런타임 의존성 중 두 개가
라이선스 허용 목록(tech.md 3장) 밖이다.

| 패키지 | 버전 | 라이선스 | 이유 |
|---|---|---|---|
| pillow | 13.0.0 | MIT-CMU (HPND 계열 허용형) | WeasyPrint 이미지 처리 필수 |
| pyphen | 0.18.1 | GPL-2.0+ / LGPL-2.1+ / MPL-1.1 중 선택 | WeasyPrint 하이픈 처리 필수. MPL-1.1을 택한다 (파일 단위 약한 카피레프트, 수정하지 않고 그대로 배포) |

시스템 라이브러리 pango·harfbuzz(LGPL 계열)는 Debian 패키지로 설치해 동적 링크한다. 글꼴은 나눔(OFL-1.1, 허용 목록).

## 결정
1. PDF: 보고서 HTML을 WeasyPrint로 렌더링한다. `REPORT_FORMATS`에 PDF가 있고 WeasyPrint를 불러올 수 있을 때만 켠다
   (`ReportRendererPort` 어댑터, 불러오기 실패 시 기능만 꺼지고 서버는 뜬다).
2. XLSX: openpyxl(MIT) — 보고서·지표·근거 시트.
3. HWPX: 새 의존성 없이 표준 라이브러리 zipfile로 처리한다. 운영자가 올린 HWPX 양식(zip) 안의 XML에서
   `{{변수}}` 자리를 XML 이스케이프한 값으로 바꾼다. 양식 파일의 내부 구조는 해석하지 않는다.
   **확인 필요:** 한글 프로그램이 자리표시자를 여러 텍스트 조각으로 나눠 저장하면 치환되지 않는다. 실제 관리기관
   양식을 받으면(T11-f) 한글에서 열리는지와 자리표시자 작성 요령을 확인한다.
4. Pillow·Pyphen은 `tools/license-exceptions.json`에 `approved=false`로 올린다(검사는 경고만). 사람이 승인하지 않으면
   PDF를 끄고(`REPORT_FORMATS`에서 PDF 제외) WeasyPrint를 의존성에서 빼면 된다 — XLSX·HWPX는 영향 없음.

버린 대안:
- ReportLab: chardet(LGPL) 의존, 한글 조판을 직접 해야 한다.
- fpdf2: LGPL-3.0.
- LibreOffice 변환 서버: 무겁고 HWPX 출력 품질을 보장하지 못한다.
- pyhwpxlib: 일부가 BSL 1.1 (OSS-CANDIDATES.md "안 씀").

## 결과
- 같은 보고서 데이터(필드·지표·근거)에서 세 형식이 나온다. 초안은 모든 형식에 "초안"·생성 시각·데이터 기준 시각을 넣는다.
- "검증 필요" 지표는 내보낼 때 숫자 대신 "검증 필요"로 쓴다(확정 성능값으로 내보내지 않음, 요구사항 3.4).
- 감수: API 이미지가 pango·글꼴 때문에 커진다(수십 MB). 라이선스 예외 승인이 필요하다.
