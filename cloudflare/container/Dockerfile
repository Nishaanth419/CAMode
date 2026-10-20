# syntax=docker/dockerfile:1
# check=error=true
# The CA Mode image for Cloudflare Containers: the same app as ../../Dockerfile, plus Litestream, which
# restores SQLite from R2 when the (always fresh) container disk starts empty and streams every write back.
# Build context is the repository root (wrangler.jsonc: image_build_context "..").
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

WORKDIR /build
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    pip install --prefix=/install -r requirements.txt


FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS litestream

ADD --checksum=sha256:cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d \
    https://github.com/benbjohnson/litestream/releases/download/v0.5.17/litestream-0.5.17-linux-x86_64.tar.gz /tmp/litestream.tar.gz
RUN tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin litestream && /usr/local/bin/litestream version


FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app && \
    install -d -o 10001 -g 10001 -m 700 /data

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data \
    BACKUP_ENABLED=false \
    PORT=8080

COPY --from=builder /install /usr/local
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream
COPY cloudflare/container/litestream.yml /etc/litestream.yml
COPY --chmod=755 cloudflare/container/entrypoint.sh /usr/local/bin/ca-mode-entrypoint
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 frontend ./frontend
COPY --chown=10001:10001 scripts ./scripts

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/ca-mode-entrypoint"]
