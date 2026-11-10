from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import agent, model_gate


def ok_response():
    return httpx.Response(200, json={}, request=httpx.Request("POST", "http://model/v1/chat/completions"))


async def slow(seconds, response=None):
    await asyncio.sleep(seconds)
    return response or ok_response()


@pytest.mark.asyncio
async def test_a_new_chat_gives_up_quickly_when_every_slot_is_taken(monkeypatch):
    monkeypatch.setenv("MODEL_QUEUE_SECONDS", "0.05")
    holder = asyncio.create_task(model_gate.call("ollama", lambda: slow(0.5)))
    await asyncio.sleep(0.01)

    started = time.monotonic()
    with model_gate.chat_budget(30), pytest.raises(model_gate.ModelBusy) as busy:
        await model_gate.call("ollama", lambda: slow(0))

    assert time.monotonic() - started < 0.3
    assert busy.value.retry_after_seconds >= 5
    assert "busy" in str(busy.value)
    holder.cancel()


@pytest.mark.asyncio
async def test_a_chat_already_answering_waits_for_its_next_slot(monkeypatch):
    monkeypatch.setenv("MODEL_QUEUE_SECONDS", "0.05")
    with model_gate.chat_budget(5):
        await model_gate.call("ollama", lambda: slow(0))  # admitted
        holder = asyncio.create_task(model_gate.call("ollama", lambda: slow(0.2)))
        await asyncio.sleep(0.01)

        response = await model_gate.call("ollama", lambda: slow(0))  # waits past the 0.05 s queue limit

    assert response.is_success
    await holder


@pytest.mark.asyncio
async def test_slots_follow_ollama_num_parallel(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "2")
    monkeypatch.setenv("MODEL_QUEUE_SECONDS", "0.05")
    first = asyncio.create_task(model_gate.call("ollama", lambda: slow(0.3)))
    await asyncio.sleep(0.01)

    assert (await model_gate.call("ollama", lambda: slow(0))).is_success  # the second slot
    assert model_gate.status()["concurrency"] == 2
    await first


@pytest.mark.asyncio
async def test_cloud_providers_are_not_queued(monkeypatch):
    monkeypatch.setenv("MODEL_QUEUE_SECONDS", "0.05")
    holder = asyncio.create_task(model_gate.call("ollama", lambda: slow(0.3)))
    await asyncio.sleep(0.01)

    assert (await model_gate.call("openai", lambda: slow(0))).is_success
    holder.cancel()


@pytest.mark.asyncio
async def test_the_chat_budget_cuts_a_call_off():
    with model_gate.chat_budget(0.05), pytest.raises(model_gate.DeadlineReached):
        await model_gate.call("ollama", lambda: slow(1))
    with model_gate.chat_budget(0), pytest.raises(model_gate.DeadlineReached):
        await model_gate.call("openai", lambda: slow(0))


@pytest.mark.asyncio
async def test_a_call_deadline_narrows_the_budget():
    with model_gate.chat_budget(30):
        with model_gate.call_deadline(time.monotonic() + 0.05), pytest.raises(model_gate.DeadlineReached):
            await model_gate.call("ollama", lambda: slow(1))
        assert model_gate.remaining() > 20  # the chat budget itself is untouched


@pytest.mark.asyncio
async def test_the_last_successful_answer_is_remembered():
    assert model_gate.status()["lastSuccessAt"] == {}
    failed = httpx.Response(500, request=httpx.Request("POST", "http://model"))

    await model_gate.call("ollama", lambda: slow(0, failed))
    assert model_gate.status()["lastSuccessAt"] == {}

    before = time.time() * 1000
    await model_gate.call("ollama", lambda: slow(0))
    assert model_gate.status()["lastSuccessAt"]["ollama"] >= before - 1


class OllamaClient:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers=None, json=None):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "A budget plans spending."}}]},
                              request=httpx.Request("POST", url))

    async def get(self, url):
        return httpx.Response(200, json={"models": [{"name": "ca-mode", "context_length": 32768}]},
                              request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_a_busy_model_ends_the_answer_with_model_busy(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "ca-mode")
    monkeypatch.setenv("MODEL_QUEUE_SECONDS", "0.05")
    monkeypatch.setattr(agent.httpx, "AsyncClient", OllamaClient)
    holder = asyncio.create_task(model_gate.call("ollama", lambda: slow(0.5)))
    await asyncio.sleep(0.01)

    with model_gate.chat_budget(30), pytest.raises(agent.AgentRunError) as failed:
        await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "ollama")

    assert failed.value.code == "model_busy"
    assert failed.value.retry_after >= 5
    holder.cancel()
