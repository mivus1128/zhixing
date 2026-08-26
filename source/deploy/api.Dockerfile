FROM python:3.12-slim@sha256:401f6e1a67dad31a1bd78e9ad22d0ee0a3b52154e6bd30e90be696bb6a3d7461

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    TZ=Asia/Shanghai

RUN groupadd --gid 10001 zhixing \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin zhixing

WORKDIR /app/backend

COPY --chown=10001:10001 source/backend/zhixing /app/backend/zhixing

USER 10001:10001

EXPOSE 8765
