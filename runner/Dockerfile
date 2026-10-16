# 壓測機映像檔（Cloud Run Jobs）：k6 + 上傳用的 bash／curl
# 在 repo 根目錄建置：docker build -t <image> runner
# k6 版本與本機開發一致（v2.3.0）；映像檔 tag 是否存在待建置時確認
FROM grafana/k6:2.3.0 AS k6

FROM alpine:3.22
RUN apk add --no-cache bash curl ca-certificates tzdata
COPY --from=k6 /usr/bin/k6 /usr/bin/k6

WORKDIR /app
# 整個 k6/ 目錄放進映像檔，用 K6_SCRIPT 選腳本（Terraform／gcloud 設定環境變數即可切換）
COPY k6/ /app/k6/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && adduser -D -u 10001 k6user
USER k6user

# 正式腳本；打 lab／UAT 時 Job 定義要設定 BASE_URL 與 ALLOW_REMOTE_TARGET=true
ENV K6_SCRIPT=/app/k6/script.js
ENTRYPOINT ["/app/entrypoint.sh"]
