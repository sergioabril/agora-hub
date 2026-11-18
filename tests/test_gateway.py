import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from agora_hub.config import DeploymentConfig, load_config
from agora_hub.controller import Controller
from agora_hub.gateway import create_gateway
from agora_hub.tools import FileStore
from tests.fakes import FakeAdapters

KEY = {"Authorization": "Bearer " + "b" * 64}
SSE = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16
NAME = hashlib.sha256(PNG).hexdigest()[:32] + ".png"
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
SHOWN = {"type": "text", "text": "Images shown above, attached so you can see them:"}
GENERATING = "\n\n_generating image…_\n\n"
LOOKUP = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}


@pytest.fixture
def gateway(config_dir):
    config = load_config(config_dir)
    adapters = FakeAdapters()
    controller = Controller(config, adapters)
    requests, replies = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        if replies:
            return replies.pop(0)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"ok": True})

    app = create_gateway(config, controller, httpx.MockTransport(handler))
    return config, adapters, controller, app, requests, replies


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def serve(adapters, controller, image=True):
    adapters.readiness.update(pro=True, images=image)
    adapters.models.update(pro=["m1"], images=["img"])
    await controller.deployments_status()


def call(name, arguments, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


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


def deltas(text):
    return [json.loads(line[6:])["choices"][0]["delta"] for line in text.split("\n\n") if line.startswith("data: {")]


def chats(requests):
    return [json.loads(r.content) for r in requests if r.url.path.endswith("/chat/completions")]


def ask(request, messages, **body):
    return request.post("/openai/v1/chat/completions", json={"model": "agora", "messages": messages, **body}, headers=KEY)


def stream_calls(*calls):
    return [{"tool_calls": [{"index": index, **call}]} for index, call in enumerate(calls)]


async def test_auth_and_models(gateway):
    config, adapters, controller, app, _, _ = gateway
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
        config.hub.gateway.api_keys = []
        open_ = await request.get("/openai/v1/models")
    assert empty.json() == {"object": "list", "data": []}
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == {"error": {"message": "invalid gateway key", "type": "invalid_request_error", "code": "invalid_api_key"}}
    assert [(m["id"], m["owned_by"]) for m in listed.json()["data"]] == [("agora", "agora"), ("m1", "pro"), ("m2", "pro")]
    assert [r.status_code for r in (embeddings, get_chat, *old)] == [404] * 4
    assert embeddings.json()["error"]["code"] == get_chat.json()["error"]["code"] == "not_found"
    assert embeddings.json()["error"]["message"].startswith("POST /openai/v1/embeddings is not served; the gateway serves GET")
    assert open_.status_code == 200 and open_.json() == listed.json()


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


async def test_disconnect(gateway):
    _, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)

    async def slow():  # MockTransport awaits a coroutine reply
        await asyncio.sleep(0.5)
        return completion(calls=[call("generate_image", {"prompt": "fox"})])
    replies.append(slow())
    body = json.dumps({"model": "agora", "messages": [{"role": "user", "content": "fox"}]}).encode()
    received, sent = asyncio.Queue(), asyncio.Queue()
    for message in ({"type": "http.request", "body": body}, {"type": "http.disconnect"}):
        received.put_nowait(message)
    scope = {"type": "http", "method": "POST", "path": "/openai/v1/chat/completions", "query_string": b"",
             "headers": [(b"authorization", KEY["Authorization"].encode())]}
    await app(scope, received.get, sent.put)
    assert sent.get_nowait()["status"] == 499 and len(chats(requests)) == 1 and not any("/images/" in r.url.path for r in requests)


async def test_chat_generate(gateway):
    config, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    replies += [completion(calls=[call("generate_image", {"prompt": "a red fox"})]), completion("A fox.")]
    async with client(app) as request:
        reply = await ask(request, [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "fox"}])
        image = await request.get(f"/files/{NAME}")
        missing = await request.get("/files/0000.png")
    url = f"http://test/files/{NAME}"
    assert reply.json()["choices"][0]["message"]["content"] == f"{GENERATING}![image_1]({url})\n\nA fox."
    assert image.content == PNG and image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "file_not_found"
    assert str(requests[1].url) == "http://spark-4.local:8001/v1/images/generations"
    assert json.loads(requests[1].content) == {"model": "img", "prompt": "a red fox", "n": 1, "response_format": "b64_json"}
    first, second = chats(requests)
    assert first["model"] == "m1" and [t["function"]["name"] for t in first["tools"]] == ["generate_image", "edit_image"]
    assert first["messages"][0]["content"].startswith("Be brief.\n\nImages in this conversation are labelled")
    assert second["messages"][-3]["tool_calls"][0]["id"] == "call_1"
    assert second["messages"][-2] == {"role": "tool", "tool_call_id": "call_1",
                                      "content": "image_1 is shown to the user and attached below"}
    assert second["messages"][-1] == {"role": "user", "content": [
        SHOWN, {"type": "text", "text": "[image_1]"}, {"type": "image_url", "image_url": {"url": PNG_URL}}]}
    assert (config.config_dir / "files" / NAME).stat().st_mode & 0o777 == 0o600


