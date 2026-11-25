"""The word-by-word draft of a streamed answer: the provider call's streaming twin, the draft side channel and the
delta / reset events on the chat route (progress events are in test_chat_stream.py)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

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


def parse_events(raw: bytes) -> list[tuple[str, dict]]:
    out = []
    for block in raw.decode("utf-8").split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if lines:
            out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


def stream_events(client, chat):
    with client.stream("POST", "/api/chat", headers=SSE,
                       json={"message": "What is diversification?", "chatId": chat["id"], "agentType": "finance"}) as response:
        assert response.status_code == 200
        return parse_events(response.read())


class Answer(str):
    trace = [{"type": "model", "model": "m"}]


@pytest.fixture
def chat_id(client, signed_in):
    return client.post("/api/chats", json={"agentType": "finance"}).json()


@pytest.fixture(autouse=True)
def _no_streams_left():
    yield
    main._stream_tasks.clear()


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
    events = stream_events(client, chat_id)
    assert [name for name, _ in events] == ["progress", "delta", "delta", "reset", "delta", "answer"]
    assert [data["text"] for name, data in events if name == "delta"] == ["First ", "try", "Final"]
    assert events[-1][1]["text"] == "Final answer."
    saved = client.get(f"/api/chats/{chat_id['id']}").json()
    assert [item["content"] for item in saved["messages"]] == ["What is diversification?", "Final answer."]


def test_a_sink_failure_in_the_route_does_not_fail_the_answer(client, chat_id, monkeypatch):
    async def fake(*args, on_draft=None, **kwargs):
        agent._send_draft(on_draft, "text", "x")
        return Answer("Fine.")
    monkeypatch.setattr(main, "run_agent", fake)
    monkeypatch.setattr(chat_stream.DraftMapper, "__call__", lambda self, kind, text="": 1 / 0)
    events = stream_events(client, chat_id)
    assert events[-1][0] == "answer" and events[-1][1]["text"] == "Fine."


def test_without_a_stream_run_agent_gets_no_draft_callback(client, chat_id, monkeypatch):
    seen = {}

    async def fake(*args, **kwargs):
        seen.update(kwargs)
        return Answer("Plain.")
    monkeypatch.setattr(main, "run_agent", fake)
    response = client.post("/api/chat", json={"message": "What is diversification?", "chatId": chat_id["id"], "agentType": "finance"})
    assert response.text == "Plain." and "on_draft" not in seen and "on_event" not in seen


def test_draft_mapper_resets_only_after_text():
    sent = []
    mapper = chat_stream.DraftMapper(sent.append)
    mapper("discard", "")
    mapper("text", "a")
    mapper("discard", "")
    mapper("discard", "")
    assert [item.get(chat_stream.STREAM_EVENT) for item in sent] == ["delta", "reset"]
    assert chat_stream.frame(sent[0]) == b'event: delta\ndata: {"text":"a"}\n\n'
    assert chat_stream.frame({"seq": 1, "kind": "start"}).startswith(b"event: progress\n")
