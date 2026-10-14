"""Local FastAPI app: profiles, INDmoney OAuth, MCP, snapshots, and private chat history."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import chats, privacy, snapshots, users, vault
from .agent import configured_providers, default_provider, run_agent
from .chats import MAX_CHATS_PER_USER, MAX_HISTORY_MESSAGES
from .db import connect_db, initialize_db
from .mcp_client import fetch_mcp_session, filter_portfolio_tools
from .users import (
    INDMONEY_REVOKE_URL,
    INDMONEY_TOKEN_URL,
    MCP_RESOURCE,
    SESSION_MAX_AGE_SECONDS,
    ProfileFields,
    get_session,
    random_token,
    save_session,
    touch_session,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ca_mode")

MCP_RESOURCE_METADATA = "https://mcp.indmoney.com/.well-known/oauth-protected-resource/mcp"
SESSION_COOKIE = "indmoney_session"
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "frontend"

__all__ = [
    "app", "connect_db", "initialize_db", "get_session", "save_session", "touch_session",
    "MAX_CHATS_PER_USER", "MAX_HISTORY_MESSAGES",
]

initialize_db()
vault.check_key()
users.encrypt_stored_secrets()


def _scheduler_enabled() -> bool:
    return os.getenv("SNAPSHOT_SCHEDULER_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    stop = asyncio.Event()
    task = asyncio.create_task(snapshots.run_scheduler(stop, mcp_url=MCP_RESOURCE)) if _scheduler_enabled() else None
    try:
        yield
    finally:
        stop.set()
        if task:
            try:
                await asyncio.wait_for(task, timeout=10)
            except TimeoutError:
                task.cancel()


app = FastAPI(title="CA Mode", docs_url=None, redoc_url=None, lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    chatId: str
    agentType: Literal["finance", "portfolio"] = "finance"
    aiProvider: Literal["openai", "groq"] = "openai"


class CreateChatRequest(BaseModel):
    agentType: Literal["finance", "portfolio"] = "finance"


class CreateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str
    pin: str


class SignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    userId: str
    pin: str


class ConfirmPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str


def session_id(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def current_user(request: Request) -> dict[str, Any] | None:
    user_id = users.session_user_id(session_id(request))
    return users.get_user(user_id) if user_id else None


def public_user(user: dict[str, Any]) -> dict[str, str]:
    return {"id": user["id"], "displayName": user["displayName"]}


def trace_header(trace: list[dict[str, Any]]) -> str:
    compact = []
    for step in trace[:12]:
        item = {key: value for key, value in step.items() if key != "result_preview" and not key.startswith("_")}
        if isinstance(item.get("arguments"), str):
            item["arguments"] = item["arguments"][:180]
        compact.append(item)
    return json.dumps(compact, ensure_ascii=True, separators=(",", ":"))


def request_origin(request: Request) -> str:
    configured = os.getenv("APP_ORIGIN", "").strip().rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return f"{proto}://{host}"


def is_secure_request(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def cookie_options(request: Request) -> dict[str, Any]:
    return {"key": SESSION_COOKIE, "httponly": True, "secure": is_secure_request(request), "samesite": "lax", "path": "/", "max_age": SESSION_MAX_AGE_SECONDS}


def auth_error(message: str) -> JSONResponse:
    return JSONResponse({"error": message, "loginUrl": "/auth/indmoney/connect"}, status_code=401, headers={"Cache-Control": "no-store"})


def profile_required() -> JSONResponse:
    return JSONResponse({"error": "Choose your profile to continue.", "profileRequired": True}, status_code=401)


def consent_required() -> JSONResponse:
    return JSONResponse(
        {"error": "Review and accept how CA Mode stores your data to continue.", "consentRequired": True},
        status_code=403,
    )


def pin_error(exc: users.ProfileLockedError | None = None) -> JSONResponse:
    if exc is not None:
        return JSONResponse({"error": str(exc)}, status_code=429, headers={"Retry-After": str(exc.retry_after_seconds)})
    return JSONResponse({"error": "Incorrect PIN."}, status_code=401)


def is_indmoney_auth_error(error: Exception) -> bool:
    details = str(error).lower()
    return "401" in details and any(term in details for term in ("invalid_token", "authentication required", "unauthorized"))


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/providers")
async def list_ai_providers():
    return {"providers": configured_providers(), "defaultProvider": default_provider()}


# ── Session and profiles ───────────────────────────────────────────────────────

@app.get("/api/session")
async def local_session(request: Request):
    sid = session_id(request)
    if sid:
        touch_session(sid)
    else:
        sid = random_token()
        save_session(sid, {})
    user_id = users.session_user_id(sid)
    user = users.get_user(user_id) if user_id else None
    connected = False
    if user:
        chats.purge_expired(user["id"])
        connected = bool(await users.user_access_token(user["id"]))
    response = JSONResponse({
        "connected": connected,
        "user": public_user(user) if user else None,
        "consentRequired": bool(user) and not privacy.has_current_consent(user["id"]),
        "storageMode": user["storageMode"] if user else None,
        "profiles": users.list_profiles(),
    })
    response.set_cookie(value=sid, **cookie_options(request))
    return response


def _signed_in_response(request: Request, user: dict[str, Any], status_code: int = 200) -> JSONResponse:
    sid = users.sign_in(session_id(request), user["id"])
    response = JSONResponse({"user": public_user(user)}, status_code=status_code)
    response.set_cookie(value=sid, **cookie_options(request))
    return response


@app.post("/api/users", status_code=201)
async def create_profile(body: CreateProfileRequest, request: Request):
    try:
        display_name = users.clean_display_name(body.displayName)
        users.validate_new_pin(body.pin)
        pin_hash = await asyncio.to_thread(users.hash_pin, body.pin)
        user = users.create_user(display_name, pin_hash)
    except users.DuplicateProfileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except users.ProfileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.info("profile_created", extra={"user_id": user["id"]})
    return _signed_in_response(request, user, status_code=201)


@app.post("/api/session/login")
async def sign_in(body: SignInRequest, request: Request):
    user = users.get_user(body.userId)
    if user is None:
        return JSONResponse({"error": "Incorrect profile or PIN."}, status_code=401)
    try:
        valid = await asyncio.to_thread(users.check_pin, user, body.pin)
    except users.ProfileLockedError as exc:
        return JSONResponse(
            {"error": str(exc), "retryAfterSeconds": exc.retry_after_seconds},
            status_code=429, headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if not valid:
        return JSONResponse({"error": "Incorrect profile or PIN."}, status_code=401)
    logger.info("profile_signed_in", extra={"user_id": user["id"]})
    return _signed_in_response(request, user)


@app.post("/api/session/logout")
async def sign_out(request: Request):
    sid = session_id(request)
    user_id = users.session_user_id(sid)
    if sid:
        users.sign_out(sid)
    if user_id:
        chats.forget_private_chats(user_id)
    return {"user": None}


@app.get("/api/profile")
async def get_profile(request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    return {"displayName": user["displayName"], "profile": user["profile"]}


@app.put("/api/profile")
async def put_profile(body: ProfileFields, request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    return {"displayName": user["displayName"], "profile": users.update_profile(user["id"], body)}


async def revoke_indmoney(user: dict[str, Any]) -> None:
    stored = user["indmoney"]
    if not (stored.get("accessToken") and stored.get("clientId") and stored.get("clientSecret")):
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(INDMONEY_REVOKE_URL, data={
                "token": stored["accessToken"], "token_type_hint": "access_token",
                "client_id": stored["clientId"], "client_secret": stored["clientSecret"],
            })
    except Exception:
        logger.exception("INDmoney token revocation failed")


@app.delete("/api/users/me")
async def delete_profile(body: ConfirmPinRequest, request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    try:
        valid = await asyncio.to_thread(users.check_pin, user, body.pin)
    except users.ProfileLockedError as exc:
        return pin_error(exc)
    if not valid:
        return pin_error()
    await revoke_indmoney(user)
    users.delete_user(user["id"])
    chats.forget_private_chats(user["id"])
    logger.info("profile_deleted", extra={"user_id": user["id"]})
    return {"deleted": True}


# ── Privacy: consent, storage mode, export ────────────────────────────────────

@app.get("/api/privacy")
async def get_privacy(request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    return privacy.privacy_state(user)


@app.post("/api/privacy/consent")
async def accept_privacy(body: privacy.ConsentRequest, request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    try:
        privacy.record_consent(user, body)
    except privacy.StaleNoticeError as exc:
        return JSONResponse({"error": str(exc), "policyVersion": privacy.POLICY_VERSION}, status_code=409)
    snapshots.prune_snapshots(user["id"])
    logger.info("privacy_consent_recorded", extra={"user_id": user["id"], "storage_mode": body.storageMode})
    updated = users.get_user(user["id"])
    assert updated is not None  # current_user() just read this profile.
    return privacy.privacy_state(updated)


@app.post("/api/users/me/export")
async def export_profile_data(body: ConfirmPinRequest, request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    try:
        valid = await asyncio.to_thread(users.check_pin, user, body.pin)
    except users.ProfileLockedError as exc:
        return pin_error(exc)
    if not valid:
        return pin_error()
    logger.info("profile_data_exported", extra={"user_id": user["id"]})
    return JSONResponse(
        privacy.export_data(user),
        headers={"Content-Disposition": 'attachment; filename="ca-mode-export.json"'},
    )


@app.delete("/api/users/me/history")
async def clear_profile_history(request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    privacy.clear_history(user["id"])
    logger.info("profile_history_cleared", extra={"user_id": user["id"]})
    return {"cleared": True}


# ── INDmoney OAuth ─────────────────────────────────────────────────────────────

async def register_oauth_client(redirect_uri: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Discover INDmoney's OAuth server and dynamically register this app as a client."""
    async with httpx.AsyncClient(timeout=30) as client:
        resource_response = await client.get(MCP_RESOURCE_METADATA)
        resource_response.raise_for_status()
        servers = resource_response.json().get("authorization_servers", [])
        if not servers:
            raise HTTPException(502, "INDmoney did not advertise an OAuth authorization server")
        metadata_response = await client.get(f"{servers[0].rstrip('/')}/.well-known/oauth-authorization-server")
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        registration = await client.post(metadata["registration_endpoint"], json={
            "client_name": "CA Mode", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        })
        if not registration.is_success:
            raise HTTPException(502, f"INDmoney OAuth client registration failed: HTTP {registration.status_code} {registration.text[:300]}")
        client_info = registration.json()
    if not metadata.get("authorization_endpoint"):
        raise HTTPException(502, "INDmoney did not advertise an OAuth authorization endpoint")
    return metadata, client_info


