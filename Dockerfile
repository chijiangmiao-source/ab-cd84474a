FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data \
    LEASE_MS=4000

WORKDIR /srv
COPY app ./app
COPY tests ./tests

EXPOSE 8080
VOLUME ["/data"]

# 可运行产物：轨道编队控制台服务
CMD ["python", "-m", "app.server"]
