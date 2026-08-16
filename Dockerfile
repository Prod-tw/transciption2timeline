# COSCUP 剪輯時間線廣播 server
#
# 只有 Python 標準函式庫，沒有任何相依套件。
# timeline.json 直接烤進 image，deploy 機器不用放任何資料檔。
# 想在不重 build 的情況下換資料，就掛一份到 /data/timeline.json：
#   docker run -v ./timeline.json:/data/timeline.json:ro \
#              -e TIMELINE_PATH=/data/timeline.json ...
#
# build context 是 _timeline/ 這層：
#   docker build -f _timeline/Dockerfile -t coscup-transcript-timeline _timeline

FROM python:3.13-alpine

LABEL org.opencontainers.image.title="COSCUP 剪輯時間線廣播" \
      org.opencontainers.image.description="把逐字稿推導出的議程切點廣播成 COSCUP Cut 可匯入的 server" \
      org.opencontainers.image.source="https://github.com/Prod-tw/coscup-timeline"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=3000 \
    TIMELINE_PATH=/app/out/timeline.json \
    MIN_CONFIDENCE=none

WORKDIR /app

COPY serve_timeline.py /app/serve_timeline.py
COPY out/timeline.json /app/out/timeline.json

RUN adduser -D -H -u 10001 coscup && chown -R coscup /app
USER coscup

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import os,urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/v1/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["python3", "/app/serve_timeline.py"]