@app.get("/auth/indmoney/connect")
async def connect_indmoney(request: Request):
    sid = session_id(request)
    user_id = users.session_user_id(sid)
    if not user_id or not privacy.has_current_consent(user_id):
        # The connection belongs to a profile that has accepted the privacy notice;
        # the app shows the profile picker or the notice.
        return RedirectResponse(request_origin(request) + "/", status_code=302, headers={"Cache-Control": "no-store"})
    redirect_uri = f"{request_origin(request)}/auth/indmoney/callback"
    state, verifier = random_token(), random_token()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    try:
        metadata, client_info = await register_oauth_client(redirect_uri)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("INDmoney OAuth discovery failed: %s", type(exc).__name__)
        raise HTTPException(502, "Could not reach INDmoney sign-in. Try again shortly.") from exc
    if not client_info.get("client_id") or not client_info.get("client_secret"):
        raise HTTPException(502, "INDmoney registration did not return the required client credentials")
    save_session(sid, {"state": state, "verifier": verifier, "clientId": client_info["client_id"], "clientSecret": client_info["client_secret"], "redirectUri": redirect_uri})
    authorize = metadata["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": client_info["client_id"], "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": MCP_RESOURCE, "scope": "portfolio:read market:read",
    })
    return RedirectResponse(authorize, status_code=302, headers={"Cache-Control": "no-store"})


