# 壓測機映像檔（Cloud Run Jobs）：k6 + 上傳用的 bash／curl
# 在 repo 根目錄建置：docker build -f runner/Dockerfile -t <image> .
# k6 版本與本機開發一致（v2.3.0）；映像檔 tag 是否存在待建置時確認
FROM grafana/k6:2.3.0 AS k6

FROM alpine:3.22
RUN apk add --no-cache bash curl ca-certificates tzdata
COPY --from=k6 /usr/bin/k6 /usr/bin/k6

WORKDIR /app
# 目前放 mock 版腳本；正式腳本完成後改為正式腳本目錄
COPY k6-mock/script.js /app/k6/script.js
COPY k6-mock/data /app/k6/data
COPY runner/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && adduser -D -u 10001 k6user
USER k6user

ENV K6_SCRIPT=/app/k6/script.js
ENTRYPOINT ["/app/entrypoint.sh"]
