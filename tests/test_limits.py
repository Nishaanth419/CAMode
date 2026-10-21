from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import limits, main, privacy
from app.db import connect_db
from tests.conftest import sign_up

IST = timezone(timedelta(hours=5, minutes=30))


def answer_with(monkeypatch, reply="Diversification spreads risk."):
    calls = []

    async def fake_run_agent(message, *args, **kwargs):
        calls.append(message)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    return calls


def ask(client, chat_id, message="What is diversification?"):
    return client.post("/api/chat", json={"message": message, "chatId": chat_id, "agentType": "finance"})


def new_chat(client):
    return client.post("/api/chats", json={"agentType": "finance"}).json()["id"]


def stored_count(user_id):
    with connect_db() as connection:
        rows = connection.execute("SELECT day, questions FROM question_counts WHERE user_id = ?", (user_id,)).fetchall()
    return {row["day"]: row["questions"] for row in rows}


# ── Off by default ──────────────────────────────────────────────────────────────

def test_limits_are_off_by_default(client, signed_in, monkeypatch):
    calls = answer_with(monkeypatch)
    chat_id = new_chat(client)

    for _ in range(40):
        assert ask(client, chat_id).status_code == 200

    assert len(calls) == 40
    assert stored_count(signed_in["id"]) == {}
    assert "turnstileSiteKey" not in client.get("/api/session").json()


@pytest.mark.parametrize("value", ["", "0", "-3", "lots"])
def test_daily_limit_ignores_unset_zero_and_invalid_values(monkeypatch, value):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", value)

    assert limits.daily_question_limit() is None


# ── Daily question cap ─────────────────────────────────────────────────────────

def test_daily_limit_returns_429_with_a_plain_message(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "2")
    calls = answer_with(monkeypatch)
    chat_id = new_chat(client)

    assert ask(client, chat_id).status_code == 200
    assert ask(client, chat_id).status_code == 200
    blocked = ask(client, chat_id)

    assert blocked.status_code == 429
    body = blocked.json()
    assert body["dailyLimitReached"] is True
    assert body["limit"] == 2
    assert body["error"] == "You've reached today's limit of 2 questions. It resets at midnight IST."
    assert body["resetsAt"].endswith("T00:00+05:30")
    retry_after = int(blocked.headers["Retry-After"])
    assert 0 < retry_after <= 24 * 3600
    assert body["retryAfterSeconds"] == retry_after
    assert len(calls) == 2
    # The refused question is not saved in the chat.
    assert len(client.get(f"/api/chats/{chat_id}").json()["messages"]) == 4


def test_daily_limit_is_per_profile(client, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "1")
    answer_with(monkeypatch)
    sign_up(client, "Asha")
    assert ask(client, new_chat(client)).status_code == 200
    assert ask(client, new_chat(client)).status_code == 429

    client.post("/api/session/logout")
    sign_up(client, "Ravi", "1357")

    assert ask(client, new_chat(client)).status_code == 200


def test_failed_answers_do_not_use_the_daily_limit(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "1")
    chat_id = new_chat(client)
    answer_with(monkeypatch, RuntimeError("model offline"))

    assert ask(client, chat_id).status_code == 502
    answer_with(monkeypatch, "   ")
    assert ask(client, chat_id).status_code == 200

    answer_with(monkeypatch)
    assert ask(client, chat_id).status_code == 200
    assert ask(client, chat_id).status_code == 429


def test_daily_limit_resets_at_local_midnight(signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "1")
    late = datetime(2026, 10, 21, 23, 59, tzinfo=IST)

    assert limits.reserve_question(signed_in["id"], now=late).isoformat() == "2026-10-21"
    with pytest.raises(limits.DailyLimitReached) as reached:
        limits.reserve_question(signed_in["id"], now=late)
    assert reached.value.resets_at == datetime(2026, 10, 22, 0, 0, tzinfo=IST)
    assert reached.value.retry_after_seconds(late) == 60

    # 18:31 UTC is 00:01 the next day in IST.
    assert limits.reserve_question(signed_in["id"], now=datetime(2026, 10, 21, 18, 31, tzinfo=timezone.utc))
    assert limits.questions_used(signed_in["id"], now=late + timedelta(minutes=2)) == 1


def test_old_daily_counts_are_dropped(signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "5")
    for day in (18, 20, 21):
        limits.reserve_question(signed_in["id"], now=datetime(2026, 10, day, 12, tzinfo=IST))

    assert stored_count(signed_in["id"]) == {"2026-10-20": 1, "2026-10-21": 1}


def test_released_question_never_goes_below_zero(signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "3")
    now = datetime(2026, 10, 21, 12, tzinfo=IST)
    day = limits.reserve_question(signed_in["id"], now=now)

    limits.release_question(signed_in["id"], day)
    limits.release_question(signed_in["id"], day)
    limits.release_question(signed_in["id"], None)

    assert limits.questions_used(signed_in["id"], now=now) == 0


