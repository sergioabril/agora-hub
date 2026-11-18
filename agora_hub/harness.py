import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from .tools import FILES_PATH, FileStore, ToolContext, ToolError, Tools

MAX_ROUNDS = 10  # runaway backstop only; a normal reply ends after 1–3 calls
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
DATA_URL = re.compile(r"data:image/[\w.+-]+;base64,(.*)", re.S)
DONE = "data: [DONE]\n\n"
SHOWN = "Images shown above, attached so you can see them:"


class BadRequest(Exception):
    pass


class UpstreamDown(Exception):
    pass


def error_text(error: Exception) -> str:
    return str(error) or type(error).__name__


def raw(response: httpx.Response) -> Response:
    return Response(response.content, response.status_code, media_type=response.headers.get("content-type"))


def chunk(last: dict, delta: dict, finish: str | None = None) -> str:
    head = {"id": last.get("id"), "object": "chat.completion.chunk", "created": last.get("created"),
            "model": last.get("model")}
    return f"data: {json.dumps({**head, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})}\n\n"


@dataclass
class Turn:
    """One client request: the rewritten conversation, the tools on offer and the round counter."""

    body: dict
    url: str
    context: ToolContext
    messages: list[dict]
    client_tools: list[dict]
    hub_tools: list[dict]
    note: str
    round: int = 1  # number of the upstream call being made or read
    attached: set[str] = field(default_factory=set)  # file names already sent to the model as image parts

    def payload(self) -> dict:
        final = self.round == MAX_ROUNDS
        hub = [] if final else self.hub_tools
        messages = self.messages
        if hub:
            first = messages[0] if messages else {}
            if first.get("role") == "system" and isinstance(first.get("content"), str):
                messages = [{**first, "content": first["content"] + "\n\n" + self.note}, *messages[1:]]
            else:
                messages = [{"role": "system", "content": self.note}, *messages]
        payload = {**self.body, "messages": messages}
        if tools := self.client_tools + hub:
            payload["tools"] = tools
        elif final:
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
        return payload

    def split(self, calls: list[dict]) -> tuple[list[dict], list[dict]]:
        names = set() if self.round == MAX_ROUNDS else {tool["function"]["name"] for tool in self.hub_tools}
        return ([call for call in calls if call["function"]["name"] in names],
                [call for call in calls if call["function"]["name"] not in names])


