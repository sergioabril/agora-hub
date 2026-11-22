import asyncio
import base64
import json
import re

import httpx
import pytest

from agora_hub.accounts import Accounts
from agora_hub.api import create_app
from agora_hub.config import DeploymentConfig, load_config
from agora_hub.controller import Controller
from agora_hub.home_assistant import HomeAssistant
from agora_hub.gateway import create_gateway
from agora_hub.harness import SHOWN, Delta, Failed, Finished, Harness, ToolFinished, ToolStarted, UpstreamError
from agora_hub.tools import IMAGE_NOTE, SEARCH_NOTE, VISION_NOTE, FileStore, Tools
from tests.fakes import FakeAdapters

KEY = {}  # the gateway fixture puts a linked token here
SSE = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
JPG = b"\xff\xd8\xff" + b"j" * 16
JPG_URL = "data:image/jpeg;base64," + base64.b64encode(JPG).decode()
NOTES = "\n\n".join((VISION_NOTE, IMAGE_NOTE, SEARCH_NOTE))


def upstream(requests, replies):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        if replies:
            return replies.pop(0)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


@pytest.fixture
def gateway(config_dir, monkeypatch):
    config = load_config(config_dir)
    adapters = FakeAdapters()
    controller = Controller(config, adapters)
    requests, replies = [], []
    accounts = Accounts(config_dir / "agora.db")
    token = accounts.link(accounts.add("Test")["invite_code"])["token"]
    monkeypatch.setitem(KEY, "Authorization", "Bearer " + token)
    app = create_gateway(config, controller, accounts, upstream(requests, replies))
    return config, adapters, controller, app, requests, replies


@pytest.fixture
def harness(tmp_path):
    requests, replies, image = [], [], [("http://img.local/v1", "img")]  # clear image to stop the image server
    http = httpx.AsyncClient(transport=upstream(requests, replies))
    store = FileStore(tmp_path / "files")
    return Harness(http, Tools(http, store, lambda: image[0] if image else None, 60), store), store, requests, replies, image


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def serve(adapters, controller, image=True):
    adapters.readiness.update(pro=True, images=image)
    adapters.models.update(pro=["m1"], images=["img"])
    await controller.deployments_status()


