import json

from fastapi.responses import JSONResponse, PlainTextResponse

from app import tasks
from app.chat_stream import ProgressMapper, response_event, sse


def _mapper(*tools):
    m = ProgressMapper()
    m.start()
    m.map("contract", {"contract": tasks.TaskContract("account_data", allowed_tools=frozenset(tools))})
    return m


def test_sse_frames_one_event():
    assert sse("progress", {"seq": 1}) == b'event: progress\ndata: {"seq":1}\n\n'


def test_start_is_seq_zero():
    assert ProgressMapper().start() == {"seq": 0, "kind": "start"}


def test_tool_name_is_kept_only_when_offered():
    m = _mapper("get_portfolio_snapshot")
    assert m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {}, "source": "local"}) == {
        "seq": 1, "kind": "tool_call", "name": "get_portfolio_snapshot", "source": "saved"}
    assert "name" not in m.map("tool_call", {"name": "made_up_tool", "arguments": {}, "source": "local"})


def test_contract_parts_are_remembered():
    m = ProgressMapper()
    part = tasks.TaskContract("goal_calculation", allowed_tools=frozenset({"calculate_goal_plan"}))
    m.map("contract", {"contract": tasks.TaskContract("multi_step", parts=(part,))})
    assert m.map("tool_call", {"name": "calculate_goal_plan", "arguments": {}, "source": "local"})["name"] == "calculate_goal_plan"


def test_sources_per_tool_kind():
    m = _mapper()
    src = lambda name, args=None, source="local": m.map("tool_call", {"name": name, "arguments": args or {}, "source": source})["source"]
    assert src("get_networth", source="indmoney") == "indmoney"
    assert src("get_broker_holdings", {"broker": "zerodha"}) == "broker:zerodha"
    assert src("call_broker_tool", '{"broker":"zerodha"}') == "broker:zerodha"
    assert src("get_broker_holdings", {"broker": "nope"}) == "broker"
    assert src("get_broker_holdings", "not json") == "broker"
    assert src("calculate") == "analysis"
    assert src("get_portfolio_snapshot") == "saved"


def test_progress_never_carries_arguments_results_or_figures():
    m = _mapper("get_portfolio_snapshot")
    mapped = [
        m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {"amount": 125000}, "source": "local"}),
        m.map("tool_result", {"name": "get_portfolio_snapshot", "result": "₹1,25,000"}),
        m.map("retry", {"reason": "no_account_tool", "detail": "worth ₹1,25,000"}),
        m.map("plan", {"stage": "initial", "reason": "125000 rupees"}),
    ]
    text = json.dumps(mapped)
    assert "125000" not in text and "1,25,000" not in text
    for item in mapped:
        assert set(item) <= {"seq", "kind", "name", "source", "status"}


def test_free_text_names_are_dropped():
    m = _mapper()
    assert "name" not in m.map("retry", {"reason": "Your balance is 5 lakh"})


def test_tool_result_status_and_planned_step_name():
    m = _mapper("get_portfolio_snapshot")
    m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {}, "source": "local", "step": "s1"})
    assert m.map("tool_result", {"error_kind": "failed"})["status"] == "failed"
    assert m.map("tool_result", {"errorKind": "auth"})["status"] == "failed"
    planned = m.map("tool_result", {"step": "s1", "result": "x"})
    assert planned["status"] == "ok" and planned["name"] == "get_portfolio_snapshot"
    assert m.map("tool_result", {"name": "get_portfolio_snapshot", "error": "boom"})["status"] == "failed"


def test_simple_events():
    m = ProgressMapper()
    assert m.map("route", {"category": "account_data", "source": "model"}) == {"seq": 0, "kind": "route", "name": "account_data"}
    assert m.map("context", {"items": ["a goal"]}) == {"seq": 1, "kind": "context"}
    assert m.map("model_call", {"provider": "x"}) == {"seq": 2, "kind": "model_call"}
    assert m.map("validation", {"check": "numbers", "outcome": "ok"}) == {"seq": 3, "kind": "validation", "name": "numbers"}
    assert m.waiting() == {"seq": 4, "kind": "waiting"}


def test_unsent_events_map_to_none():
    m = ProgressMapper()
    for event in ("content", "model_result", "plan_stop", "done", "error", "other"):
        assert m.map(event, {}) is None


