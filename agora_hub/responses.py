import asyncio
import base64
import json
import secrets
import time

from starlette.responses import JSONResponse, Response, StreamingResponse

from .harness import Delta, Failed, Finished, Harness, KeepAlive, ToolFinished, ToolStarted
from .tools import FILES_PATH, FileStore
from .trace import trace

UNSUPPORTED = ("previous_response_id", "conversation", "background")
HUB_TOOLS = {"web_search", "web_search_preview", "image_generation"}  # the hub's own tools are on anyway
FORMATS = {"png": "png", "jpg": "jpeg", "webp": "webp"}
ECHOED = {"instructions": None, "tools": [], "tool_choice": "auto", "parallel_tool_calls": True, "temperature": None,
          "top_p": None, "max_output_tokens": None, "metadata": {}}


class BadRequest(Exception):
    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.message, self.code = message, code


def _assistant(messages: list[dict]) -> dict:
    """The open assistant message: the assistant side of a turn becomes one chat message."""
    if not messages or messages[-1]["role"] != "assistant":
        messages.append({"role": "assistant", "content": None})
    return messages[-1]


def _say(messages: list[dict], text: str):
    if text:
        message = _assistant(messages)
        message["content"] = f"{message['content']}\n\n{text}" if message["content"] else text


def _part(part: dict) -> dict:
    kind = part.get("type")
    if kind == "input_text":
        return {"type": "text", "text": part.get("text", "")}
    if kind == "input_image" and (url := part.get("image_url")):
        return {"type": "image_url", "image_url": {"url": url}}
    if kind == "input_image":
        return {"type": "text", "text": "[image unavailable]"}
    raise BadRequest(f"content type {kind} is not supported")


def _texts(content, kind: str, separator: str) -> str:
    if isinstance(content, str):
        return content
    return separator.join(part.get("text", "") for part in content or [] if part.get("type") == kind)


def _item(item: dict, messages: list[dict], store: FileStore):
    kind = item.get("type") or ("message" if "role" in item else None)
    if kind == "message" and item.get("role") == "assistant":
        _say(messages, _texts(item.get("content"), "output_text", "\n\n"))
    elif kind == "message" and item.get("role") in ("user", "system", "developer"):
        content = item.get("content")
        if not isinstance(content, (str, list)):
            raise BadRequest("message content must be a string or a list of parts")
        messages.append({"role": "system" if item["role"] == "developer" else item["role"],
                         "content": content if isinstance(content, str) else [_part(part) for part in content]})
    elif kind == "function_call":
        _assistant(messages).setdefault("tool_calls", []).append(
            {"id": item.get("call_id"), "type": "function",
             "function": {"name": item.get("name"), "arguments": item.get("arguments")}})
    elif kind == "function_call_output":
        messages.append({"role": "tool", "tool_call_id": item.get("call_id"),
                         "content": _texts(item.get("output"), "input_text", "")})
    elif kind == "image_generation_call" and item.get("status") == "completed":
        item_id = str(item.get("id") or "")
        name = store.find(item_id[3:]) if item_id.startswith("ig_") else None
        if not name and isinstance(item.get("result"), str):
            try:
                name = store.save(base64.b64decode(item["result"], validate=True))
            except ValueError:
                name = None
        _say(messages, f"![image]({FILES_PATH}{name})" if name else "[image unavailable]")
    elif kind not in ("image_generation_call", "reasoning", "web_search_call"):  # past reasoning and searches are skipped
        raise BadRequest(f"input item type {kind} is not supported")