def completion(content=None, calls=None):
    message = {"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}
    choice = {"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}
    return httpx.Response(200, json={"id": "c1", "object": "chat.completion", "created": 1, "model": "m1",
                                     "choices": [choice]})


def sse(*deltas, finish="stop", joined=False):  # joined: finish_reason on the last delta, as vLLM sends it
    choices = [{"index": 0, "delta": delta, "finish_reason": None} for delta in deltas]
    choices[-1:] = [{**choices[-1], "finish_reason": finish}] if joined else [choices[-1], {"index": 0, "delta": {}, "finish_reason": finish}]
    head = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1"}
    body = "".join(f"data: {json.dumps({**head, 'choices': [choice]})}\n\n" for choice in choices)
    return httpx.Response(200, content=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})


def chats(requests):
    return [json.loads(r.content) for r in requests if r.url.path.endswith("/chat/completions")]


def ask(request, messages, **body):
    return request.post("/openai/v1/chat/completions", json={"model": "agora", "messages": messages, **body}, headers=KEY)


async def test_auth_and_models(gateway):
    _, adapters, controller, app, _, _ = gateway
    async with client(app) as request:
        empty = await request.get("/openai/v1/models", headers=KEY)
        adapters.readiness.update(pro=True, images=True)
        adapters.models.update(pro=["m1", "m2"], images=["img"])
        await controller.deployments_status()
        wrong = await request.get("/openai/v1/models", headers={"Authorization": "Bearer " + "a" * 64})
        missing = await request.post("/openai/v1/chat/completions", json={"model": "agora"})
        listed = await request.get("/openai/v1/models", headers=KEY)
        embeddings = await request.post("/openai/v1/embeddings", json={"model": "agora"}, headers=KEY)
        get_chat = await request.get("/openai/v1/chat/completions", headers=KEY)
        old = [await request.get(f"/{prefix}/v1/models", headers=KEY) for prefix in ("proxy", "agent")]  # removed
    assert empty.json() == {"object": "list", "data": []}
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == {"error": {"message": "invalid token", "type": "invalid_request_error", "code": "invalid_api_key"}}
    assert [(m["id"], m["owned_by"]) for m in listed.json()["data"]] == [("agora", "agora"), ("m1", "pro"), ("m2", "pro")]
    assert [r.status_code for r in (embeddings, get_chat, *old)] == [404] * 4
    assert embeddings.json()["error"]["code"] == get_chat.json()["error"]["code"] == "not_found"
    assert embeddings.json()["error"]["message"].startswith("POST /openai/v1/embeddings is not served; the gateway serves GET")


async def test_accounts_link(gateway):
    config, _, _, app, _, _ = gateway
    accounts = Accounts(config.config_dir / "agora.db")
    admin = create_app(config, Controller(config, FakeAdapters()), HomeAssistant(config), accounts)
    admin_headers = {"Authorization": "Bearer " + "a" * 64}
    seen = []

    def bearer(token):
        return {"Authorization": "Bearer " + token}

    async with client(admin) as admin_request, client(app) as request:
        async def new(name):
            reply = (await admin_request.post("/admin/accounts/new", json={"name": name}, headers=admin_headers)).json()
            seen.append(reply["invite_code"])
            return reply["id"], reply["invite_code"]

        async def invite(user_id):
            code = (await admin_request.post(f"/admin/accounts/{user_id}/invite", headers=admin_headers)).json()["invite_code"]
            seen.append(code)
            return code

        async def link(code):
            reply = await request.post("/agora/v1/link", json={"code": code})
            if reply.status_code == 200:
                seen.append(reply.json()["token"])
            return reply

        async def status(token, path="/agora/v1/me"):
            return (await request.get(path, headers=bearer(token))).status_code

        ana, code = await new("Ana")
        linked = await link(code)
        token = linked.json()["token"]
        me = await request.get("/agora/v1/me", headers=bearer(token))
        models = await status(token, "/openai/v1/models")
        reused = await link(code)
        expired_code = await invite(ana)
        with accounts.db:
            accounts.db.execute("UPDATE user_invites SET expires_at = '2000-01-01T00:00:00+00:00' WHERE consumed = 0")
        expired = await link(expired_code)
        no_code = await request.post("/agora/v1/link", json={"name": "Ana"})
        second = (await link(await invite(ana))).json()["token"]
        both = [await status(token), await status(second)]
        pending = await invite(ana)
        revoked = await admin_request.post(f"/admin/accounts/{ana}/revoke", headers=admin_headers)
        after_revoke = [await status(token), await status(second), (await link(pending)).status_code]

        bob, bob_code = await new("Bob")
        bob_token = (await link(bob_code)).json()["token"]
        bob_pending = await invite(bob)
        await admin_request.post(f"/admin/accounts/{bob}/disable", json={"disabled": True}, headers=admin_headers)
        while_disabled = [await status(bob_token), (await link(bob_pending)).status_code]
        await admin_request.post(f"/admin/accounts/{bob}/disable", json={"disabled": False}, headers=admin_headers)
        enabled = await status(bob_token)

        missing = await request.get("/agora/v1/me")
        unknown = await status("c" * 64)
        non_ascii = await request.get("/agora/v1/me", headers={"Authorization": "Bearer caf\u00e9".encode("latin-1")})
    assert linked.status_code == 200 and re.fullmatch(r"[0-9a-f]{64}", token)
    assert linked.json()["user"] == me.json() == {"id": ana, "name": "Ana"}
    assert models == 200
    assert [(r.status_code, r.json()["error"]["code"]) for r in (reused, expired, no_code)] == [
        (400, "invalid_invite"), (400, "invalid_invite"), (400, "invalid_request")]
    assert both == [200, 200]
    assert revoked.json() == {"revoked": 2}
    assert after_revoke == [401, 401, 400]
    assert while_disabled == [401, 400] and enabled == 200
    assert missing.status_code == unknown == non_ascii.status_code == 401
    db = (config.config_dir / "agora.db").read_bytes()
    assert not [value for value in seen if value.encode() in db]


async def test_routing(gateway):
    config, adapters, controller, app, requests, _ = gateway
    config.deployments["fast"] = DeploymentConfig(name="Fast", kind="llm", machines=["spark-1"], head="spark-1",
                                                  start_command="/s", stop_command="/s", endpoint="http://fast.local:9000/v1/")
    messages = [{"role": "user", "content": "hi"}]
    async with client(app) as request:
        down = await ask(request, messages)
        adapters.readiness.update(pro=True, fast=True, images=True)
        adapters.models.update(pro=["m1", "m2"], fast=["f1"], images=["img"])
        await controller.deployments_status()
        agora = await ask(request, messages)
        exact, image, unknown = [await ask(request, messages, model=model) for model in ("f1", "img", "nope")]
        bad = await request.post("/openai/v1/chat/completions", content=b"[]", headers=KEY)
    assert agora.json() == exact.json() == {"ok": True}
    assert [(str(r.url), json.loads(r.content)["model"]) for r in requests] == [
        ("http://spark-1.local:8000/v1/chat/completions", "m1"), ("http://fast.local:9000/v1/chat/completions", "f1")]
    assert [(r.status_code, r.json()["error"]["code"]) for r in (down, image, unknown, bad)] == [
        (503, "no_llm_running"), (404, "model_not_found"), (404, "model_not_found"), (400, "invalid_request")]
    assert unknown.json()["error"]["message"] == "model nope is not served; use agora or one of: m1, m2, f1"
    assert all("authorization" not in r.headers for r in requests)


@pytest.mark.parametrize("path, body", [("/openai/v1/chat/completions", {"messages": [{"role": "user", "content": "fox"}]}),
                                        ("/openai/v1/responses", {"input": "fox", "stream": True})])
async def test_disconnect(gateway, path, body):
    _, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)

    finished = []

    async def slow():  # MockTransport awaits a coroutine reply
        await asyncio.sleep(0.5)
        finished.append(True)
        return completion("late")
    replies.append(slow())
    body = json.dumps({"model": "agora", **body}).encode()
    received, sent = asyncio.Queue(), asyncio.Queue()
    for message in ({"type": "http.request", "body": body}, {"type": "http.disconnect"}):
        received.put_nowait(message)
    scope = {"type": "http", "method": "POST", "path": path, "query_string": b"",
             "headers": [(b"authorization", KEY["Authorization"].encode())]}
    await app(scope, received.get, sent.put)
    assert sent.get_nowait()["status"] == 499 and len(chats(requests)) == 1
    assert not any("/images/" in r.url.path for r in requests)
    await asyncio.sleep(0.6)
    assert not finished