def test_response_event_for_an_answer():
    response = PlainTextResponse("Hello", headers={
        "X-Agent-Trace": json.dumps([{"kind": "route"}]), "X-Agent-Run": "r1", "X-Questions-Remaining": "4",
        "X-Credits-Remaining": "90", "X-Credits-Charged": "2"})
    assert response_event(response) == ("answer", {
        "text": "Hello", "trace": [{"kind": "route"}], "runId": "r1", "questionsRemaining": 4,
        "creditsRemaining": 90, "creditsCharged": 2})
    assert response_event(PlainTextResponse("Hi")) == ("answer", {"text": "Hi", "trace": []})


def test_response_event_for_a_failure():
    response = JSONResponse({"error": "busy", "modelBusy": True, "retryAfterSeconds": 5}, status_code=503)
    assert response_event(response) == ("error", {"status": 503, "trace": [], "error": "busy", "modelBusy": True, "retryAfterSeconds": 5})


def _two_calls(m, key):
    m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {}, "source": "local", **({"step": "s1"} if key else {})})
    m.map("tool_call", {"name": "get_networth", "arguments": {}, "source": "indmoney", **({"step": "s2"} if key else {})})


def test_planned_results_in_reverse_order_keep_their_own_name_and_source():
    m = _mapper("get_portfolio_snapshot", "get_networth")
    _two_calls(m, True)
    second = m.map("tool_result", {"step": "s2"})
    first = m.map("tool_result", {"step": "s1"})
    assert (second["name"], second["source"]) == ("get_networth", "indmoney")
    assert (first["name"], first["source"]) == ("get_portfolio_snapshot", "saved")


def test_graph_results_in_reverse_order_keep_their_own_source():
    m = _mapper("get_portfolio_snapshot", "get_networth")
    _two_calls(m, False)
    second = m.map("tool_result", {"name": "get_networth"})
    first = m.map("tool_result", {"name": "get_portfolio_snapshot"})
    assert (second["name"], second["source"]) == ("get_networth", "indmoney")
    assert (first["name"], first["source"]) == ("get_portfolio_snapshot", "saved")


# ── The streaming route ─────────────────────────────────────────────────────────

import asyncio  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from app import chat_stream, limits, main, model_gate, plans, users  # noqa: E402
from app.agent import AgentResult, AgentRunError  # noqa: E402

STREAM = {"Accept": "text/event-stream"}


def new_chat(client, agent_type="finance"):
    return client.post("/api/chats", json={"agentType": agent_type}).json()["id"]


def parse_events(raw: bytes) -> list[tuple[str, dict]]:
    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        name = next(line[len("event: "):] for line in lines if line.startswith("event: "))
        data = "\n".join(line[len("data: "):] for line in lines if line.startswith("data: "))
        events.append((name, json.loads(data)))
    return events


def stream_raw(client, chat_id, message="What is diversification?", agent_type="finance"):
    with client.stream("POST", "/api/chat", headers=STREAM,
                       json={"message": message, "chatId": chat_id, "agentType": agent_type}) as response:
        return response, response.read()


def stream_ask(client, chat_id, message="What is diversification?", agent_type="finance"):
    response, raw = stream_raw(client, chat_id, message, agent_type)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    return parse_events(raw)


def answer_with(monkeypatch, reply="Diversification spreads risk.", events=(("route", {"category": "general_finance"}),)):
    calls = []

    async def fake_run_agent(message, *args, **kwargs):
        calls.append(kwargs)
        for event in events:
            kwargs["on_event"](*event)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    return calls


@pytest.fixture(autouse=True)
def _no_streams_left():
    yield
    main._stream_tasks.clear()


def test_wants_stream_reads_the_accept_header():
    class Fake:
        def __init__(self, accept):
            self.headers = {"accept": accept} if accept is not None else {}

    assert main.wants_stream(Fake("text/event-stream"))
    assert main.wants_stream(Fake("text/event-stream, */*"))
    assert not main.wants_stream(Fake("*/*"))
    assert not main.wants_stream(Fake(None))