def chat_request(body: dict, store: FileStore) -> dict:
    for field in UNSUPPORTED:
        if body.get(field):
            raise BadRequest(f"{field} is not supported; send the whole conversation in input", "unsupported_parameter")
    items = body.get("input")
    items = [{"role": "user", "content": items}] if isinstance(items, str) else items
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise BadRequest("input must be a string or a list of items")
    messages = [{"role": "system", "content": body["instructions"]}] if isinstance(body.get("instructions"), str) else []
    for item in items:
        _item(item, messages, store)
    chat = {"model": body["model"], "messages": messages}
    tools = []  # the client's function tools; the harness adds its own
    for tool in body.get("tools") or []:
        if tool.get("type") == "function":
            function = {key: tool[key] for key in ("name", "description", "parameters") if key in tool}
            tools.append({"type": "function", "function": function})
        elif tool.get("type") not in HUB_TOOLS:
            raise BadRequest(f"tool type {tool.get('type')} is not supported", "unsupported_tool")
    chat.update({"tools": tools} if tools else {})
    choice = body.get("tool_choice")
    if choice in ("auto", "none", "required"):
        chat["tool_choice"] = choice
    elif isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
        chat["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    for key, chat_key in (("temperature", "temperature"), ("top_p", "top_p"),
                          ("parallel_tool_calls", "parallel_tool_calls"), ("max_output_tokens", "max_tokens")):
        if body.get(key) is not None:
            chat[chat_key] = body[key]
    return chat


class Render:
    """Turns harness events into Responses events, building the response object as they go."""

    def __init__(self, response: dict, store: FileStore):
        self.response, self.store, self.sequence = response, store, 0
        self.open = None  # (item, part, output_index): the reasoning or message item receiving deltas
        self.tool = None  # (item, output_index, event prefix): the running hub call

    def send(self, kind: str, **fields) -> str:
        data = json.dumps({"type": kind, "sequence_number": self.sequence, **fields}, ensure_ascii=False)
        self.sequence += 1
        return f"event: {kind}\ndata: {data}\n\n"

    def add(self, item: dict) -> tuple[str, int]:
        self.response["output"].append(item)
        index = len(self.response["output"]) - 1
        return self.send("response.output_item.added", output_index=index, item=item), index

    def start(self) -> list[str]:
        return [self.send(kind, response=self.response) for kind in ("response.created", "response.in_progress")]

    def close(self) -> list[str]:
        if not self.open:
            return []
        (item, part, index), self.open = self.open, None
        ids = {"item_id": item["id"], "output_index": index, "content_index": 0}
        item["status"] = "completed"
        done = (self.send("response.reasoning_text.done", **ids, text=part["text"]) if item["type"] == "reasoning"
                else self.send("response.output_text.done", **ids, text=part["text"], logprobs=[]))
        return [done, self.send("response.content_part.done", **ids, part=part),
                self.send("response.output_item.done", output_index=index, item=item)]

    def delta(self, kind: str, text: str) -> list[str]:
        out = []
        if not self.open or self.open[0]["type"] != kind:
            out += self.close()
            if kind == "reasoning":
                item = {"type": "reasoning", "id": f"rs_{secrets.token_hex(16)}", "summary": [], "content": [],
                        "status": "in_progress"}
                part = {"type": "reasoning_text", "text": ""}
            else:
                item = {"type": "message", "id": f"msg_{secrets.token_hex(16)}", "status": "in_progress",
                        "role": "assistant", "content": []}
                part = {"type": "output_text", "text": "", "annotations": [], "logprobs": []}
            added, index = self.add(item)
            out += [added, self.send("response.content_part.added", item_id=item["id"], output_index=index,
                                     content_index=0, part=part)]
            item["content"].append(part)
            self.open = (item, part, index)
        item, part, index = self.open
        part["text"] += text
        ids = {"item_id": item["id"], "output_index": index, "content_index": 0}
        if kind == "reasoning":
            return [*out, self.send("response.reasoning_text.delta", **ids, delta=text)]
        return [*out, self.send("response.output_text.delta", **ids, delta=text, logprobs=[])]

    def started(self, event: ToolStarted) -> list[str]:
        call, out = event.call, self.close()
        if call.kind == "image":
            item = {"type": "image_generation_call", "id": f"ig_{call.id}", "status": "in_progress",
                    "action": call.action, "revised_prompt": call.text, "result": None}
            prefix, stages = "response.image_generation_call", ("in_progress", "generating")
        else:
            item = {"type": "web_search_call", "id": f"ws_{call.id}", "status": "in_progress",
                    "action": {"type": "search", "query": call.text}}
            prefix, stages = "response.web_search_call", ("in_progress", "searching")
        added, index = self.add(item)
        self.tool = (item, index, prefix)
        return [*out, added, *(self.send(f"{prefix}.{stage}", item_id=item["id"], output_index=index) for stage in stages)]

    def finished_tool(self, event: ToolFinished) -> list[str]:
        (item, index, prefix), self.tool, out = self.tool, None, []
        if event.output:
            out.append(self.send(f"{prefix}.completed", item_id=item["id"], output_index=index))
            item["status"] = "completed"
            if item["type"] == "image_generation_call" and event.output.images:
                name = event.output.images[0]
                item["result"] = base64.b64encode(self.store.path(name).read_bytes()).decode()
                item["output_format"] = FORMATS[name.split(".")[1]]
        else:
            item["status"] = "failed"
        return [*out, self.send("response.output_item.done", output_index=index, item=item)]

    def finished(self, event: Finished) -> list[str]:
        out = self.close()
        for call in event.calls:
            arguments = call["function"]["arguments"]
            item = {"type": "function_call", "id": f"fc_{secrets.token_hex(16)}", "call_id": call["id"],
                    "name": call["function"]["name"], "arguments": "", "status": "in_progress"}
            added, index = self.add(item)
            ids = {"item_id": item["id"], "output_index": index}
            out += [added, self.send("response.function_call_arguments.delta", **ids, delta=arguments),
                    self.send("response.function_call_arguments.done", **ids, arguments=arguments)]
            item.update(arguments=arguments, status="completed")
            out.append(self.send("response.output_item.done", output_index=index, item=item))
        if event.finish == "length":
            self.response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            return [*out, self.send("response.incomplete", response=self.response)]
        self.response["status"] = "completed"
        return [*out, self.send("response.completed", response=self.response)]

    def handle(self, event) -> list[str]:
        if isinstance(event, Delta):
            return [*(self.delta("reasoning", event.reasoning) if event.reasoning else []),
                    *(self.delta("message", event.text) if event.text else [])]
        if not isinstance(event, Failed):
            return {ToolStarted: self.started, KeepAlive: lambda _: [": keep-alive\n\n"],
                    ToolFinished: self.finished_tool, Finished: self.finished}[type(event)](event)
        out = self.close()
        self.response.update(status="failed", error={"code": "server_error", "message": event.error})
        return [*out, self.send("response.failed", response=self.response)]