def test_daily_counts_are_deleted_with_the_profile(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "3")
    answer_with(monkeypatch)
    assert ask(client, new_chat(client)).status_code == 200

    assert client.request("DELETE", "/api/users/me", json={"pin": "2468"}).status_code == 200

    assert stored_count(signed_in["id"]) == {}


# ── Request rate limits ────────────────────────────────────────────────────────

def test_rate_limiter_slides_its_window():
    clock = [1000.0]
    limiter = limits.RateLimiter(clock=lambda: clock[0])
    rule = limits.Rule(2, 60)

    assert limiter.hit("k", rule) == 0
    clock[0] += 10
    assert limiter.hit("k", rule) == 0
    assert limiter.hit("k", rule) == 50
    assert limiter.hit("other", rule) == 0
    clock[0] += 50
    assert limiter.hit("k", rule) == 0


def test_rate_limiter_sweeps_out_expired_request_times():
    clock = [0.0]
    limiter = limits.RateLimiter(clock=lambda: clock[0])
    limiter.hit("short", limits.Rule(5, 10))
    limiter.hit("long", limits.Rule(5, 3600))
    clock[0] += limits.SWEEP_SECONDS

    limiter.hit("new", limits.Rule(5, 10))

    assert len(limiter) == 2  # "short" expired and was dropped; "long" and "new" remain.


def test_rate_limiter_forgets_keys_when_full():
    clock = [0.0]
    limiter = limits.RateLimiter(clock=lambda: clock[0], max_keys=4)
    rule = limits.Rule(1, 60)
    for index in range(4):
        clock[0] += 1
        limiter.hit(f"k{index}", rule)
    clock[0] += 1
    limiter.hit("k4", rule)

    # The two least recently seen keys were dropped, so they can hit again; the newest cannot.
    assert limiter.hit("k0", rule) == 0
    assert limiter.hit("k4", rule) > 0


def test_chat_is_rate_limited_per_profile_with_retry_after(client, signed_in, monkeypatch):
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "true")
    monkeypatch.setitem(limits.RATE_RULES, "chat", limits.GroupRules(limits.Rule(100, 60), limits.Rule(2, 60)))
    calls = answer_with(monkeypatch)
    chat_id = new_chat(client)

    assert ask(client, chat_id).status_code == 200
    assert ask(client, chat_id).status_code == 200
    blocked = ask(client, chat_id)

    assert blocked.status_code == 429
    assert blocked.json()["rateLimited"] is True
    assert blocked.json()["error"].startswith("Too many requests. Try again in ")
    assert 0 < int(blocked.headers["Retry-After"]) <= 60
    assert len(calls) == 2


def test_chat_is_rate_limited_per_ip_across_profiles(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "true")
    monkeypatch.setitem(limits.RATE_RULES, "chat", limits.GroupRules(limits.Rule(1, 60), limits.Rule(10, 60)))
    answer_with(monkeypatch)
    sign_up(client, "Asha")
    assert ask(client, new_chat(client)).status_code == 200
    client.post("/api/session/logout")
    sign_up(client, "Ravi", "1357")

    assert ask(client, new_chat(client)).status_code == 429


def test_sign_up_is_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "true")
    monkeypatch.setitem(limits.RATE_RULES, "sign_up", limits.GroupRules(limits.Rule(1, 3600), None))
    sign_up(client, "Asha")
    client.post("/api/session/logout")

    blocked = client.post("/api/users", json={"displayName": "Ravi", "pin": "1357"})

    assert blocked.status_code == 429
    assert blocked.json()["rateLimited"] is True
    assert int(blocked.headers["Retry-After"]) > 60
    assert [profile["displayName"] for profile in client.get("/api/session").json()["profiles"]] == ["Asha"]


def test_sign_in_unlock_and_recover_share_a_limit_per_profile(client, signed_in, monkeypatch):
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "true")
    monkeypatch.setitem(limits.RATE_RULES, "sign_in", limits.GroupRules(limits.Rule(100, 600), limits.Rule(3, 600)))

    assert client.post("/api/session/unlock", json={"pin": "2468"}).status_code == 200
    client.post("/api/session/logout")
    assert client.post("/api/session/login", json={"userId": signed_in["id"], "pin": "2468"}).status_code == 200
    recover = client.post("/api/session/recover", json={
        "userId": signed_in["id"], "recoveryCode": "WRONG-CODE", "newPin": "1357",
    })
    assert recover.status_code == 401

    blocked = client.post("/api/session/login", json={"userId": signed_in["id"], "pin": "2468"})
    assert blocked.status_code == 429
    assert blocked.json()["rateLimited"] is True


