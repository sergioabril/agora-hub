import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .tools import FILES_PATH, Call, FileStore, ToolContext, ToolError, ToolOutput, Tools
from .trace import trace

MAX_ROUNDS = 10  # runaway backstop only; a normal reply ends after 1–3 calls
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
DATA_URL = re.compile(r"data:image/[\w.+-]+;base64,(.*)", re.S)
SHOWN = "Images shown above, attached so you can see them:"


class UpstreamDown(Exception):
    """The first LLM call couldn't connect."""


class UpstreamError(Exception):
    """The first LLM call answered non-2xx."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def error_text(error: Exception) -> str:
    return str(error) or type(error).__name__


@dataclass
class Delta:
    text: str
    reasoning: str


@dataclass
class ToolStarted:
    call: Call


@dataclass
class KeepAlive:
    pass


@dataclass
class ToolFinished:
    call: Call
    output: ToolOutput | None
    error: str | None


@dataclass
class Finished:
    calls: list[dict]  # the client tool calls the turn ends with, in chat format
    finish: str | None  # the last round's finish_reason


@dataclass
class Failed:
    error: str


class Message:
    """Collects a streamed chat completion into one assistant message."""

    def __init__(self):
        self.content, self.reasoning, self.finish, self.usage, self.calls = "", "", None, None, {}

    def add(self, chunk: dict) -> tuple[str, str]:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choice = (chunk.get("choices") or [{}])[0]
        self.finish = choice.get("finish_reason") or self.finish
        delta = choice.get("delta") or {}
        for piece in delta.get("tool_calls") or []:
            call = self.calls.setdefault(piece.get("index", 0), {"id": None, "type": "function",
                                                                 "function": {"name": "", "arguments": ""}})
            function = piece.get("function") or {}
            call["id"] = call["id"] or piece.get("id")
            call["function"]["name"] = call["function"]["name"] or function.get("name") or ""
            call["function"]["arguments"] += function.get("arguments") or ""
        content, reasoning = delta.get("content") or "", delta.get("reasoning") or delta.get("reasoning_content") or ""
        self.content += content
        self.reasoning += reasoning
        return content, reasoning

    def tool_calls(self) -> list[dict]:
        return [self.calls[index] for index in sorted(self.calls)]

    def summary(self) -> dict:
        return {"content": self.content, "reasoning": self.reasoning, "tool_calls": self.tool_calls(),
                "finish_reason": self.finish, "usage": self.usage}


@dataclass
class Turn:
    """One client request: the rewritten conversation, the tools on offer and the round counter."""

    body: dict
    url: str
    request_id: str
    context: ToolContext
    messages: list[dict]
    client_tools: list[dict]
    hub_tools: list[dict]
    note: str
    round: int = 1  # number of the upstream call being made or read
    attached: set[str] = field(default_factory=set)  # file names already sent to the model as image parts
    response: httpx.Response | None = None  # the round being read

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
        payload = {**self.body, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
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

    async def start(self, body: dict, url: str, request_id: str) -> Turn:
        client_tools = body.get("tools") or []
        taken = {tool.get("function", {}).get("name") for tool in client_tools}
        specs, note = self.tools.offer()
        hub_tools = [tool for tool in specs if tool["function"]["name"] not in taken]
        turn = Turn(body, url, request_id, ToolContext([]), [], client_tools, hub_tools, note)
        found = []  # names first seen as links, shown after the message and the tool messages that follow it
        for message in body["messages"]:
            if message.get("role") != "tool":
                turn.messages += self._shown(found, turn)
                found = []
            turn.messages.append(self._message(message, turn, found))
        turn.messages += self._shown(found, turn)
        if error := await self._send(turn):
            raise error
        return turn

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

    async def _send(self, turn: Turn) -> Exception | None:
        """Sends a round, leaving its streamed response in turn.response; returns the error instead when it fails."""
        payload = turn.payload()
        trace(turn.request_id, "upstream.request", round=turn.round, url=turn.url, body=payload)
        try:
            response = await self.client.send(self.client.build_request("POST", turn.url, json=payload), stream=True)
        except httpx.RequestError as error:
            trace(turn.request_id, "upstream.error", round=turn.round, error=error_text(error))
            return UpstreamDown(error_text(error))
        if response.is_success:
            turn.response = response
            return None
        try:
            await response.aread()
        finally:
            await response.aclose()
        try:
            data = response.json()
        except ValueError:
            data = response.text
        trace(turn.request_id, "upstream.response", round=turn.round, status=response.status_code, body=data)
        error = data.get("error") if isinstance(data, dict) else None
        message = ((error.get("message") if isinstance(error, dict) else None)
                   or (data.get("message") if isinstance(data, dict) else None))
        return UpstreamError(response.status_code, message or f"upstream returned HTTP {response.status_code}")

    async def _tool(self, tool_call: dict, turn: Turn, images: list[str]):
        """Runs one hub call, yielding its events, then appends its tool message."""
        call = self.tools.call(tool_call)
        trace(turn.request_id, "tool.call", call=call.id, name=tool_call["function"]["name"],
              arguments=tool_call["function"]["arguments"])
        yield ToolStarted(call)
        started = time.monotonic()
        task = asyncio.ensure_future(self.tools.run(call, turn.context))
        try:
            while not (await asyncio.wait({task}, timeout=10))[0]:
                yield KeepAlive()
            output, error = task.result(), None
        except ToolError as exception:
            output, error = None, str(exception)
        finally:
            task.cancel()
        seconds = round(time.monotonic() - started, 3)
        if output:
            trace(turn.request_id, "tool.result", call=call.id, seconds=seconds, content=output.content,
                  images=output.images)
            images += output.images
        else:
            trace(turn.request_id, "tool.result", call=call.id, seconds=seconds, error=error)
        yield ToolFinished(call, output, error)
        turn.messages.append({"role": "tool", "tool_call_id": tool_call["id"],
                              "content": output.content if output else f"error: {error}"})

    async def events(self, turn: Turn):
        """Yields the turn's events, round after round, until Finished or Failed."""
        try:
            while True:
                message = Message()
                try:
                    async for line in turn.response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        if (data := line[5:].strip()) == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        text, reasoning = message.add(chunk)
                        if text or reasoning:
                            yield Delta(text, reasoning)
                except httpx.HTTPError as error:
                    trace(turn.request_id, "upstream.error", round=turn.round, error=error_text(error))
                    yield Failed(error_text(error))
                    return
                await turn.response.aclose()
                trace(turn.request_id, "upstream.response", round=turn.round, status=turn.response.status_code,
                      message=message.summary())
                hub_calls, client_calls = turn.split(message.tool_calls())
                if hub_calls and not client_calls:
                    turn.messages.append({"role": "assistant", "content": message.content or None,
                                          "tool_calls": hub_calls})
                images = []
                for tool_call in hub_calls:
                    tool = self._tool(tool_call, turn, images)
                    try:
                        async for event in tool:
                            yield event
                    finally:
                        await tool.aclose()
                turn.messages += self._shown(images, turn)  # after the round's tool messages, so the next round sees them
                if not hub_calls or client_calls:
                    yield Finished(client_calls, message.finish)
                    return
                turn.round += 1
                if error := await self._send(turn):
                    yield Failed(str(error))
                    return
        finally:
            await turn.response.aclose()
