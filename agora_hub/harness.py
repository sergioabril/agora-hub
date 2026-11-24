import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import anyio
import httpx

from .tools import FILES_PATH, Call, FileStore, ToolContext, ToolOutput, Tools
from .trace import trace

MAX_TOOL_ITERATIONS = 40  # plus one final answer iteration
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
    registry: Tools
    round: int = 1  # number of the upstream call being made or read
    visible: set[str] = field(default_factory=set)  # file names the model may get as image parts in this turn
    attached: set[str] = field(default_factory=set)  # file names already sent to the model as image parts
    response: httpx.Response | None = None  # the round being read

    final: bool = False
    web_attached: list[str] = field(default_factory=list)
    cleaned: bool = False
    web_names: set[str] = field(default_factory=set)
    web_pixels: dict[str, list[dict]] = field(default_factory=dict)

    def payload(self) -> dict:
        specs, note = self.registry.offer(self.context)
        taken = {t["function"]["name"] for t in self.client_tools}
        hub = [t for t in specs if t["function"]["name"] not in taken]
        self.final = (self.final or not hub or self.round > MAX_TOOL_ITERATIONS
                      or self.context.total_actions >= self.registry.config.total_round_limit)
        if self.final or self.body.get("tool_choice") == "none":
            hub = []
        if self.final:
            note += "\n\nTool research has ended. Give the final answer from collected evidence, include sources actually read, and state limitations. Do not call more hub tools."
        messages = self.messages
        if note:
            first = messages[0] if messages else {}
            if first.get("role") == "system" and isinstance(first.get("content"), str):
                messages = [{**first, "content": first["content"] + "\n\n" + note}, *messages[1:]]
            else:
                messages = [{"role": "system", "content": note}, *messages]
        payload = {**self.body, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools := self.client_tools + hub:
            payload["tools"] = tools
        else:
            payload.pop("tools", None)
        choice = payload.get("tool_choice")
        forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
        offered = {t["function"]["name"] for t in self.client_tools + hub}
        if (forced and forced not in offered) or (choice == "required" and not offered):
            payload.pop("tool_choice", None)
        return payload

    def split(self, calls: list[dict]) -> tuple[list[dict], list[dict]]:
        # A client declaration explicitly owns a collision, even when the hub family is disabled.
        names = self.registry.owned_names - {t["function"]["name"] for t in self.client_tools}
        return ([call for call in calls if call["function"]["name"] in names],
                [call for call in calls if call["function"]["name"] not in names])


class Harness:
    def __init__(self, client: httpx.AsyncClient, tools: Tools, store: FileStore):
        self.client, self.tools, self.store = client, tools, store

    async def start(self, body: dict, url: str, request_id: str) -> Turn:
        client_tools = body.get("tools") or []
        turn = Turn(body, url, request_id, ToolContext([]), [], client_tools, self.tools)
        number, seen = 0, {}  # turn number -> the images that turn refers to; a user message starts a turn
        for message in body["messages"]:
            number += message.get("role") == "user"
            seen.setdefault(number, []).extend(self._names(message))
        earlier = [n for n, names in seen.items() if names and n < number]
        turn.visible = {name for n in (*earlier[-1:], number) for name in seen.get(n, [])}
        found = []  # names first seen as links, shown after the message and the tool messages that follow it
        for message in body["messages"]:
            if message.get("role") != "tool":
                turn.messages += self._shown(found, turn)
                found = []
            turn.messages.append(self._message(message, turn, found))
        turn.messages += self._shown(found, turn)
        self._cap_web_images(turn)
        try:
            if error := await self._send(turn):
                raise error
        except BaseException:
            await self.cleanup(turn)
            raise
        return turn

    # Request rewrite: every image gets its [image_N] label. The model gets the pixels, once each, of the images in
    # view: those of the current turn and of the latest earlier turn that has any. The rest are marked not attached.

    def _attach(self, name: str, turn: Turn) -> list[dict]:
        label = turn.context.label(name)
        if name not in turn.visible:
            return [{"type": "text", "text": f"[{label}, not attached]"}]
        parts = [{"type": "text", "text": f"[{label}]"}]
        if name not in turn.attached:
            turn.attached.add(name)
            pixel = {"type": "image_url", "image_url": {"url": self.store.data_url(name)}}
            metadata = self.store.metadata(name.rsplit(".", 1)[0])
            if name in turn.web_names or metadata and metadata.get("type") == "web_image_call":
                turn.web_names.add(name)
                if name in turn.web_attached:
                    turn.web_attached.remove(name)
                turn.web_attached.append(name)
                turn.web_pixels.setdefault(name, []).append(pixel)
            parts.append(pixel)
        return parts

    def _shown(self, names: list[str], turn: Turn) -> list[dict]:
        parts = [part for name in names if name not in turn.attached for part in self._attach(name, turn)]
        header = [{"type": "text", "text": SHOWN}] if any(part["type"] == "image_url" for part in parts) else []
        return [{"role": "user", "content": [*header, *parts]}] if parts else []

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

    def _name(self, part: dict) -> str | None:
        """The stored file an image part refers to; a data URL is saved on the way."""
        url = (part.get("image_url") or {}).get("url") or ""
        if not (match := DATA_URL.match(url)):
            return self._file(url)
        try:
            return self.store.save(base64.b64decode(match[1], validate=True))
        except ValueError:
            return None

    def _names(self, message: dict) -> list[str]:
        """The stored images a message refers to, as image parts or as links in its text."""
        content, names = message.get("content"), []
        for part in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            if part.get("type") == "image_url":
                names.append(self._name(part))
            elif part.get("type") == "text" and isinstance(part.get("text"), str):
                names += [self._file(match[1]) for match in MARKDOWN_IMAGE.finditer(part["text"])]
        return [name for name in names if name]

    def _message(self, message: dict, turn: Turn, found: list[str]) -> dict:
        content = message.get("content")
        if isinstance(content, str):
            return {**message, "content": self._text(content, turn, found)}
        if not isinstance(content, list):
            return message
        parts = []
        for part in content:
            if part.get("type") == "image_url":
                name = self._name(part)
                parts += self._attach(name, turn) if name else [{"type": "text", "text": "[image unavailable]"}]
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
        await self.tools.prepare(call, turn.context)
        yield ToolStarted(call)
        started = time.monotonic()
        task = asyncio.ensure_future(self.tools.run(call, turn.context))
        try:
            while not (await asyncio.wait({task}, timeout=10))[0]:
                yield KeepAlive()
            output, error = task.result(), None
        except Exception as exception:
            output, error = None, error_text(exception)
        finally:
            task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(task, return_exceptions=True)
        seconds = round(time.monotonic() - started, 3)
        if output:
            trace(turn.request_id, "tool.result", call=call.id, seconds=seconds, content=output.content,
                  images=output.images)
            images += output.images
            turn.web_names.update(output.web_images)
            for name in output.images:
                if name not in turn.web_names:
                    continue
                if name in turn.web_attached:
                    turn.web_attached.remove(name)
                turn.web_attached.append(name)
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
                if hub_calls and (turn.final or turn.body.get("tool_choice") == "none"):
                    yield Failed("model requested hub tools when a tool-free answer was required")
                    return
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
                turn.visible.update(images)  # what a turn makes or asks to see stays in view while it runs
                turn.messages += self._shown(images, turn)
                self._cap_web_images(turn)
                if not hub_calls or client_calls:
                    yield Finished(client_calls, message.finish)
                    return
                turn.round += 1
                if error := await self._send(turn):
                    yield Failed(str(error))
                    return
        finally:
            await self.cleanup(turn)

    def _cap_web_images(self, turn):
        evicted = turn.web_attached[:-8]
        turn.web_attached = turn.web_attached[-8:]
        if not evicted:
            return
        pixels = {id(part) for name in evicted for part in turn.web_pixels.pop(name, [])}
        labels = {f"[{turn.context.label(name)}]": f"[{turn.context.label(name)}, not attached]" for name in evicted}
        for message in turn.messages:
            if isinstance(message.get("content"), list):
                message["content"] = [part for part in message["content"] if id(part) not in pixels]
                for part in message["content"]:
                    if part.get("type") == "text" and part.get("text") in labels:
                        part["text"] = labels[part["text"]]
        turn.attached.difference_update(evicted)
        turn.visible.difference_update(evicted)

    async def cleanup(self, turn):
        if not turn.cleaned:
            turn.cleaned = True
            with anyio.CancelScope(shield=True):
                try:
                    if turn.response:
                        await turn.response.aclose()
                finally:
                    await self.tools.cleanup(turn.context)