def test_streamed_answer_sends_start_progress_then_one_answer(client, signed_in, monkeypatch):
    answer_with(monkeypatch)

    events = stream_ask(client, new_chat(client))

    assert [name for name, _ in events] == ["progress", "progress", "answer"]
    assert events[0][1] == {"seq": 0, "kind": "start"}
    assert events[1][1] == {"seq": 1, "kind": "route", "name": "general_finance"}
    assert events[-1][1]["text"] == "Diversification spreads risk."
    assert main._chats_in_flight == set() and main._stream_tasks == set()


def test_streamed_answer_matches_the_plain_answer(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "5")
    trace = [{"kind": "route", "category": "general_finance", "result_preview": "kept out"}]
    run_events = [{"seq": 1, "atMs": 0, "kind": "route", "category": "general_finance"}]

    async def fake_run_agent(message, *args, **kwargs):
        return AgentResult("Diversification spreads risk.", trace, events=run_events)

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    chat_id = new_chat(client)

    plain = client.post("/api/chat", json={"message": "What is diversification?", "chatId": chat_id, "agentType": "finance"})
    name, streamed = stream_ask(client, chat_id)[-1]

    assert plain.status_code == 200 and name == "answer"
    assert streamed["text"] == plain.text
    assert streamed["trace"] == json.loads(plain.headers["X-Agent-Trace"])
    assert streamed["runId"] and plain.headers["X-Agent-Run"]
    assert int(plain.headers["X-Questions-Remaining"]) == 4 and streamed["questionsRemaining"] == 3
    saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert saved[-1]["content"] == "Diversification spreads risk." and saved[-1]["runId"] == streamed["runId"]


