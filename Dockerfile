# Pinned by digest: the image changes only when this line does (refresh it with
# `docker buildx imagetools inspect python:3.12-slim`, take the index digest, and re-run CI).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The app runs as this user. On a server, the mounted data, key and backup directories must belong to it:
#   sudo chown -R 10001:10001 /srv/ca-mode/data /srv/ca-mode/key /srv/ca-mode/backups   (docs/hosting.md)
RUN groupadd --system --gid 10001 camode \
    && useradd --system --uid 10001 --gid camode --home-dir /app --no-create-home --shell /usr/sbin/nologin camode \
    && mkdir -p /app/.data /run/ca-mode /backups \
    && chown camode:camode /app/.data /run/ca-mode /backups \
    && chmod 700 /app/.data /run/ca-mode /backups

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
# Operator commands (python -m scripts.plans / backup / page_views) run inside the container.
COPY scripts ./scripts

USER camode:camode

EXPOSE 8080
# One worker, always: unlocked keys, rate limits and private chats live in process memory
# (app.db.claim_single_process() refuses a second process). On a deploy, requests in flight get
# --timeout-graceful-shutdown seconds to finish; Compose's stop_grace_period is a little longer.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-graceful-shutdown ${GRACEFUL_SHUTDOWN_SECONDS:-90}"]
