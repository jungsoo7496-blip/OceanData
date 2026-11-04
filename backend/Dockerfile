# API·워커 공용 이미지 (tech.md HW-4: 산출물은 컨테이너 이미지 + 환경변수)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# SSL 검사 프록시가 있는 망: deploy/certs/*.crt를 신뢰한다 (없으면 아무 일도 하지 않는다)
COPY deploy/certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates >/dev/null 2>&1 || true
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
# FFmpeg는 별도 프로세스로만 호출한다 (tech.md 2장). 빌드 구성은 17번 릴리스 고지에 기록한다.
# pango·나눔 글꼴(OFL-1.1)은 보고서 PDF(WeasyPrint)용 (10번, ADR-0018)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-nanum \
 && apt-get clean \
 && useradd --system --uid 10001 --home /app marine
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install .
COPY backend/alembic.ini ./
COPY contracts /contracts
ENV CONTRACTS_DIR=/contracts
USER marine
EXPOSE 8000
CMD ["uvicorn", "--factory", "app.main:app_factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