def test_investigations_are_rate_limited(client, signed_in, monkeypatch):
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "true")
    monkeypatch.setitem(limits.RATE_RULES, "investigation", limits.GroupRules(limits.Rule(100, 600), limits.Rule(1, 600)))

    assert client.post("/api/investigations", json={}).status_code in {200, 201}
    blocked = client.post("/api/investigations", json={})

    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_cloudflare_ip_is_used_only_behind_a_trusted_proxy(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "true")
    monkeypatch.setitem(limits.RATE_RULES, "sign_up", limits.GroupRules(limits.Rule(1, 3600), None))

    def create(name, ip):
        client.post("/api/session/logout")
        return client.post("/api/users", json={"displayName": name, "pin": "2468"}, headers={"CF-Connecting-IP": ip})

    # Untrusted: the header is ignored, so every request comes from the same peer.
    assert create("A", "203.0.113.1").status_code == 201
    assert create("B", "203.0.113.2").status_code == 429

    monkeypatch.setenv("TRUSTED_PROXY", "cloudflare")
    assert create("C", "203.0.113.3").status_code == 201
    assert create("D", "203.0.113.4").status_code == 201
    assert create("E", "203.0.113.4").status_code == 429
    # A forged, unparseable header falls back to the peer (already over its limit).
    assert create("F", "not-an-ip").status_code == 429


def test_ipv6_clients_are_grouped_by_their_64_block():
    assert limits._client_key("2001:db8:1:2:aaaa::1") == limits._client_key("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert limits._client_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert limits._client_key("nonsense") is None


# ── Cloudflare Turnstile on sign-up ─────────────────────────────────────────────

def turnstile_on(monkeypatch, *, success=True, status=200):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    sent = []

    async def fake_verify(token, *, remote_ip, transport=None):
        sent.append((token, remote_ip))

        def handler(request):
            return httpx.Response(status, json={"success": success, "error-codes": [] if success else ["invalid-input-response"]})

        return await real_verify(token, remote_ip=remote_ip, transport=httpx.MockTransport(handler))

    real_verify = limits.verify_turnstile
    monkeypatch.setattr(limits, "verify_turnstile", fake_verify)
    return sent


def test_session_shares_the_turnstile_site_key_only_when_both_keys_are_set(client, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    assert "turnstileSiteKey" not in client.get("/api/session").json()

    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    assert client.get("/api/session").json()["turnstileSiteKey"] == "site-key"


def test_sign_up_requires_a_valid_turnstile_token(client, monkeypatch):
    sent = turnstile_on(monkeypatch)
    client.get("/api/session")

    missing = client.post("/api/users", json={"displayName": "Asha", "pin": "2468"})
    assert missing.status_code == 400
    assert missing.json()["turnstileFailed"] is True

    created = client.post("/api/users", json={"displayName": "Asha", "pin": "2468", "turnstileToken": "token-1"})
    assert created.status_code == 201
    assert sent == [(None, "testclient"), ("token-1", "testclient")]


def test_sign_up_refuses_a_rejected_turnstile_token(client, monkeypatch):
    turnstile_on(monkeypatch, success=False)
    client.get("/api/session")

    response = client.post("/api/users", json={"displayName": "Asha", "pin": "2468", "turnstileToken": "used"})

    assert response.status_code == 400
    assert client.get("/api/session").json()["profiles"] == []


def test_sign_up_fails_closed_when_turnstile_is_unreachable(client, monkeypatch):
    turnstile_on(monkeypatch, status=502)
    client.get("/api/session")

    response = client.post("/api/users", json={"displayName": "Asha", "pin": "2468", "turnstileToken": "token"})

    assert response.status_code == 503
    assert response.json()["turnstileFailed"] is True


def test_turnstile_is_checked_after_the_name_and_pin(client, monkeypatch):
    sent = turnstile_on(monkeypatch)
    client.get("/api/session")

    response = client.post("/api/users", json={"displayName": "Asha", "pin": "1", "turnstileToken": "token"})

    assert response.status_code == 400
    assert sent == []  # The single-use token is not spent on a PIN that would be refused anyway.


@pytest.mark.asyncio
async def test_verify_turnstile_sends_the_secret_token_and_ip(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    seen = []

    def handler(request):
        seen.append((str(request.url), request.content.decode()))
        return httpx.Response(200, json={"success": True})

    assert await limits.verify_turnstile("tok", remote_ip="203.0.113.9", transport=httpx.MockTransport(handler))
    assert seen == [(limits.TURNSTILE_VERIFY_URL, "secret=secret-key&response=tok&remoteip=203.0.113.9")]
    assert not await limits.verify_turnstile("x" * 3000, remote_ip=None)


def test_notice_describes_the_usage_limits():
    text = " ".join(section["body"] for section in privacy.NOTICE)

    assert "number of questions your profile asked today and yesterday" in text
    assert "kept in memory only, for about an hour at most, and never saved" in text
    assert "challenges.cloudflare.com" in text