async def test_chat_generate_streaming(gateway):
    _, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    replies += [
        sse({"role": "assistant", "content": "Sure."}, *stream_calls(call("generate_image", {"prompt": "fox"})),
            finish="tool_calls"),
        sse({"content": "Done."}),
    ]
    async with client(app) as request:
        reply = await ask(request, [{"role": "user", "content": "fox"}], stream=True)
    assert "tool_calls" not in reply.text and reply.text.endswith("data: [DONE]\n\n")
    image = f"![image_1](http://test/files/{NAME})\n\n"
    assert [d.get("content") for d in deltas(reply.text)] == ["Sure.", GENERATING, image, "Done.", None]
    assert chats(requests)[1]["messages"][-3]["content"] == "Sure."
    assert chats(requests)[1]["messages"][-1]["content"][2] == {"type": "image_url", "image_url": {"url": PNG_URL}}


async def test_chat_history_and_edit(gateway):
    config, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    other = FileStore(config.config_dir / "files").save(PNG[:8] + b"y" * 16)
    other_url = "data:image/png;base64," + base64.b64encode(PNG[:8] + b"y" * 16).decode()
    data_url = {"type": "image_url", "image_url": {"url": PNG_URL}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "make it blue"}, data_url]},
        {"role": "assistant", "content": f"Here ![image_1](http://elsewhere:8888/files/{other})",
         "tool_calls": [call("lookup", {}, "call_9")]},
        {"role": "tool", "tool_call_id": "call_9", "content": "found"},
        {"role": "user", "content": [data_url, {"type": "image_url", "image_url": {"url": "https://x.org/a.png"}}]},
    ]
    replies += [completion(calls=[call("edit_image", {"image": "image_1", "prompt": "blue"})]), completion("Done.")]
    async with client(app) as request:
        reply = await ask(request, messages)
    first = chats(requests)[0]
    assert first["messages"][1]["content"] == [{"type": "text", "text": "make it blue"}, {"type": "text", "text": "[image_1]"}, data_url]
    assert first["messages"][2]["content"] == "Here [image_2]" and first["messages"][3]["role"] == "tool"
    assert first["messages"][4] == {"role": "user", "content": [
        SHOWN, {"type": "text", "text": "[image_2]"}, {"type": "image_url", "image_url": {"url": other_url}}]}
    assert first["messages"][5]["content"] == [{"type": "text", "text": "[image_1]"}, {"type": "text", "text": "[image unavailable]"}]
    edit = requests[1]
    assert str(edit.url) == "http://spark-4.local:8001/v1/images/edits"
    assert edit.headers["content-type"].startswith("multipart/form-data")
    assert PNG in edit.content and f'filename="{NAME}"'.encode() in edit.content and b"image/png" in edit.content
    assert "_editing image_1…_" in reply.json()["choices"][0]["message"]["content"]


async def test_chat_client_tools(gateway):
    _, adapters, controller, app, requests, replies = gateway
    await serve(adapters, controller)
    messages = [{"role": "user", "content": "go"}]
    lookup = call("lookup", {}, "call_2")
    replies += [
        completion(calls=[lookup]),
        sse(*stream_calls(call("generate_image", {"prompt": "x"}), lookup), finish="tool_calls", joined=True),
    ]
    async with client(app) as request:
        only = await ask(request, messages, tools=[LOOKUP])
        mixed = await ask(request, messages, tools=[LOOKUP], stream=True)
        await serve(adapters, controller, image=False)
        await ask(request, messages, tools=[LOOKUP])
    assert only.json()["choices"][0]["message"]["tool_calls"] == [lookup]
    assert [r.url.path for r in requests].count("/v1/images/generations") == 1
    sent = deltas(mixed.text)
    assert [d["tool_calls"] for d in sent if "tool_calls" in d] == [[{"index": 0, **lookup}]]
    assert f"![image_1](http://test/files/{NAME})\n\n" in [d.get("content") for d in sent]
    assert mixed.text.count('"finish_reason": "tool_calls"') == 1 and len(chats(requests)) == 3
    last = chats(requests)[-1]
    assert last["tools"] == [LOOKUP] and last["messages"] == messages


async def test_chat_public_host(gateway):
    config, adapters, controller, app, _, replies = gateway
    config.hub.gateway.public_host = "100.64.1.2"
    await serve(adapters, controller)
    replies += [completion(calls=[call("generate_image", {"prompt": "x"})]), completion("ok")]
    async with client(app) as request:
        reply = await ask(request, [{"role": "user", "content": "fox"}])
    content = reply.json()["choices"][0]["message"]["content"]
    assert f"![image_1](http://100.64.1.2:8888/files/{NAME})" in content
    assert (await controller.deployments_status())["gateway"]["host"] == "100.64.1.2"