async def _rendered(harness: Harness, turn, render: Render, request_id: str, stream: bool):
    events = harness.events(turn)
    try:
        for text in render.start():
            yield text
        async for event in events:
            for text in render.handle(event):
                yield text
        trace(request_id, "response", response=render.response)
    except (asyncio.CancelledError, GeneratorExit):
        if stream:  # the gateway's race helper logs a non-streaming disconnect
            trace(request_id, "disconnect", response=render.response)
        raise
    finally:
        await events.aclose()  # cancels a running tool and closes the LLM response
        await turn.response.aclose()  # in case the client left before the events started


async def respond(harness: Harness, store: FileStore, body: dict, url: str, request_id: str) -> Response:
    chat = chat_request(body, store)
    response = {"id": request_id, "object": "response", "created_at": int(time.time()), "status": "in_progress",
                "model": body["model"], "output": [], "error": None, "incomplete_details": None,
                **{key: body.get(key, default) for key, default in ECHOED.items()},
                "previous_response_id": None, "store": False, "usage": None}
    turn = await harness.start(chat, url, request_id)
    chunks = _rendered(harness, turn, Render(response, store), request_id, body.get("stream") is True)
    if body.get("stream") is True:
        return StreamingResponse(chunks, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                                 media_type="text/event-stream")
    async for _ in chunks:
        pass
    return JSONResponse(response)
