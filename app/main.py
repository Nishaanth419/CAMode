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
from datetime import date
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import backup, cas, chats, goal_planning, goal_portfolio, goal_suitability, investigations, memory, privacy, sealing, snapshots, unlock, users, vault
from .agent import PortfolioContext, configured_providers, default_provider, run_agent
from .context import observed_sources, retrieve_context, wants_goal_calculator
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
    # Omitted: the server default (AI_PROVIDER), so a bare request never picks a cloud provider by itself.
    aiProvider: Literal["ollama", "openai", "groq"] | None = None


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


class CasUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=100)
    # Base64 of a PDF up to cas.MAX_PDF_BYTES (4 characters per 3 bytes).
    pdfBase64: str = Field(min_length=1, max_length=cas.MAX_PDF_BYTES * 4 // 3 + 4)


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Compare with the snapshot on or before this day; omitted: the previous saved day.
    baselineDate: str | None = Field(default=None, examples=["2026-09-01"])

    @field_validator("baselineDate")
    @classmethod
    def _valid_date(cls, value: str | None) -> str | None:
        try:
            return investigations.parse_baseline_date(value)
        except investigations.InvestigationError as exc:
            raise ValueError(str(exc)) from exc


class RecoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    userId: str
    recoveryCode: str
    newPin: str


def session_id(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def current_user(request: Request) -> dict[str, Any] | None:
    """Return the signed-in profile with ``keys`` (unlocked if this session entered the PIN)."""
    sid = session_id(request)
    user_id = users.session_user_id(sid)
    user = users.get_user(user_id) if user_id else None
    if user:
        user["keys"] = unlock.keys_for_session(user, sid)
    return user


def history_gate(user: dict[str, Any] | None, *, needs_consent: bool = False) -> JSONResponse | None:
    """Return the error response for a route that reads the profile's history, or None if it may proceed."""
    if not user:
        return profile_required()
    if not user["keys"].is_unlocked:
        return unlock_required()
    if needs_consent and not privacy.has_current_consent(user["id"]):
        return consent_required()
    return None


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


def unlock_required() -> JSONResponse:
    return JSONResponse({"error": "Enter your PIN to unlock your history.", "unlockRequired": True}, status_code=403)


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
    locked = bool(user) and not unlock.keys_for_session(user, sid).is_unlocked
    connected = False
    if user:
        chats.purge_expired(user["id"])
        connected = bool(await users.user_access_token(user["id"]))
    response = JSONResponse({
        "connected": connected,
        "user": public_user(user) if user else None,
        "locked": locked,
        "consentRequired": bool(user) and not privacy.has_current_consent(user["id"]),
        "storageMode": user["storageMode"] if user else None,
        "profiles": users.list_profiles(),
    })
    response.set_cookie(value=sid, **cookie_options(request))
    return response


def _unlocked_body(user: dict[str, Any], unlocked: unlock.Unlocked) -> dict[str, Any]:
    body: dict[str, Any] = {"user": public_user(user)}
    if unlocked.recovery_code:
        body["recoveryCode"] = unlocked.recovery_code
    return body


def _signed_in_response(
    request: Request, user: dict[str, Any], unlocked: unlock.Unlocked, status_code: int = 200,
) -> JSONResponse:
    old_sid = session_id(request)
    sid = users.sign_in(old_sid, user["id"])
    if old_sid:
        unlock.forget_session(old_sid)
    unlock.activate(sid, user["id"], unlocked.private_key)
    response = JSONResponse(_unlocked_body(user, unlocked), status_code=status_code)
    response.set_cookie(value=sid, **cookie_options(request))
    return response


@app.post("/api/users", status_code=201)
async def create_profile(body: CreateProfileRequest, request: Request):
    try:
        display_name = users.clean_display_name(body.displayName)
        users.validate_new_pin(body.pin)
        pin_hash = await asyncio.to_thread(users.hash_pin, body.pin)
        user = users.create_user(display_name, pin_hash)
        unlocked = await asyncio.to_thread(unlock.create_keys, user["id"], body.pin)
    except users.DuplicateProfileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except users.ProfileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.info("profile_created", extra={"user_id": user["id"]})
    return _signed_in_response(request, user, unlocked, status_code=201)


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
    unlocked = await asyncio.to_thread(unlock.unlock_with_pin, user, body.pin)
    return _signed_in_response(request, user, unlocked)


@app.post("/api/session/unlock")
async def unlock_session(body: ConfirmPinRequest, request: Request):
    """Unlock the signed-in profile's history after a restart."""
    user = current_user(request)
    if not user:
        return profile_required()
    try:
        valid = await asyncio.to_thread(users.check_pin, user, body.pin)
    except users.ProfileLockedError as exc:
        return pin_error(exc)
    if not valid:
        return pin_error()
    unlocked = await asyncio.to_thread(unlock.unlock_with_pin, user, body.pin)
    unlock.activate(session_id(request), user["id"], unlocked.private_key)
    return _unlocked_body(user, unlocked)


@app.post("/api/session/recover")
async def recover_profile(body: RecoverRequest, request: Request):
    """Set a new PIN with the recovery code, sign in, and return a replacement recovery code."""
    user = users.get_user(body.userId)
    if user is None:
        return JSONResponse({"error": "Incorrect profile or recovery code."}, status_code=401)
    try:
        unlocked = await asyncio.to_thread(unlock.recover, user["id"], body.recoveryCode, body.newPin)
    except users.ProfileLockedError as exc:
        return pin_error(exc)
    except sealing.WrongSecretError:
        return JSONResponse({"error": "Incorrect profile or recovery code."}, status_code=401)
    except users.ProfileError as exc:  # Includes NoRecoveryCodeError and an unacceptable new PIN.
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.info("profile_recovered", extra={"user_id": user["id"]})
    return _signed_in_response(request, user, unlocked)


@app.post("/api/session/logout")
async def sign_out(request: Request):
    sid = session_id(request)
    user_id = users.session_user_id(sid)
    if sid:
        users.sign_out(sid)
        unlock.forget_session(sid)
    if user_id:
        chats.forget_private_chats(user_id)
    return {"user": None}


@app.get("/api/profile")
async def get_profile(request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return {
        "displayName": user["displayName"], "profile": users.profile_fields(user, user["keys"]),
        "updatedAt": users.profile_updated_at(user, user["keys"]),
    }


@app.put("/api/profile")
async def put_profile(body: ProfileFields, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    profile = users.update_profile(user["id"], body, user["keys"])
    updated = users.get_user(user["id"]) or user
    return {"displayName": user["displayName"], "profile": profile, "updatedAt": users.profile_updated_at(updated, user["keys"])}


# ── Goals, decisions and saved context ────────────────────────────────────────

class GoalPlanRequest(BaseModel):
    """A goal's planning inputs (saved or still being edited) and, optionally, the returns to compare."""

    model_config = ConfigDict(extra="forbid")

    currency: memory.Currency
    targetAmount: int = Field(ge=1, le=100_000_000_000)
    # Exactly one of the date and the horizon (years from today) is given.
    targetDate: date | None = None
    horizonYears: int | None = Field(default=None, ge=1, le=memory.MAX_HORIZON_YEARS)
    savedAmount: int = Field(default=0, ge=0, le=100_000_000_000)
    monthlyContribution: int = Field(default=0, ge=0, le=1_000_000_000)
    assumedReturnPct: float | None = Field(default=None, ge=0, le=30)
    inflationPct: float | None = Field(default=None, ge=0, le=15)
    flexibility: memory.Flexibility | None = None
    liquidityNeed: memory.Level | None = None
    riskTolerance: memory.Level | None = None
    lossCapacity: memory.Level | None = None
    # Yearly returns to compare instead of the default scenarios (no growth and the assumption ± 2 points).
    annualReturnPcts: list[float] | None = Field(default=None, min_length=1, max_length=goal_planning.MAX_SCENARIOS)
    # The saved goal being edited: its assigned holdings count toward the preview, as they do on the saved goal.
    goalId: str | None = Field(default=None, pattern=memory.ID_PATTERN)

    @model_validator(mode="after")
    def _one_date(self) -> "GoalPlanRequest":
        if (self.targetDate is None) == (self.horizonYears is None):
            raise ValueError("Give a target date or a horizon, not both and not neither.")
        return self


def _today() -> date:
    return snapshots.local_now().date()


def _not_found(what: str) -> JSONResponse:
    return JSONResponse({"error": f"{what} not found."}, status_code=404)


@app.get("/api/goals")
async def list_goals(request: Request):
    """The profile's goals, each with its plan, suitability and assigned holdings calculated today."""
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return await asyncio.to_thread(goal_portfolio.goals_overview, user, _today())


@app.post("/api/goals", status_code=201)
async def create_goal(body: memory.GoalInput, request: Request):
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    today = _today()
    try:
        goal = memory.create_goal(user["id"], body, user["keys"], today=today)
    except memory.LimitReachedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except memory.InvalidGoalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    view = await asyncio.to_thread(goal_portfolio.goal_with_mapping, user, goal, today)
    return JSONResponse(view, status_code=201, headers={"Location": f"/api/goals/{goal['id']}"})


@app.post("/api/goals/plan")
async def plan_goal(body: GoalPlanRequest, request: Request):
    """Calculate a plan without saving anything, for the goal editor's preview and what-if returns."""
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    today = _today()
    inputs = body.model_dump(mode="json", exclude={"annualReturnPcts", "horizonYears", "goalId"})
    if body.horizonYears is not None:
        inputs["targetDate"] = goal_planning.date_after_years(today, body.horizonYears).isoformat()
    mapped = None
    if body.goalId is not None:
        if memory.get_goal(user["id"], body.goalId, user["keys"]) is None:
            return _not_found("Goal")
        # Rupee holdings count only toward a rupee goal, as on the saved goal; amounts are never converted.
        if body.currency == "INR":
            mapped = await asyncio.to_thread(goal_portfolio.counted_value, user, body.goalId)
    try:
        plan = goal_planning.plan_goal(inputs, today, return_rates=body.annualReturnPcts, mapped_amount=mapped)
    except goal_planning.PlanningError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    suitability = goal_suitability.evaluate(inputs, today)
    if warning := goal_suitability.assumption_warning(inputs.get("assumedReturnPct"), suitability):
        plan = {**plan, "warnings": [*plan["warnings"], warning]}
    return {**plan, "suitability": suitability, "followUps": goal_planning.follow_ups(inputs, today)}


# Declared before /api/goals/{goal_id}, which would otherwise take "allocations" as an id.
@app.get("/api/goals/allocations")
async def get_goal_allocations(request: Request):
    """Every holding in the latest snapshot with how it is split across goals, and what is still unassigned."""
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return await asyncio.to_thread(goal_portfolio.allocations_view, user)


@app.put("/api/goals/allocations")
async def put_goal_allocation(body: memory.AllocationInput, request: Request):
    """Replace how one holding is split across goals. Bookkeeping only: nothing is bought or sold."""
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    try:
        return await asyncio.to_thread(goal_portfolio.assign, user, body)
    except goal_portfolio.NoSnapshotError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except memory.LimitReachedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (goal_portfolio.UnknownHoldingError, memory.UnknownGoalError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@app.get("/api/goals/{goal_id}")
async def get_goal(goal_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    goal = memory.get_goal(user["id"], goal_id, user["keys"])
    if goal is None:
        return _not_found("Goal")
    return await asyncio.to_thread(goal_portfolio.goal_with_mapping, user, goal, _today())


@app.put("/api/goals/{goal_id}")
async def update_goal(goal_id: str, body: memory.GoalInput, request: Request):
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    today = _today()
    try:
        goal = memory.update_goal(user["id"], goal_id, body, user["keys"], today=today)
    except memory.InvalidGoalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    if goal is None:
        return _not_found("Goal")
    return await asyncio.to_thread(goal_portfolio.goal_with_mapping, user, goal, today)


@app.delete("/api/goals/{goal_id}")
async def delete_goal(goal_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return {"deleted": True} if memory.delete_goal(user["id"], goal_id, user["keys"]) else _not_found("Goal")


@app.get("/api/decisions")
async def list_decisions(request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return {"maxDecisions": memory.MAX_DECISIONS, "decisions": memory.list_decisions(user["id"], user["keys"])}


@app.post("/api/decisions", status_code=201)
async def create_decision(body: memory.DecisionInput, request: Request):
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    try:
        decision = memory.create_decision(user["id"], body, user["keys"])
    except memory.LimitReachedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except memory.UnknownGoalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse(decision, status_code=201, headers={"Location": f"/api/decisions/{decision['id']}"})


@app.put("/api/decisions/{decision_id}")
async def update_decision(decision_id: str, body: memory.DecisionInput, request: Request):
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    try:
        decision = memory.update_decision(user["id"], decision_id, body, user["keys"])
    except memory.UnknownGoalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return decision or _not_found("Decision")


@app.delete("/api/decisions/{decision_id}")
async def delete_decision(decision_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return {"deleted": True} if memory.delete_decision(user["id"], decision_id) else _not_found("Decision")


@app.get("/api/context")
async def get_saved_context(request: Request):
    """Everything the assistant may draw on, grouped by where it came from."""
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    keys = user["keys"]
    return {
        "confirmed": {
            "preferences": {
                "fields": users.profile_fields(user, keys), "updatedAt": users.profile_updated_at(user, keys),
                "source": memory.SOURCE_USER,
            },
            "goals": memory.list_goals(user["id"], keys),
            "decisions": memory.list_decisions(user["id"], keys),
        },
        "observed": observed_sources(user),
        "inferred": {
            "stored": False,
            "note": "CA Mode does not save details the assistant infers in chats. Goal projections are calculated "
                    "when needed from your saved goals and are never stored.",
        },
    }


@app.delete("/api/context")
async def clear_saved_context(request: Request):
    """Delete the profile's goals, decisions and profile details. Chats, snapshots and statements are kept."""
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    memory.clear_saved_context(user["id"])
    logger.info("saved_context_cleared", extra={"user_id": user["id"]})
    return {"cleared": True}


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
    unlock.forget_user(user["id"])
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
    keys = await asyncio.to_thread(unlock.keys_from_pin, user, body.pin)
    user["keys"] = keys
    logger.info("profile_data_exported", extra={"user_id": user["id"]})
    return JSONResponse(
        privacy.export_data(
            user, snapshots.all_snapshots(user["id"], keys), cas.all_statements(user["id"], keys),
            investigations.all_investigations(user["id"], keys), memory.export_context(user["id"], keys),
        ),
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


@app.post("/api/users/me/recovery-code")
async def new_recovery_code(body: ConfirmPinRequest, request: Request):
    """Replace the recovery code; the old one stops working."""
    user = current_user(request)
    if not user:
        return profile_required()
    try:
        valid = await asyncio.to_thread(users.check_pin, user, body.pin)
    except users.ProfileLockedError as exc:
        return pin_error(exc)
    if not valid:
        return pin_error()
    code = await asyncio.to_thread(unlock.replace_recovery_code, user, body.pin)
    logger.info("recovery_code_replaced", extra={"user_id": user["id"]})
    return {"recoveryCode": code}


# ── CAS statements ─────────────────────────────────────────────────────────────

@app.post("/api/cas", status_code=201)
async def upload_cas(body: CasUploadRequest, request: Request):
    """Parse a CAS PDF in memory and save only its holdings, sealed with the profile's keys."""
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    if user["storageMode"] != "device":
        return JSONResponse({"error": "Private mode saves no history. Choose to save history to upload a CAS."}, status_code=409)
    try:
        pdf = base64.b64decode(body.pdfBase64, validate=True)
    except ValueError:
        return JSONResponse({"error": "The file could not be read. Try uploading it again."}, status_code=400)
    try:
        statement = await asyncio.to_thread(cas.parse_statement, pdf, body.password)
    except cas.CASError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    summary = cas.store_statement(user["id"], statement, user["keys"])
    logger.info("cas_uploaded", extra={"user_id": user["id"], "source": statement["source"]})
    return summary


@app.get("/api/cas")
async def list_cas(request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return {"statements": cas.list_statements(user["id"], user["keys"])}


@app.delete("/api/cas/{statement_id}")
async def delete_cas(statement_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    if not cas.delete_statement(user["id"], statement_id):
        return JSONResponse({"error": "Statement not found."}, status_code=404)
    return {"deleted": True}


# ── Backups ────────────────────────────────────────────────────────────────────

@app.get("/api/backups")
async def backup_status(request: Request):
    if not current_user(request):
        return profile_required()
    return backup.status()


@app.post("/api/backups", status_code=201)
async def create_backup(request: Request):
    """Back up the whole database now (all profiles; history stays sealed with each PIN)."""
    user = current_user(request)
    if not user:
        return profile_required()
    try:
        path = await asyncio.to_thread(backup.create_backup)
    except backup.BackupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    logger.info("manual_backup_created", extra={"user_id": user["id"]})
    return {"file": path.name, **backup.status()}


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
    if blocked := history_gate(user):
        return blocked
    history = snapshots.networth_history(user["id"], days, keys=user["keys"])
    return {
        "latest": _snapshot_summary(snapshots.latest_snapshot(user["id"], keys=user["keys"])),
        "history": [{"date": point["date"], "networthInr": point["networthInr"]} for point in history],
        "schedule": f"{snapshots.SNAPSHOT_TIME:%H:%M} {snapshots.SNAPSHOT_TIMEZONE_NAME}",
    }


@app.get("/api/portfolio")
async def portfolio_figures(request: Request):
    """Computed figures and every holding from the latest snapshot, for the overview pages."""
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    snapshot = snapshots.latest_snapshot(user["id"], keys=user["keys"])
    if not snapshot:
        return {"available": False}
    figures = await asyncio.to_thread(snapshots.portfolio_figures, snapshot)
    return {"available": True, **_snapshot_summary(snapshot), **figures}


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


# ── Investigations ─────────────────────────────────────────────────────────────

@app.post("/api/investigations", status_code=201)
async def create_investigation(body: InvestigationRequest, request: Request):
    """Compare the latest saved snapshot with an earlier one, check the data, and save the result.

    Reads only saved snapshots (never INDmoney), so repeating the request has no side
    effect beyond another saved run; no idempotency key is needed.
    """
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    try:
        record = await investigations.investigate_portfolio_change(user, baseline_date=body.baselineDate, trigger="api")
    except investigations.InvestigationStorageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    logger.info("investigation_run", extra={"user_id": user["id"], "status": record["status"]})
    if not record["saved"]:
        # Private mode, or the result could not be saved: nothing was created.
        return JSONResponse(record, status_code=200)
    return JSONResponse(record, status_code=201, headers={"Location": f"/api/investigations/{record['id']}"})


@app.get("/api/investigations")
async def list_investigations(request: Request, limit: int = Query(default=20, ge=1, le=investigations.KEEP_INVESTIGATIONS)):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return {"investigations": investigations.list_investigations(user["id"], user["keys"], limit=limit)}


@app.get("/api/investigations/{investigation_id}")
async def get_investigation(investigation_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    record = investigations.get_investigation(user["id"], investigation_id, user["keys"])
    if record is None:
        return JSONResponse({"error": "Investigation not found."}, status_code=404)
    return record


@app.delete("/api/investigations/{investigation_id}")
async def delete_investigation(investigation_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    if not investigations.delete_investigation(user["id"], investigation_id):
        return JSONResponse({"error": "Investigation not found."}, status_code=404)
    return {"deleted": True}


# ── Chats ──────────────────────────────────────────────────────────────────────

@app.get("/api/chats")
async def list_chats(request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    return {"chats": chats.list_chats(user)}


@app.post("/api/chats", status_code=201)
async def create_chat(request: Request, body: CreateChatRequest):
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    return chats.create_chat(user, body.agentType)


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
    chat = chats.read_chat(user, chat_id)
    if chat is None:
        return JSONResponse({"error": "Chat not found."}, status_code=404)
    return chat


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str, request: Request):
    user = current_user(request)
    if blocked := history_gate(user):
        return blocked
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
    provider = body.aiProvider or default_provider()
    if not message.strip() or len(message) > 8000:
        return JSONResponse({"error": "Provide a message with no more than 8,000 characters."}, status_code=400)
    user = current_user(request)
    if blocked := history_gate(user, needs_consent=True):
        return blocked
    data = chats.read_chat(user, body.chatId)
    if data is None:
        return JSONResponse({"error": "Chat not found in this account."}, status_code=404)
    if body.agentType != data["agentType"]:
        return JSONResponse({"error": "This conversation belongs to a different agent. Start a new chat to switch agents."}, status_code=409)
    history = data.get("messages", [])
    saved_context = retrieve_context(user, message, history, include_holdings=body.agentType == "portfolio")
    token = None
    context = None
    # Saved-goal tools are offered only to profiles that have a goal, and the calculator only to requests that read
    # as goal arithmetic, so every other request is unchanged.
    has_goals = memory.count_goals(user["id"]) > 0
    calculator = [goal_portfolio.goal_calculator_tool()] if wants_goal_calculator(message, history) else []
    if body.agentType != "portfolio":
        goal_tools = goal_portfolio.goal_tools(user, include_holdings=False) if has_goals else []
        if goal_tools or calculator:
            context = PortfolioContext(local_tools=tuple([*goal_tools, *calculator]))
    if body.agentType == "portfolio":
        token = await users.user_access_token(user["id"])
        if not token:
            return auth_error("Connect your INDmoney account to use Portfolio Analyzer.")
        extra_tools = [investigations.investigation_tool(user, provider=provider, chat_id=data["id"])]
        if has_goals:
            extra_tools += goal_portfolio.goal_tools(user, include_holdings=True)
        extra_tools += calculator
        context = snapshots.portfolio_context(user, extra_tools=extra_tools)
    try:
        answer = await run_agent(
            message, MCP_RESOURCE, token, history, body.agentType, provider, context=context, saved_context=saved_context,
        )
    except Exception as exc:
        if body.agentType == "portfolio" and is_indmoney_auth_error(exc):
            refreshed_token = await users.user_access_token(user["id"], force_refresh=True)
            if not refreshed_token:
                return auth_error("Your INDmoney session has expired. Reconnect your account and try again.")
            try:
                answer = await run_agent(
                    message, MCP_RESOURCE, refreshed_token, history, body.agentType, provider,
                    context=context, saved_context=saved_context,
                )
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