async def test_chat_passthrough(gateway):
    _, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    content = [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": PNG_URL}},
               {"type": "image_url", "image_url": {"url": "https://x.org/a.png"}}]
    messages = [{"role": "user", "content": content}]
    streamed = sse({"role": "assistant", "content": "A"}, {"content": " fox."})
    replies += [completion("A fox."), streamed, httpx.Response(400, json={"error": {"message": "too long"}})]
    async with client(app) as request:
        plain = await ask(request, messages, temperature=0.2)
        stream = await ask(request, messages, stream=True)
        error = await ask(request, messages)
    assert chats(requests)[0] == {"model": "m1", "messages": messages, "temperature": 0.2}
    assert plain.json() == completion("A fox.").json()
    assert stream.content == streamed.content and stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-cache" and stream.headers["x-accel-buffering"] == "no"
    assert error.status_code == 400 and error.json() == {"error": {"message": "too long"}}


async def test_trace(gateway, capsys):
    _, adapters, controller, app, _, replies = gateway
    await serve(adapters, controller)
    replies.append(sse({"role": "assistant", "content": "A"}, {"content": " fox."}))
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG_URL}}]}]
    async with client(app) as request:
        await ask(request, messages, stream=True)
        await ask(request, messages, model="nope")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    first = lines[0]["request"]
    assert re.fullmatch(r"chat_[0-9a-f]{16}", first)
    assert [(line["event"], line["request"] == first) for line in lines[:3]] == [
        ("request", True), ("upstream.request", True), ("upstream.response", True)]
    url = lines[0]["body"]["messages"][0]["content"][0]["image_url"]["url"]
    assert url == f"data:image/png;base64,<{len(PNG_URL)} chars>"
    assert lines[2]["message"]["content"] == "A fox." and lines[2]["message"]["finish_reason"] == "stop"
    assert lines[-1]["event"] == "error" and lines[-1]["code"] == "model_not_found" and lines[-1]["request"] != first