async def refresh_snapshot_in_background(user_id: str) -> None:
    try:
        await snapshots.refresh_user(user_id, mcp_url=MCP_RESOURCE)
    except Exception:
        logger.warning("initial_snapshot_failed", extra={"user_id": user_id}, exc_info=True)


@app.get("/auth/indmoney/callback")
async def finish_indmoney_login(request: Request, background_tasks: BackgroundTasks):
    error = request.query_params.get("error_description") or request.query_params.get("error")
    if error:
        return PlainTextResponse(f"INDmoney sign-in was not completed: {error}", status_code=400)
    sid = session_id(request)
    stored = get_session(sid) if sid else {}
    user_id = users.session_user_id(sid)
    code = request.query_params.get("code")
    if not sid or not user_id or not stored.get("state") or not stored.get("verifier") or not stored.get("clientId") or not stored.get("clientSecret") or not code or request.query_params.get("state") != stored.get("state"):
        return PlainTextResponse("INDmoney sign-in could not be verified. Please connect again.", status_code=400)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token_response = await client.post(INDMONEY_TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": stored.get("redirectUri", f"{request_origin(request)}/auth/indmoney/callback"),
                "client_id": stored["clientId"], "client_secret": stored["clientSecret"],
                "code_verifier": stored["verifier"], "resource": MCP_RESOURCE,
            })
    except httpx.HTTPError as exc:
        logger.warning("INDmoney token exchange failed: %s", type(exc).__name__)
        return PlainTextResponse("Could not reach INDmoney to finish sign-in. Please connect again.", status_code=502)
    if not token_response.is_success:
        return PlainTextResponse(f"INDmoney token exchange failed: HTTP {token_response.status_code} {token_response.text[:500]}", status_code=502)
    try:
        token = token_response.json()
    except ValueError:
        token = None
    if not isinstance(token, dict) or not token.get("access_token"):
        return PlainTextResponse("INDmoney sign-in did not return an access token. Please connect again.", status_code=502)
    users.save_indmoney(user_id, {
        "clientId": stored["clientId"], "clientSecret": stored["clientSecret"],
        "accessToken": token["access_token"], "refreshToken": token.get("refresh_token"),
        "expiresAt": users.now_ms() + int(token.get("expires_in", 3600)) * 1000,
    })
    save_session(sid, {})
    user = users.get_user(user_id)
    if user and snapshots.snapshots_allowed(user):
        background_tasks.add_task(refresh_snapshot_in_background, user_id)
    return RedirectResponse(request_origin(request) + "/", status_code=302, headers={"Cache-Control": "no-store"})