def test_without_the_accept_header_the_answer_is_plain_text(client, signed_in, monkeypatch):
    calls = answer_with(monkeypatch, events=())

    response = client.post("/api/chat", json={"message": "What is diversification?", "chatId": new_chat(client), "agentType": "finance"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Diversification spreads risk."
    assert "X-Agent-Trace" in response.headers
    assert "on_event" not in calls[0]  # the plain path reports no progress


def test_pre_start_failures_keep_their_status_codes(client, signed_in, monkeypatch):
    calls = answer_with(monkeypatch)
    chat_id = new_chat(client)

    main._chats_in_flight.add(signed_in["id"])
    response, raw = stream_raw(client, chat_id)
    assert response.status_code == 429 and json.loads(raw)["chatInFlight"] is True
    main._chats_in_flight.clear()

    response, raw = stream_raw(client, chat_id, agent_type="portfolio")
    assert response.status_code == 409 and response.headers["content-type"].startswith("application/json")
    assert main._chats_in_flight == set()

    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "1")
    assert stream_ask(client, chat_id)[-1][0] == "answer"
    response, raw = stream_raw(client, chat_id)
    assert response.status_code == 429 and json.loads(raw)["dailyLimitReached"] is True
    assert main._chats_in_flight == set()

    monkeypatch.delenv("DAILY_QUESTION_LIMIT")
    plans.set_plan(signed_in["id"], "individual")
    from tests.test_credits import MONTHLY, set_used
    set_used(signed_in["id"], used=MONTHLY)
    response, raw = stream_raw(client, chat_id)
    assert response.status_code == 402 and json.loads(raw)["creditsExhausted"] is True
    assert main._chats_in_flight == set()
    assert len(calls) == 1


def test_pre_start_exception_releases_the_in_flight_guard(client, signed_in, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(main, "_prepare_chat", broken)

    with pytest.raises(RuntimeError):
        stream_raw(client, new_chat(client))
    assert main._chats_in_flight == set()


def test_a_busy_model_after_start_is_an_error_event(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "3")
    answer_with(monkeypatch, AgentRunError("The assistant is busy. Try again in about 20 seconds.", [],
                                           code="model_busy", retry_after=20))

    events = stream_ask(client, new_chat(client))

    name, data = events[-1]
    assert name == "error"
    assert data["status"] == 503 and data["modelBusy"] is True and data["retryAfterSeconds"] == 20
    assert data["trace"] == [] and "busy" in data["error"]
    assert limits.questions_used(signed_in["id"], now=limits.local_now()) == 0


def test_a_deadline_after_start_is_an_error_event(client, signed_in, monkeypatch):
    answer_with(monkeypatch, AgentRunError("The answer took longer than 240 seconds.", [], code="chat_deadline"))

    name, data = stream_ask(client, new_chat(client))[-1]

    assert name == "error" and data["status"] == 504 and data["chatDeadline"] is True


def test_the_streamed_run_is_cut_off_at_the_chat_budget(client, signed_in, monkeypatch):
    monkeypatch.setenv("CHAT_DEADLINE_SECONDS", "0.05")
    monkeypatch.setattr(main, "CHAT_BACKSTOP_GRACE_SECONDS", 0.05)

    async def stuck(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "run_agent", stuck)
    started = time.monotonic()

    name, data = stream_ask(client, new_chat(client))[-1]

    assert name == "error" and data["status"] == 504 and data["chatDeadline"] is True
    assert time.monotonic() - started < 2


def test_indmoney_auth_failure_retries_then_asks_to_reconnect(client, signed_in, monkeypatch):
    from tests.test_api import connect_user_to_indmoney

    connect_user_to_indmoney(signed_in["id"])
    chat_id = new_chat(client, "portfolio")
    tokens = []

    async def fake_user_access_token(user_id, *, force_refresh=False):
        return "fresh-token" if force_refresh else "stale-token"

    async def fake_run_agent(message, mcp_url, token, *args, **kwargs):
        tokens.append(token)
        kwargs["on_event"]("route", {"category": "account_data"})
        raise AgentRunError('MCP tools/call failed for "get_networth": 401 ', [], auth_failed=True)

    monkeypatch.setattr(users, "user_access_token", fake_user_access_token)
    monkeypatch.setattr(main, "run_agent", fake_run_agent)

    events = stream_ask(client, chat_id, message="What is my net worth?", agent_type="portfolio")

    assert tokens == ["stale-token", "fresh-token"]
    assert [name for name, _ in events] == ["progress", "progress", "progress", "error"]
    assert [data["seq"] for _, data in events[:3]] == [0, 1, 2]
    name, data = events[-1]
    assert data["status"] == 401 and data["loginUrl"] == "/auth/indmoney/connect"
    assert main._chats_in_flight == set()


def test_keepalive_when_nothing_happens(client, signed_in, monkeypatch):
    monkeypatch.setattr(chat_stream, "KEEPALIVE_SECONDS", 0.05)

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.2)
        return "Diversification spreads risk."

    monkeypatch.setattr(main, "run_agent", slow)

    response, raw = stream_raw(client, new_chat(client))

    assert b": keepalive\n\n" in raw
    assert parse_events(raw)[-1][0] == "answer"


def test_a_waiting_model_slot_is_a_progress_event(client, signed_in, monkeypatch):
    async def waits(*args, **kwargs):
        model_gate._wait_listener.get()()
        return "Diversification spreads risk."

    monkeypatch.setattr(main, "run_agent", waits)

    events = stream_ask(client, new_chat(client))

    assert events[1] == ("progress", {"seq": 1, "kind": "waiting"})


def test_progress_events_have_only_allowed_keys(client, signed_in, monkeypatch):
    from app import tasks

    contract = tasks.TaskContract("account_data", allowed_tools=frozenset({"get_portfolio_snapshot"}))
    answer_with(monkeypatch, events=(
        ("route", {"category": "general_finance"}),
        ("contract", {"contract": contract}),
        ("model_call", {"attempt": 1, "messages": ["secret"]}),
        ("tool_call", {"name": "get_portfolio_snapshot", "arguments": {"amount": 125000}, "source": "local"}),
        ("tool_result", {"name": "get_portfolio_snapshot", "result": "₹1,25,000"}),
        ("content", {"text": "worth ₹1,25,000"}),
        ("validation", {"check": "numbers", "detail": "125000"}),
    ))

    events = stream_ask(client, new_chat(client))

    progress = [data for name, data in events if name == "progress"]
    assert [item["kind"] for item in progress] == ["start", "route", "model_call", "tool_call", "tool_result", "validation"]
    for item in progress:
        assert set(item) <= {"seq", "kind", "name", "source", "status"}
    assert "125000" not in json.dumps(progress) and "1,25,000" not in json.dumps(progress)


def test_in_flight_is_released_after_a_streamed_failure(client, signed_in, monkeypatch):
    answer_with(monkeypatch, RuntimeError("model crashed"))

    name, data = stream_ask(client, new_chat(client))[-1]

    assert name == "error" and data["status"] == 502 and "model crashed" not in json.dumps(data)
    assert main._chats_in_flight == set()


def test_a_task_that_raises_ends_with_the_failure_event(client, signed_in, monkeypatch):
    async def broken(prepared, on_event=None):
        raise RuntimeError("could not save")

    monkeypatch.setattr(main, "_run_chat", broken)

    name, data = stream_ask(client, new_chat(client))[-1]

    assert (name, data) == ("error", {"status": 502, "trace": [], "error": main.chat_failure_message(None, "")})
    assert main._chats_in_flight == set()


def _wait_until(condition, seconds=2.0):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_a_disconnect_still_saves_the_answer(client, signed_in, monkeypatch):
    """The browser goes away after the first event: the answer is still saved and the slot confirmed."""
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "3")
    release = threading.Event()

    async def slow(*args, **kwargs):
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "Diversification spreads risk."

    monkeypatch.setattr(main, "run_agent", slow)
    chat_id = new_chat(client)
    body = json.dumps({"message": "What is diversification?", "chatId": chat_id, "agentType": "finance"}).encode()
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    sent: list[dict] = []

    async def disconnect_after_first_event():
        first_chunk = asyncio.Event()
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await first_chunk.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_chunk.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/chat", "raw_path": b"/api/chat", "root_path": "", "query_string": b"",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                        (b"accept", b"text/event-stream"), (b"cookie", cookie.encode()),
                        (b"content-length", str(len(body)).encode())],
            "client": ("testclient", 50000), "server": ("testserver", 80), "state": {},
        }
        await main.app(scope, receive, send)

    client.portal.call(disconnect_after_first_event)

    assert sent[0]["status"] == 200
    assert parse_events(b"".join(m.get("body", b"") for m in sent[1:]))[0][1] == {"seq": 0, "kind": "start"}
    assert len(main._stream_tasks) == 1 and main._chats_in_flight == {signed_in["id"]}  # still answering

    release.set()
    assert _wait_until(lambda: not main._stream_tasks)

    assert main._chats_in_flight == set()
    saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert saved[-1]["content"] == "Diversification spreads risk."
    assert limits.questions_used(signed_in["id"], now=limits.local_now()) == 1


