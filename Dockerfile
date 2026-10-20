# syntax=docker/dockerfile:1
# check=error=true
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

WORKDIR /build
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    pip install --prefix=/install -r requirements.txt


FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app && \
    install -d -o 10001 -g 10001 -m 700 /data /backups

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data \
    BACKUP_DIR=/backups \
    PORT=8080

COPY --from=builder /install /usr/local
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 frontend ./frontend
# For `python -m scripts.backup` and `python -m scripts.page_views` inside the container.
COPY --chown=10001:10001 scripts ./scripts

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4).read()"]

# Exactly one worker: unlocked PIN keys, private-mode chats, per-profile locks and the
# snapshot scheduler live in this process's memory. `exec` makes uvicorn PID 1 so it
# receives SIGTERM from `docker stop`.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1"]
