"""The streamed answer: the provider call's streaming twin, the draft side channel and the SSE route."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from app import agent, chat_stream, main
from app.agent import AgentRunError

REAL_CLIENT = httpx.AsyncClient
URL = "https://provider.test/v1/chat/completions"


def sse_body(*chunks, done: bool = True) -> bytes:
    lines = [f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta(**fields):
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": None}]}


def finish(reason: str, **extra):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}], **extra}


def transport(body: bytes, status: int = 200, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, content=body, headers={"Content-Type": "text/event-stream"})
    return httpx.MockTransport(handler)


async def stream(body: bytes, status: int = 200, seen: list | None = None):
    events: list[tuple[str, str]] = []
    async with REAL_CLIENT(transport=transport(body, status, seen)) as client:
        response = await agent._streamed_post(client, URL, {}, {"model": "m", "messages": []}, lambda kind, text: events.append((kind, text)))
    return response, events


@pytest.mark.asyncio
async def test_content_split_over_chunks_becomes_one_message():
    response, events = await stream(sse_body(delta(content="Hel"), delta(content="lo "), delta(content="there"), finish("stop")))
    message = response.json()["choices"][0]
    assert message["message"] == {"role": "assistant", "content": "Hello there"}
    assert message["finish_reason"] == "stop"
    assert events == [("discard", ""), ("text", "Hel"), ("text", "lo "), ("text", "there")]


@pytest.mark.asyncio
async def test_request_asks_for_a_stream_with_usage():
    seen: list = []
    await stream(sse_body(finish("stop")), seen=seen)
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_tool_call_split_by_index_is_assembled():
    first = delta(tool_calls=[{"index": 0, "id": "c1", "type": "function", "function": {"name": "get_", "arguments": ""}}])
    second = delta(tool_calls=[{"index": 0, "function": {"name": "holdings", "arguments": '{"a":'}}])
    third = delta(tool_calls=[{"index": 0, "function": {"arguments": "1}"}}, {"index": 1, "id": "c2", "function": {"name": "x", "arguments": "{}"}}])
    response, _ = await stream(sse_body(first, second, third, finish("tool_calls")))
    message = response.json()["choices"][0]["message"]
    assert message["content"] is None
    assert [(call["id"], call["function"]["name"], call["function"]["arguments"]) for call in message["tool_calls"]] == [
        ("c1", "get_holdings", '{"a":1}'), ("c2", "x", "{}")]


@pytest.mark.asyncio
async def test_tool_call_in_one_chunk_without_index_is_kept():
    whole = delta(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "t", "arguments": '{"q":2}'}}])
    response, _ = await stream(sse_body(whole, finish("tool_calls")))
    assert response.json()["choices"][0]["message"]["tool_calls"][0]["function"] == {"name": "t", "arguments": '{"q":2}'}


@pytest.mark.asyncio
async def test_finish_reason_length_and_usage_match_the_plain_response():
    usage = {"prompt_tokens": 11, "completion_tokens": 7}
    response, _ = await stream(sse_body(delta(content="cut"), finish("length"), {"choices": [], "usage": usage}))
    plain = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "cut"}, "finish_reason": "length"}], "usage": usage})
    assert agent._completion_summary(response, 1) == agent._completion_summary(plain, 1)
    assert agent._completion_summary(response, 1)["prompt_tokens"] == 11


@pytest.mark.asyncio
async def test_groq_usage_is_read_and_missing_usage_is_left_out():
    response, _ = await stream(sse_body(delta(content="x"), finish("stop", x_groq={"usage": {"prompt_tokens": 3, "completion_tokens": 2}})))
    assert agent._completion_summary(response, 1)["completion_tokens"] == 2
    response, _ = await stream(sse_body(delta(content="x"), finish("stop")))
    assert "usage" not in response.json()
    assert agent._completion_summary(response, 1)["prompt_tokens"] is None


@pytest.mark.asyncio
async def test_reasoning_is_never_sent_to_the_sink():
    response, events = await stream(sse_body(
        delta(reasoning="thinking about it"), delta(reasoning_content="more"), delta(content="Answer"), finish("stop")))
    assert [text for kind, text in events if kind == "text"] == ["Answer"]
    assert "thinking" not in response.text


@pytest.mark.asyncio
async def test_discard_comes_first_and_again_when_tool_calls_follow_text():
    call = delta(tool_calls=[{"index": 0, "id": "c", "function": {"name": "t", "arguments": "{}"}}])
    _, events = await stream(sse_body(delta(content="Let me look"), call, finish("tool_calls")))
    assert [kind for kind, _ in events] == ["discard", "text", "discard"]


@pytest.mark.asyncio
async def test_mid_stream_dict_error_is_a_400_the_groq_check_detects():
    error = {"error": {"code": "tool_use_failed", "message": "bad", "failed_generation": "<function=get_holdings>"}}
    response, events = await stream(sse_body(delta(content="part"), error))
    assert response.status_code == 400
    assert agent._failed_tool_generation(response) == "<function=get_holdings>"
    assert events[-1] == ("discard", "")


@pytest.mark.asyncio
async def test_mid_stream_string_error_is_a_500_that_matches():
    response, _ = await stream(sse_body({"error": "error parsing tool call: raw='x'"}))
    assert response.status_code == 500 and "error parsing tool call" in response.text


@pytest.mark.asyncio
async def test_non_success_upstream_keeps_status_and_body():
    response, events = await stream(b'{"error":{"message":"nope"}}', status=401)
    assert response.status_code == 401 and response.json()["error"]["message"] == "nope"
    assert events == [("discard", "")]


@pytest.mark.asyncio
async def test_a_sink_that_raises_does_not_break_the_stream():
    def broken(kind: str, text: str) -> None:
        raise RuntimeError("sink down")
    async with REAL_CLIENT(transport=transport(sse_body(delta(content="ok"), finish("stop")))) as client:
        response = await agent._streamed_post(client, URL, {}, {}, broken)
    assert response.json()["choices"][0]["message"]["content"] == "ok"


# ── The agent loops ────────────────────────────────────────────────────────────

def patch_provider(monkeypatch, handler):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_previous_loop_streams_and_the_content_event_is_the_guarded_text(monkeypatch):
    seen: list = []
    raw = "A budget tracks income and spending."

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, content=sse_body(
            delta(content="A budget tracks "), delta(content="income and spending."), finish("stop")))
    patch_provider(monkeypatch, handler)
    drafts: list[tuple[str, str]] = []
    events = [event async for event in agent.stream_agent(
        "What is a budget?", "https://unused", None, [], "finance", "openai", on_draft=lambda kind, text: drafts.append((kind, text)))]
    assert "".join(text for kind, text in drafts if kind == "text") == raw
    assert ("content", {"text": raw}) in events
    assert all(name != "delta" for name, _ in events)
    assert seen[0]["stream"] is True


@pytest.mark.asyncio
async def test_previous_loop_sends_no_stream_key_without_a_sink(monkeypatch):
    seen: list = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "A budget plan."}, "finish_reason": "stop"}]})
    patch_provider(monkeypatch, handler)
    answer = await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "openai")
    assert answer == "A budget plan." and "stream" not in seen[0]


@pytest.mark.asyncio
async def test_provider_call_streams_and_matches_the_plain_completion(monkeypatch):
    message = {"role": "assistant", "content": "Done."}
    config = {"model": "m", "label": "Test", "base_url": "https://provider.test/v1", "api_key": "k", "timeout": 5}
    plain_client = REAL_CLIENT(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})))
    streamed_client = REAL_CLIENT(transport=transport(sse_body(delta(content="Do"), delta(content="ne."), finish("stop"))))

    async def completion(client, on_draft):
        return [item async for item in agent._provider_call(
            client, config, "openai", [{"role": "user", "content": "hi"}], None, 1, 0, {"done": True}, on_draft)][-1]
    drafts: list[tuple[str, str]] = []
    async with plain_client, streamed_client:
        plain = await completion(plain_client, None)
        streamed = await completion(streamed_client, lambda kind, text: drafts.append((kind, text)))
    assert streamed == plain
    assert "".join(text for kind, text in drafts if kind == "text") == "Done."


# ── The route ──────────────────────────────────────────────────────────────────

SSE = {"Accept": "text/event-stream"}


def parse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if lines:
            out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


def post(client, chat, headers=None, message="What is diversification?"):
    return client.post("/api/chat", headers=headers or {}, json={"message": message, "chatId": chat["id"], "agentType": "finance"})


class Answer(str):
    trace = [{"type": "model", "model": "m"}]


@pytest.fixture
def chat_id(client, signed_in):
    return client.post("/api/chats", json={"agentType": "finance"}).json()


def test_stream_sends_deltas_a_reset_after_text_then_one_answer(client, chat_id, monkeypatch):
    async def fake(*args, on_draft=None, **kwargs):
        on_draft("discard", "")  # before any text: no reset
        on_draft("text", "First ")
        on_draft("text", "try")
        on_draft("discard", "")
        on_draft("discard", "")  # nothing sent since the last reset
        on_draft("text", "Final")
        return Answer("Final answer.")
    monkeypatch.setattr(main, "run_agent", fake)
    response = post(client, chat_id, SSE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"
    names = [name for name, _ in parse_events(response.text)]
    assert names == ["delta", "delta", "reset", "delta", "answer"]
    assert response.text.startswith(": open")
    saved = client.get(f"/api/chats/{chat_id['id']}").json()
    assert [item["content"] for item in saved["messages"]] == ["What is diversification?", "Final answer."]


def test_answer_event_equals_the_plain_body_and_headers(client, chat_id, monkeypatch):
    async def fake(*args, **kwargs):
        return Answer("Same words.")
    monkeypatch.setattr(main, "run_agent", fake)
    plain = post(client, chat_id)
    streamed = post(client, chat_id, SSE)
    assert isinstance(plain.text, str) and plain.headers["content-type"].startswith("text/plain")
    (name, data), = parse_events(streamed.text)
    assert name == "answer" and data["text"] == plain.text == "Same words."
    assert data["trace"] == plain.headers["x-agent-trace"]


def test_stream_pre_start_failures_keep_their_json_status(client, chat_id, monkeypatch):
    missing = client.post("/api/chat", headers=SSE, json={"message": "hi", "chatId": "nope", "agentType": "finance"})
    assert missing.status_code == 404 and missing.headers["content-type"].startswith("application/json")
    wrong = client.post("/api/chat", headers=SSE, json={"message": "hi", "chatId": chat_id["id"], "agentType": "portfolio"})
    assert wrong.status_code == 409
    user = client.get("/api/session").json()["user"]
    main._chats_in_flight.add(user["id"])
    busy = post(client, chat_id, SSE)
    assert busy.status_code == 429 and busy.json()["chatInFlight"] is True
    assert user["id"] in main._chats_in_flight  # a refused request must not free someone else's slot


@pytest.mark.parametrize("code, status, key", [("model_busy", 503, "modelBusy"), ("chat_deadline", 504, "chatDeadline"), ("provider_error", 502, "code")])
def test_failures_after_the_start_are_error_events(client, chat_id, monkeypatch, code, status, key):
    async def fake(*args, **kwargs):
        raise AgentRunError("boom", [], code=code, retry_after=7)
    monkeypatch.setattr(main, "run_agent", fake)
    plain = post(client, chat_id)
    (name, data), = parse_events(post(client, chat_id, SSE).text)
    assert name == "error" and data["status"] == status == plain.status_code
    assert key in data and data["error"] == plain.json()["error"]


def test_keepalive_comment_is_sent_while_the_model_works(client, chat_id, monkeypatch):
    monkeypatch.setattr(chat_stream, "KEEPALIVE_SECONDS", 0.05)

    async def fake(*args, **kwargs):
        await asyncio.sleep(0.3)
        return Answer("Late.")
    monkeypatch.setattr(main, "run_agent", fake)
    text = post(client, chat_id, SSE).text
    assert ": keepalive" in text and parse_events(text)[-1][0] == "answer"


def test_a_sink_failure_in_the_route_does_not_fail_the_answer(client, chat_id, monkeypatch):
    async def fake(*args, on_draft=None, **kwargs):
        on_draft("text", "x")
        return Answer("Fine.")
    monkeypatch.setattr(main, "run_agent", fake)
    assert parse_events(post(client, chat_id, SSE).text)[-1][1]["text"] == "Fine."


def test_without_the_accept_header_the_route_is_plain_text(client, chat_id, monkeypatch):
    async def fake(*args, **kwargs):
        assert "on_draft" not in kwargs
        return Answer("Plain.")
    monkeypatch.setattr(main, "run_agent", fake)
    response = post(client, chat_id)
    assert response.headers["content-type"].startswith("text/plain") and response.text == "Plain."


def test_unexpected_exception_is_a_generic_error_event(client, chat_id, monkeypatch):
    async def fake(*args, **kwargs):
        raise ValueError("secret detail")
    monkeypatch.setattr(main, "run_agent", fake)
    text = post(client, chat_id, SSE).text
    assert "secret detail" not in text


@pytest.mark.asyncio
async def test_closing_the_stream_leaves_the_task_to_finish_and_save(client, chat_id, monkeypatch):
    sid = client.cookies.get(main.SESSION_COOKIE)
    request = Request({"type": "http", "headers": [(b"cookie", f"{main.SESSION_COOKIE}={sid}".encode())], "scheme": "http", "server": ("t", 80), "path": "/api/chat", "query_string": b""})
    user = main.current_user(request)
    release = asyncio.Event()

    async def fake(*args, on_draft=None, **kwargs):
        on_draft("text", "Hello")
        await release.wait()
        return Answer("Saved after the browser left.")
    monkeypatch.setattr(main, "run_agent", fake)
    confirmed: list = []
    monkeypatch.setattr(main.limits, "confirm_question", lambda reservation: confirmed.append(reservation))
    body = main.ChatRequest(message="Hi there", chatId=chat_id["id"], agentType="finance")
    main._chats_in_flight.add(user["id"])  # what chat() does before handing off
    started = await main._start_streamed_chat(body, request, user, "Hi there", "openai")
    assert not isinstance(started, main.Response)
    stream, task = started
    events = stream.events(task)
    assert await events.__anext__() == ": open\n\n"
    assert "Hello" in await events.__anext__()
    await events.aclose()  # the browser left
    assert not task.done() and user["id"] in main._chats_in_flight
    release.set()
    await task
    assert user["id"] not in main._chats_in_flight
    assert confirmed
    assert main.chats.read_chat(user, chat_id["id"])["messages"][-1]["content"] == "Saved after the browser left."