@app.post("/auth/indmoney/disconnect")
async def disconnect_indmoney(request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    await revoke_indmoney(user)
    users.clear_indmoney(user["id"])
    return {"connected": False}


# ── Portfolio snapshots ────────────────────────────────────────────────────────

def _snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        "asOfDate": snapshot["asOfDate"], "capturedAt": snapshot["capturedAt"],
        "networthInr": snapshot["networthInr"], "unavailable": snapshot.get("errors", {}),
    }


@app.get("/api/snapshots")
async def list_snapshots(request: Request, days: int = Query(default=30, ge=1, le=365)):
    user = current_user(request)
    if not user:
        return profile_required()
    history = snapshots.networth_history(user["id"], days)
    return {
        "latest": _snapshot_summary(snapshots.latest_snapshot(user["id"])),
        "history": [{"date": point["date"], "networthInr": point["networthInr"]} for point in history],
        "schedule": f"{snapshots.SNAPSHOT_TIME:%H:%M} {snapshots.SNAPSHOT_TIMEZONE_NAME}",
    }


@app.post("/api/snapshots/refresh")
async def refresh_snapshot(request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    if not privacy.has_current_consent(user["id"]):
        return consent_required()
    if user["storageMode"] != "device":
        return JSONResponse({"error": "Private mode keeps no snapshots. Choose to save history to turn them on."}, status_code=409)
    if not await users.user_access_token(user["id"]):
        return auth_error("Connect your INDmoney account to save portfolio snapshots.")
    try:
        snapshot = await snapshots.refresh_user(user["id"], mcp_url=MCP_RESOURCE)
    except snapshots.SnapshotError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except Exception as exc:
        logger.warning("manual_snapshot_failed", extra={"user_id": user["id"]}, exc_info=True)
        return JSONResponse({"error": "Could not save a snapshot from INDmoney.", "details": str(exc)[:300]}, status_code=502)
    return {"latest": _snapshot_summary(snapshot)}


# ── Chats ──────────────────────────────────────────────────────────────────────

@app.get("/api/chats")
async def list_chats(request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    return {"chats": chats.list_chats(user)}


@app.post("/api/chats", status_code=201)
async def create_chat(request: Request, body: CreateChatRequest):
    user = current_user(request)
    if not user:
        return profile_required()
    if not privacy.has_current_consent(user["id"]):
        return consent_required()
    return chats.create_chat(user, body.agentType)


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str, request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    chat = chats.read_chat(user, chat_id)
    if chat is None:
        return JSONResponse({"error": "Chat not found."}, status_code=404)
    return chat


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str, request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    if not chats.delete_chat(user, chat_id):
        return JSONResponse({"error": "Chat not found."}, status_code=404)
    return {"deleted": True}


@app.get("/api/tools")
async def list_tools(request: Request):
    user = current_user(request)
    if not user:
        return profile_required()
    if not privacy.has_current_consent(user["id"]):
        return consent_required()
    token = await users.user_access_token(user["id"])
    if not token:
        return {"mcpServer": MCP_RESOURCE, "connected": False, "toolCount": 0, "tools": [], "loginUrl": "/auth/indmoney/connect"}
    try:
        session = await fetch_mcp_session(MCP_RESOURCE, token)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    tools = filter_portfolio_tools(session["tools"])
    return {"mcpServer": MCP_RESOURCE, "connected": True, "toolCount": len(tools), "tools": [{"name": tool["name"], "description": tool.get("description", "")} for tool in tools]}


@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request):
    message = body.message
    if not message.strip() or len(message) > 8000:
        return JSONResponse({"error": "Provide a message with no more than 8,000 characters."}, status_code=400)
    user = current_user(request)
    if not user:
        return profile_required()
    if not privacy.has_current_consent(user["id"]):
        return consent_required()
    data = chats.read_chat(user, body.chatId)
    if data is None:
        return JSONResponse({"error": "Chat not found in this account."}, status_code=404)
    if body.agentType != data["agentType"]:
        return JSONResponse({"error": "This conversation belongs to a different agent. Start a new chat to switch agents."}, status_code=409)
    token = None
    context = None
    if body.agentType == "portfolio":
        token = await users.user_access_token(user["id"])
        if not token:
            return auth_error("Connect your INDmoney account to use Portfolio Analyzer.")
        context = snapshots.portfolio_context(user)
    history = data.get("messages", [])
    try:
        answer = await run_agent(message, MCP_RESOURCE, token, history, body.agentType, body.aiProvider, context=context)
    except Exception as exc:
        if body.agentType == "portfolio" and is_indmoney_auth_error(exc):
            refreshed_token = await users.user_access_token(user["id"], force_refresh=True)
            if not refreshed_token:
                return auth_error("Your INDmoney session has expired. Reconnect your account and try again.")
            try:
                answer = await run_agent(message, MCP_RESOURCE, refreshed_token, history, body.agentType, body.aiProvider, context=context)
            except Exception as retry_exc:
                if is_indmoney_auth_error(retry_exc):
                    return auth_error("INDmoney rejected the renewed session. Reconnect your account and try again.")
                exc = retry_exc
            else:
                exc = None

        if exc is not None:
            logger.error("Chat agent request failed: %s", exc)
            details = str(exc)
            error = "INDmoney data request failed" if "INDmoney" in details or "MCP" in details else "Agent request failed"
            trace = getattr(exc, "trace", [])[:12]
            return JSONResponse(
                {"error": error, "details": details}, status_code=502,
                headers={"X-Agent-Trace": trace_header(trace)},
            )
    answer_text = str(answer)
    trace = getattr(answer, "trace", [])[:12]
    if answer_text.strip():
        chats.append_turn(user, data, message, answer_text, trace)
    return PlainTextResponse(answer_text, headers={
        "X-Agent-Trace": trace_header(trace),
    })


@app.get("/")
async def index():
    return FileResponse(PUBLIC_DIR / "index.html")


if PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=PUBLIC_DIR), name="static")