@pytest.mark.asyncio
async def test_closing_the_event_stream_never_cancels_the_answer():
    release = asyncio.Event()
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def answer():
        queue.put_nowait({"seq": 1, "kind": "model_call"})
        await release.wait()
        return PlainTextResponse("Saved anyway.")

    task = asyncio.create_task(answer())
    stream = chat_stream.events(task, queue, {"seq": 0, "kind": "start"}, {"status": 502})

    assert await anext(stream) == sse("progress", {"seq": 0, "kind": "start"})
    assert await anext(stream) == sse("progress", {"seq": 1, "kind": "model_call"})
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.01)
    pending.cancel()  # the client went away while the stream was waiting
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()

    assert not task.done()
    release.set()
    assert bytes((await task).body) == b"Saved anyway."


@pytest.mark.asyncio
async def test_the_event_stream_drains_progress_before_the_final_event(monkeypatch):
    monkeypatch.setattr(chat_stream, "KEEPALIVE_SECONDS", 5)
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def answer():
        for seq in (1, 2, 3):
            queue.put_nowait({"seq": seq, "kind": "model_call"})
        return PlainTextResponse("Done.")

    task = asyncio.create_task(answer())
    await task
    chunks = [chunk async for chunk in chat_stream.events(task, queue, {"seq": 0, "kind": "start"}, {"status": 502})]

    events = parse_events(b"".join(chunks))
    assert [data.get("seq") for _, data in events[:-1]] == [0, 1, 2, 3]
    assert events[-1] == ("answer", {"text": "Done.", "trace": []})


@pytest.mark.asyncio
async def test_a_failed_task_ends_the_stream_with_the_failure_event():
    async def answer():
        raise RuntimeError("boom")

    task = asyncio.create_task(answer())
    chunks = [chunk async for chunk in chat_stream.events(task, asyncio.Queue(), {"seq": 0, "kind": "start"},
                                                          {"status": 502, "trace": [], "error": "Try again."})]

    assert parse_events(b"".join(chunks))[-1] == ("error", {"status": 502, "trace": [], "error": "Try again."})
