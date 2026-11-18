FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    APP_HOST=127.0.0.1 APP_PORT=8095 DATA_DIR=/data TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd -r -u 10001 -m app && mkdir -p /data && chown app /data
COPY app ./app

USER app
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://{os.environ[\"APP_HOST\"]}:{os.environ[\"APP_PORT\"]}/healthz',timeout=4)"
# 只能单进程：任务锁在内存里，保证一次只跑一个任务
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$APP_HOST\" --port \"$APP_PORT\" --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1"]