def tool_call(name, **arguments):
    return {"tool_calls": [{"index": 0, "id": f"call_{name}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


async def turn(harness, messages, **body):
    started = await harness.start({"model": "m1", "messages": messages, **body}, "http://llm.local/v1/chat/completions",
                                  "resp_test")
    return [event async for event in harness.events(started)]


async def test_harness_generate(harness):
    harness, store, requests, replies, _ = harness
    replies += [sse({"reasoning_content": "fox?"}, {"content": "Sure."}, tool_call("generate_image", prompt="a red fox"),
                    finish="tool_calls", joined=True), sse({"content": "Done."})]
    events = await turn(harness, [{"role": "user", "content": "draw a red fox"}])
    call = events[2].call
    assert (call.kind, call.action, call.text, call.labels) == ("image", "generate", "a red fox", [])
    assert events == [Delta("", "fox?"), Delta("Sure.", ""), ToolStarted(call),
                      ToolFinished(call, events[3].output, None), Delta("Done.", ""), Finished([], "stop")]
    assert events[3].output.images == [f"{call.id}.png"]
    first, second = chats(requests)
    assert all(body["stream"] and body["stream_options"] == {"include_usage": True} for body in (first, second))
    assert [tool["function"]["name"] for tool in first["tools"]] == ["generate_image", "edit_image", "view_image", "web_search"]
    assert first["messages"][0] == {"role": "system", "content": NOTES}
    assistant, tool, shown = second["messages"][-3:]
    assert assistant["tool_calls"][0]["function"]["name"] == "generate_image" and assistant["content"] == "Sure."
    assert tool == {"role": "tool", "tool_call_id": "call_generate_image",
                    "content": "[image_1] is shown to the user"}
    assert shown["content"] == [{"type": "text", "text": SHOWN}, {"type": "text", "text": "[image_1]"},
                                {"type": "image_url", "image_url": {"url": PNG_URL}}]
    assert store.path(f"{call.id}.png").stat().st_mode & 0o777 == 0o600


async def test_harness_history_and_edit(harness):
    harness, store, requests, replies, _ = harness
    linked = store.save(PNG)
    lookup = {"id": "c9", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": JPG_URL}}]},
        {"role": "assistant", "content": f"Here ![x](/files/{linked})", "tool_calls": [lookup]},
        {"role": "tool", "tool_call_id": "c9", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": "combine"}, {"type": "image_url", "image_url": {"url": JPG_URL}},
                                     {"type": "image_url", "image_url": {"url": "https://x.org/a.png"}}]}]
    replies.append(sse(tool_call("edit_image", images=["image_1", "image_2"], prompt="combine them"), finish="tool_calls"))
    await turn(harness, messages)
    sent = chats(requests)[0]["messages"]
    assert sent[1]["content"] == [{"type": "text", "text": "look"}, {"type": "text", "text": "[image_1]"},
                                  {"type": "image_url", "image_url": {"url": JPG_URL}}]
    assert sent[2]["content"] == "Here [image_2]" and sent[3]["role"] == "tool"
    assert sent[4]["content"] == [{"type": "text", "text": SHOWN}, {"type": "text", "text": "[image_2]"},
                                  {"type": "image_url", "image_url": {"url": PNG_URL}}]
    assert sent[5]["content"] == [{"type": "text", "text": "combine"}, {"type": "text", "text": "[image_1]"},
                                  {"type": "text", "text": "[image unavailable]"}]
    edit = next(r for r in requests if r.url.path.endswith("/images/edits")).content
    assert edit.count(b'name="image"; filename=') == 2 and edit.index(JPG) < edit.index(PNG)


async def test_harness_tools_and_errors(harness, capsys):
    harness, _, requests, replies, image = harness
    user = [{"role": "user", "content": "hi"}]
    replies += [sse(tool_call("web_search", query="news"), finish="tool_calls"), sse({"content": "No search."})]
    searched = await turn(harness, user)
    assert searched[1].call.text == "news" and searched[1].error.startswith("TO BE IMPLEMENTED: web search")
    assert chats(requests)[-1]["messages"][-1]["content"] == "error: " + searched[1].error
    lookup = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    replies.append(sse(tool_call("lookup", q="x"), finish="tool_calls"))
    assert (await turn(harness, user, tools=[lookup]))[-1] == Finished([
        {"id": "call_lookup", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "x"}'}}], "tool_calls")
    replies.append(httpx.Response(400, json={"error": {"message": "too long"}}))
    with pytest.raises(UpstreamError) as error:
        await turn(harness, user)
    assert (error.value.status, error.value.message) == (400, "too long")
    replies += [sse(tool_call("generate_image", prompt="fox"), finish="tool_calls"), httpx.Response(500, text="boom")]
    assert (await turn(harness, user))[-1] == Failed("upstream returned HTTP 500")
    image.clear()
    replies.append(sse({"content": "ok"}))
    await turn(harness, user)
    assert [tool["function"]["name"] for tool in chats(requests)[-1]["tools"]] == ["view_image", "web_search"]
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events.count("tool.call") == events.count("tool.result") == 2


def respond(request, **body):
    return request.post("/openai/v1/responses", json={"model": "agora", **body}, headers=KEY)


def sse_events(text):
    return [json.loads(block.split("\ndata: ", 1)[1]) for block in text.split("\n\n") if block.startswith("event:")]


async def test_responses_stream(gateway):
    _, adapters, controller, app, _, replies = gateway
    await serve(adapters, controller)
    replies += [sse({"reasoning_content": "fox?"}, {"content": "Sure."}, tool_call("generate_image", prompt="a red fox"),
                    finish="tool_calls", joined=True), sse({"content": "Done."})]
    async with client(app) as request:
        reply = await respond(request, input="draw a red fox", stream=True)
    events = sse_events(reply.text)
    text = ["output_item.added", "content_part.added", "output_text.delta", "output_text.done", "content_part.done",
            "output_item.done"]
    assert [e["type"] for e in events] == ["response." + kind for kind in [
        "created", "in_progress", *[kind.replace("output_text", "reasoning_text") for kind in text], *text,
        "output_item.added", "image_generation_call.in_progress", "image_generation_call.generating",
        "image_generation_call.completed", "output_item.done", *text, "completed"]]
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    added, done = events[14]["item"], events[18]["item"]
    assert re.fullmatch(r"ig_[0-9a-f]{32}", added["id"]) and (added["action"], added["revised_prompt"]) == ("generate", "a red fox")
    assert (done["status"], done["result"], done["output_format"]) == ("completed", base64.b64encode(PNG).decode(), "png")
    final = events[-1]["response"]
    assert [item["type"] for item in final["output"]] == ["reasoning", "message", "image_generation_call", "message"]
    assert final["model"] == "m1" and final["status"] == "completed"


async def test_responses_input(gateway):
    config, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    stem = "a" * 32
    FileStore(config.config_dir / "files").save(PNG, stem)
    lookup = {"type": "function", "name": "lookup", "parameters": {"type": "object"}}
    items = [{"role": "developer", "content": "Be kind."},
             {"role": "user", "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": JPG_URL}]},
             {"type": "reasoning", "id": "rs_1", "summary": []},
             {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Drawing."}]},
             {"type": "image_generation_call", "id": f"ig_{stem}", "status": "completed", "result": None},
             {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
             {"type": "function_call_output", "call_id": "c1", "output": "ok"}]
    async with client(app) as request:
        reply = await respond(request, instructions="Be brief.", input=items, max_output_tokens=50,
                              tools=[lookup, {"type": "image_generation"}, {"type": "web_search"}],
                              tool_choice={"type": "function", "name": "lookup"})
        errors = [await respond(request, **{"input": "hi", **extra}) for extra in (
            {"previous_response_id": "resp_1"}, {"tools": [{"type": "file_search"}]},
            {"input": [{"type": "item_reference", "id": "x"}]})]
    sent = chats(requests)[0]
    assert reply.status_code == 200 and len(chats(requests)) == 1
    assert sent["messages"] == [
        {"role": "system", "content": "Be brief.\n\n" + NOTES},
        {"role": "system", "content": "Be kind."},
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "text", "text": "[image_1]"},
                                     {"type": "image_url", "image_url": {"url": JPG_URL}}]},
        {"role": "assistant", "content": "Drawing.\n\n[image_2]",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": SHOWN}, {"type": "text", "text": "[image_2]"},
                                     {"type": "image_url", "image_url": {"url": PNG_URL}}]}]
    assert sent["tools"][0] == {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    assert [tool["function"]["name"] for tool in sent["tools"]] == ["lookup", "generate_image", "edit_image", "view_image", "web_search"]
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "lookup"}} and sent["max_tokens"] == 50
    assert [(r.status_code, r.json()["error"]["code"]) for r in errors] == [
        (400, "unsupported_parameter"), (400, "unsupported_tool"), (400, "invalid_request")]