class Harness:
    def __init__(self, client: httpx.AsyncClient, tools: Tools, store: FileStore):
        self.client, self.tools, self.store = client, tools, store

    async def chat(self, body: dict, url: str, base_url: str) -> Response:
        if body.get("n") not in (None, 1):
            raise BadRequest("n must be 1")
        if not isinstance(body.get("messages"), list):
            raise BadRequest("messages must be a list")
        context = ToolContext([], base_url)
        client_tools = body.get("tools") or []
        taken = {tool.get("function", {}).get("name") for tool in client_tools}
        hub_tools = [tool for tool in self.tools.specs() if tool["function"]["name"] not in taken]
        turn = Turn(body, url, context, [], client_tools, hub_tools, self.tools.instructions())
        found = []  # names first seen as links, shown after the message and the tool messages that follow it
        for message in body["messages"]:
            if message.get("role") != "tool":
                turn.messages += self._shown(found, turn)
                found = []
            turn.messages.append(self._message(message, turn, found))
        turn.messages += self._shown(found, turn)
        stream = bool(body.get("stream"))
        try:
            response = await self._send(turn, stream)
        except httpx.RequestError as error:
            raise UpstreamDown(error_text(error))
        if not response.is_success:
            if stream:
                await response.aread()
            return raw(response)
        if not stream:
            return await self._complete(turn, response)
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(self._stream(turn, response), media_type="text/event-stream", headers=headers)

    # Request rewrite: each image reaches the model once, as its [image_N] label plus a data URL part.

    def _attach(self, name: str, turn: Turn) -> list[dict]:
        parts = [{"type": "text", "text": f"[{turn.context.label(name)}]"}]
        if name not in turn.attached:
            turn.attached.add(name)
            parts.append({"type": "image_url", "image_url": {"url": self.store.data_url(name)}})
        return parts

    def _shown(self, names: list[str], turn: Turn) -> list[dict]:
        parts = [part for name in names if name not in turn.attached for part in self._attach(name, turn)]
        return [{"role": "user", "content": [{"type": "text", "text": SHOWN}, *parts]}] if parts else []

    def _file(self, url: str) -> str | None:
        try:
            path = urlsplit(url).path
        except ValueError:
            return None
        name = path[len(FILES_PATH):] if path.startswith(FILES_PATH) else ""
        return name if self.store.path(name) else None

    def _text(self, text: str, turn: Turn, found: list[str]) -> str:
        def label(match):
            if not (name := self._file(match[1])):
                return match[0]
            found.append(name)
            return f"[{turn.context.label(name)}]"

        return MARKDOWN_IMAGE.sub(label, text)

    def _image(self, part: dict, turn: Turn) -> list[dict]:
        url = (part.get("image_url") or {}).get("url") or ""
        if match := DATA_URL.match(url):
            try:
                name = self.store.save(base64.b64decode(match[1], validate=True))
            except ValueError:
                name = None
        else:
            name = self._file(url)
        return self._attach(name, turn) if name else [{"type": "text", "text": "[image unavailable]"}]

    def _message(self, message: dict, turn: Turn, found: list[str]) -> dict:
        content = message.get("content")
        if isinstance(content, str):
            return {**message, "content": self._text(content, turn, found)}
        if not isinstance(content, list):
            return message
        parts = []
        for part in content:
            if part.get("type") == "image_url":
                parts += self._image(part, turn)
            elif part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append({**part, "text": self._text(part["text"], turn, found)})
            else:
                parts.append(part)
        return {**message, "content": parts}

    # Rounds

    async def _send(self, turn: Turn, stream: bool) -> httpx.Response:
        return await self.client.send(self.client.build_request("POST", turn.url, json=turn.payload()), stream=stream)

    async def _run(self, calls: list[dict], turn: Turn):
        """Runs hub calls in order, yielding the text to emit, or None every 10 s while a tool runs."""
        images = []
        for call in calls:
            name, arguments = call["function"]["name"], call["function"]["arguments"]
            yield self.tools.progress(name, arguments)
            task = asyncio.ensure_future(self.tools.run(name, arguments, turn.context))
            try:
                while not (await asyncio.wait({task}, timeout=10))[0]:
                    yield None
                output = task.result()
                result, text = output.content, output.display
                images += output.images
            except ToolError as error:
                result, text = f"error: {error}", f"_{name} failed: {error}_\n\n"
            finally:
                task.cancel()
            yield text
            turn.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        turn.messages += self._shown(images, turn)  # after the round's tool messages, so the next round sees them

    async def _complete(self, turn: Turn, response: httpx.Response) -> Response:
        text, previous = "", None
        while True:
            try:
                data = response.json()
                message = data["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError):
                data = message = None
            hub_calls, client_calls = turn.split((message or {}).get("tool_calls") or [])
            if previous is None and not hub_calls:
                return raw(response)
            if data is None:
                return self._ended(previous, text, "upstream returned invalid JSON")
            text += message.get("content") or ""
            if hub_calls and not client_calls:
                turn.messages.append({"role": "assistant", "content": message.get("content") or None,
                                      "tool_calls": hub_calls})
            async for line in self._run(hub_calls, turn):
                text += line or ""
            if not hub_calls or client_calls:
                message["content"] = text
                if client_calls:
                    message["tool_calls"] = client_calls
                return JSONResponse(data)
            previous = data
            turn.round += 1
            try:
                response = await self._send(turn, False)
            except httpx.RequestError as error:
                return self._ended(previous, text, error_text(error))
            if not response.is_success:
                return self._ended(previous, text, f"upstream returned HTTP {response.status_code}")

    @staticmethod
    def _ended(data: dict, text: str, error: str) -> Response:
        choice = data["choices"][0]
        choice["message"]["content"] = f"{text}\n\n_error: {error}_"
        choice["message"].pop("tool_calls", None)
        choice["finish_reason"] = "stop"
        return JSONResponse(data)

    async def _stream(self, turn: Turn, upstream: httpx.Response):
        last = {}
        try:
            while True:
                calls, held, content = {}, [], ""
                async for line in upstream.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if (data := line[5:].strip()) == "[DONE]":
                        break
                    try:
                        last = json.loads(data)
                    except ValueError:
                        yield line + "\n\n"
                        continue
                    choice = (last.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    for piece in delta.get("tool_calls") or []:
                        call = calls.setdefault(piece.get("index", 0), {"id": None, "type": "function",
                                                                        "function": {"name": "", "arguments": ""}})
                        function = piece.get("function") or {}
                        call["id"] = call["id"] or piece.get("id")
                        call["function"]["name"] = call["function"]["name"] or function.get("name") or ""
                        call["function"]["arguments"] += function.get("arguments") or ""
                    if delta.get("tool_calls"):
                        if choice.get("finish_reason"):  # vLLM: last call delta and finish in one chunk
                            held.append(chunk(last, {}, choice["finish_reason"]))
                        continue
                    if not last.get("choices") or choice.get("finish_reason"):
                        held.append(line + "\n\n")
                    else:
                        content += delta.get("content") or ""
                        yield line + "\n\n"
                await upstream.aclose()
                hub_calls, client_calls = turn.split([calls[index] for index in sorted(calls)])
                if hub_calls and not client_calls:
                    turn.messages.append({"role": "assistant", "content": content or None, "tool_calls": hub_calls})
                async for text in self._run(hub_calls, turn):
                    yield ": keep-alive\n\n" if text is None else chunk(last, {"content": text})
                if not hub_calls or client_calls:
                    for index, call in enumerate(client_calls):
                        yield chunk(last, {"tool_calls": [{"index": index, **call}]})
                    yield "".join(held) + DONE
                    return
                turn.round += 1
                try:
                    upstream = await self._send(turn, True)
                    error = None if upstream.is_success else f"upstream returned HTTP {upstream.status_code}"
                except httpx.RequestError as exception:
                    error = error_text(exception)
                if error:
                    yield chunk(last, {"content": f"\n\n_error: {error}_"}) + chunk(last, {}, "stop") + DONE
                    return
        finally:
            await upstream.aclose()
