# Deployment, backups, logs and environment variables

Moved out of the always-loaded CLAUDE.md. Read this when changing `deploy/`, the Dockerfile, auto-update, probes, logging, backups, or any environment variable. Operator steps: [../hosting.md](../hosting.md).

## Modules

- **`app/backup.py`**: Daily database backups (`backup_if_due()` from the scheduler loop makes `kind="daily"` copies; `create_backup()` defaults to `kind="manual"` (`-manual` file suffix: CLI, deploys, admin, in-app) via SQLite's online backup API with `journal_mode = DELETE` so each backup is one file, verified before it is kept), each kind pruned on its own (`BACKUP_KEEP` daily, `BACKUP_MANUAL_KEEP` manual, so manual copies never push daily ones out), `list_backups(kind)`, `backup_kind()`, `verify_backup()`, and `restore_backup()` (moves the current database aside). Never copies the master key. CLI: `python -m scripts.backup now|list|verify|restore`

## Hosted setup, updates and containers

- Hosted setup (`deploy/`, README "Host on a server"): the app and cloudflared run on a server behind Cloudflare Access; Ollama stays on the Mac, shared with `tailscale serve --tcp 11434` and reached as `ollama.internal` (`extra_hosts`), because Ollama bound to loopback returns 403 for a Tailscale name or IP in the `Host` header. Model and classifier calls use a `router.CONNECT_TIMEOUT_SECONDS` (10 s) connect limit, so a sleeping Mac fails fast with a plain message
- Automatic updates (docs/hosting.md "Automatic updates"): `.github/workflows/deploy.yml` ("Publish image", after CI succeeds on a push to `main`; actions pinned to SHAs) publishes `ghcr.io/<owner>/<repo>:sha-<commit>` for amd64 and arm64 and moves `:main` to it only while that commit is still the head of `main` (checked before and after the build), so an old run never downgrades the server; on the server `deploy/update.sh` (systemd `ca-mode-update.timer`, every 5 min; reads `CA_MODE_IMAGE` from `deploy/.env` itself) pulls, backs up (a manual copy), tags the image `ca-mode:current`, restarts, waits for the `/healthz` healthcheck (never `/readyz`: a sleeping Mac must not roll back) and on failure re-tags `ca-mode:previous` as current, remembering the failed image in `/srv/ca-mode/update/bad` (also checked against the running image, so a hand-started bad image is left again). `deploy/docker-compose.image.yml` always runs `ca-mode:current` (`pull_policy: never`), so a plain `docker compose up -d` keeps the rolled-back image. A failed run starts `ca-mode-update-failed.service` (journal, optional `CA_MODE_ALERT_URL` POST)
- Compose layout (`deploy/`): `docker-compose.yml` is the app only; the way in is `docker-compose.caddy.yml` or `docker-compose.tunnel.yml` (cloudflared), chosen with `COMPOSE_FILE` in `deploy/.env`; every service logs with `json-file` rotation (10m × 5); the app has `stop_grace_period: 100s` and uvicorn `--timeout-graceful-shutdown` `GRACEFUL_SHUTDOWN_SECONDS` (90). Changes to `deploy/` files are not shipped by the image: list the operator's manual steps in docs/hosting.md "Upgrading an existing server"
- Container (`Dockerfile`, `.dockerignore`): `python:3.12-slim` pinned by digest, runs as `camode` (uid 10001; the server's `/srv/ca-mode/{data,key,backups}` must be owned by it), `--workers 1`. `db.claim_single_process()` (lifespan) takes an exclusive `flock` on `<APP_DATA_DIR>/app.lock` once per process and raises `SecondProcessError` in a second process
- Logs (`app/logs.py`): `logs.configure()` (in `main`, instead of `basicConfig` alone) gives the stderr handler `KeyValueFormatter`: `LEVEL:logger:message` then every `extra=` field as `key=value` (JSON-quoted when needed), so `admin_*` lines carry `admin=<email>`; `AccessLogFilter` on `uvicorn.access` strips query strings (OAuth `code`/`state`) and drops `/healthz` and `/readyz` lines
- Probes: `/healthz` is liveness (Docker healthcheck, `update.sh` rollbacks); `/readyz` runs `SELECT 1` on the database and `GET /api/tags` on Ollama (when it is the default provider) within `READY_TIMEOUT_SECONDS` (3) and returns `503` with `checks` when either fails: for monitoring only, never restarts

## Routes

- Backup routes: `GET /api/backups` (status), `POST /api/backups` (back up now, `backup` rate rule); both 404 unless `main.backups_in_app()`: never with an admin page, else `BACKUPS_IN_APP` when set, else only `AUTH_MODE=local` with an unset or localhost `APP_ORIGIN` (they show the server path and back up every profile: the operator uses `POST /api/admin/backups`). `/api/session` returns `backupsInApp` and `app.js` hides the profile dialog's `.backup-row` when it is `false`

- Other: `GET /api/providers`, `GET /api/tools`, `GET /healthz`, `GET /readyz`

## Environment variables

- `OLLAMA_MODEL` (enables the local provider; `ca-mode`), `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`; Compose sets `host.docker.internal`), `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_ROUTER_MODEL`
- `OPENAI_API_KEY` / `GROQ_API_KEY`: enable providers; `OPENAI_MODEL` / `GROQ_MODEL`, `OPENAI_BASE_URL` / `GROQ_BASE_URL`: overrides
- `AI_PROVIDER`: default provider (`ollama` by default, `openai` or `groq`); users can switch in the app
- `GROQ_INPUT_TOKEN_BUDGET`, `GROQ_COMPLETION_TOKEN_LIMIT`: Groq limits
- `REQUEST_ROUTER` (`model` default, or `keywords`), `OPENAI_ROUTER_MODEL` / `GROQ_ROUTER_MODEL`: request classifier
- `MCP_SERVER_URL`: INDmoney MCP endpoint (default `https://mcp.indmoney.com/mcp`)
- `APP_ORIGIN`: public origin for the OAuth callbacks (INDmoney and Google)
- `AUTH_MODE` (`local` default, or `google`), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ALLOW_PROFILE_CLAIMS` (true): sign-in
- `ADMIN_EMAILS`: comma-separated Google accounts allowed into `/admin` (unset: admin off)
- `FUND_STALE_DAYS` (5): days after a NAV date before fund returns are marked old (also `nav_stale` on `#/returns`)
- `MF_DATA_ENABLED` (true; `conftest.py` sets false), `AMFI_NAV_URL` (`https://www.amfiindia.com/spages/NAVAll.txt`), `MFAPI_URL` (`https://api.mfapi.in`): the daily AMFI NAV download and the benchmark history
- `AGENT_GRAPH_ENABLED` (true), `AGENT_STEP_TIMEOUT_SECONDS` (60), `AGENT_DEADLINE_SECONDS` (180, below `CHAT_DEADLINE_SECONDS`; a model call still running then is cut off and the model answers without tools): the general agent
- `MODEL_CONCURRENCY` (else `OLLAMA_NUM_PARALLEL`, else 1), `MODEL_QUEUE_SECONDS` (20), `CHAT_DEADLINE_SECONDS` (240; below the proxy timeout, 90 behind Cloudflare), `OLLAMA_ROUTER_TIMEOUT_SECONDS` (30): `model_gate` and the classifier
- `PLAN_STEP_TIMEOUT_SECONDS` (60), `PLAN_DEADLINE_SECONDS` (240): planned answers
- `INVESTIGATION_TOLERANCE_PCT` (0.5), `INVESTIGATION_TOLERANCE_INR` (1), `INVESTIGATION_STALE_HOURS` (36): investigation checks
- `SNAPSHOT_TIME`, `SNAPSHOT_TIMEZONE`, `SNAPSHOT_RETENTION_DAYS`, `SNAPSHOT_SCHEDULER_ENABLED`: snapshot job (defaults `09:00`, `Asia/Kolkata`, `365`, `true`; falls back to a fixed +05:30 offset when tz data is missing)
- `APP_MASTER_KEY_FILE` / `APP_MASTER_KEY`: token encryption key (default file `~/.config/ca-mode/master.key`, created on first start)
- `BACKUP_DIR` (default `~/ca-mode-backups`), `BACKUP_KEEP` (14), `BACKUP_ENABLED` (true): daily backups; `conftest.py` points `BACKUP_DIR` at a temp folder
- `ZERODHA_MCP_URL` (`https://mcp.kite.trade/mcp`), `FYERS_MCP_URL`, `KOTAK_MCP_URL`, `UPSTOX_MCP_URL`, `DHAN_MCP_URL`, `GROWW_MCP_URL`: the brokers' MCP servers
- `LOGO_DEV_TOKEN` (unset: stocks show a letter), `LOGOS_ENABLED` (true): holding logos (`logos.py`); `conftest.py` sets `LOGOS_ENABLED=false` and removes the token
- `PAGE_VIEWS_ENABLED` (true): anonymous local page-view counts
- `PRICING_CONTACT_EMAIL`: address behind the `/pricing` buttons (unset or invalid: no buttons)
- `DAILY_QUESTION_LIMIT` (unset: unlimited; never above `limits.FREE_DAILY_QUESTIONS` (10), a larger number is lowered to it; Free plan only), `RATE_LIMITS_ENABLED` (false), `TRUSTED_PROXY` (`cloudflare` behind a Tunnel only, `caddy` behind `deploy/docker-compose.caddy.yml` only), `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY`: usage limits for a shared copy (`limits.py`); `conftest.py` clears them and the in-memory limiter for every test
- `APP_DATA_DIR`: SQLite directory (default `.data/`); `LOG_LEVEL` (default `INFO`); `PORT` (Docker, default `8080`); `GRACEFUL_SHUTDOWN_SECONDS` (Docker, default 90)
- `BACKUPS_IN_APP` (unset: only a local-mode copy on localhost), `BACKUP_MANUAL_KEEP` (5): in-app and manual backups
- Server only (`deploy/.env`, read by Compose and `update.sh`): `COMPOSE_FILE`, `MAC_TAILSCALE_IP`, `SITE_ADDRESS`, `CLOUDFLARE_TUNNEL_TOKEN`, `CA_MODE_HOME`, `CA_MODE_IMAGE`, `CA_MODE_ALERT_URL`
