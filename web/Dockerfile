FROM node:22-alpine AS build
WORKDIR /web
# SSL 검사 프록시가 있는 망: deploy/certs/*.crt를 신뢰한다 (빌드 컨텍스트 밖이라 compose가 certs를 추가 컨텍스트로 넘긴다)
COPY --from=certs . /usr/local/share/ca-certificates/extra/
RUN cat /usr/local/share/ca-certificates/extra/*.crt > /tmp/extra-ca.pem 2>/dev/null || true
ENV NODE_EXTRA_CA_CERTS=/tmp/extra-ca.pem
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