async def test_responses_replay(gateway):
    """An image the hub made goes back to the model as its tool call and result, exactly as the model saw them live.
    Shown as a bare [image_N] in the assistant's own text, it taught the model to write a label instead of calling."""
    _, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    replies += [sse({"content": "Sure."}, tool_call("generate_image", prompt="a red fox"), finish="tool_calls", joined=True),
                sse({"content": "Done."})]
    asked = {"role": "user", "content": "draw a red fox"}
    async with client(app) as request:
        output = (await respond(request, input=[asked])).json()["output"]
        items = [{**item, "result": None} if item["type"] == "image_generation_call" else item for item in output]
        await respond(request, input=[asked, *items, {"role": "user", "content": "again"}])
    live, replay = chats(requests)[1]["messages"], chats(requests)[2]["messages"]
    assert [message["role"] for message in live] == ["system", "user", "assistant", "tool", "user"]
    assert replay == [*live, {"role": "assistant", "content": "Done."}, {"role": "user", "content": "again"}]


async def test_responses_view(gateway):
    """The model gets the pixels of the latest turn that has images. Older images are labels: it can still edit them,
    and view_image attaches them again. That call is not an output item."""
    _, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    replies += [sse(tool_call("edit_image", images=["image_1"], prompt="redder"), finish="tool_calls"),
                sse(tool_call("view_image", images=["[image_1, not attached]"]), finish="tool_calls"), sse({"content": "Seen."})]
    photos = [{"role": "user", "content": [{"type": "input_text", "text": text}, {"type": "input_image", "image_url": url}]}
              for text, url in (("one", JPG_URL), ("two", PNG_URL))]
    async with client(app) as request:
        output = (await respond(request, input=[*photos, {"role": "user", "content": "compare"}])).json()["output"]
    first, _, third = [body["messages"] for body in chats(requests)]
    assert first[1]["content"] == [{"type": "text", "text": "one"}, {"type": "text", "text": "[image_1, not attached]"}]
    assert first[2]["content"][1:] == [{"type": "text", "text": "[image_2]"}, {"type": "image_url", "image_url": {"url": PNG_URL}}]
    edit = next(r for r in requests if r.url.path.endswith("/images/edits")).content
    assert JPG in edit and b'name="output_format"\r\n\r\njpeg' in edit
    assert third[-2] == {"role": "tool", "tool_call_id": "call_view_image", "content": "You can see [image_1] now."}
    assert third[-1]["content"] == [{"type": "text", "text": SHOWN}, {"type": "text", "text": "[image_1]"},
                                    {"type": "image_url", "image_url": {"url": JPG_URL}}]
    assert [item["type"] for item in output] == ["image_generation_call", "message"]


async def test_responses_items(gateway, capsys):
    _, adapters, controller, app, _, replies = gateway
    await serve(adapters, controller)
    lookup = {"type": "function", "name": "lookup", "parameters": {"type": "object"}}
    replies += [sse(tool_call("web_search", query="news"), finish="tool_calls"), sse({"content": "No search."}),
                sse(tool_call("lookup", q="x"), finish="tool_calls"), sse({"content": "Cut"}, finish="length"),
                sse(tool_call("generate_image", prompt="fox"), finish="tool_calls"), httpx.Response(500, text="boom")]
    async with client(app) as request:
        searched, called, cut, failed = [(await respond(request, input="hi", **extra)).json()
                                         for extra in ({}, {"tools": [lookup]}, {}, {})]
    assert [item["type"] for item in searched["output"]] == ["web_search_call", "message"]
    assert searched["output"][0]["status"] == "failed" and searched["output"][0]["action"]["query"] == "news"
    call = called["output"][-1]
    assert (call["type"], call["call_id"], call["name"], call["arguments"]) == ("function_call", "call_lookup", "lookup",
                                                                                '{"q": "x"}')
    assert cut["status"] == "incomplete" and cut["incomplete_details"] == {"reason": "max_output_tokens"}
    assert failed["status"] == "failed" and failed["error"] == {"code": "server_error", "message": "upstream returned HTTP 500"}
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["request"] for line in lines if line["event"] == "response"] == [searched["id"], called["id"], cut["id"], failed["id"]]
    assert re.fullmatch(r"resp_[0-9a-f]{32}", searched["id"])
