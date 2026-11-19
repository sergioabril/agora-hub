import base64
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

FILES_PATH = "/files/"  # how the harness refers to a stored file in text; no route serves it
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
FILE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp)$")
IMAGE_NOTE = (
    "Images in this conversation are labelled [image_1], [image_2], … in order, and you can see each one. To create "
    "an image, call generate_image. To change an image, or to combine up to four, call edit_image with their labels. "
    "You see each result after the call: if it clearly misses the request, fix it once with edit_image; otherwise "
    "don't retry. The user sees every result automatically, so never write image links or markdown for it; describe "
    "what you did in one short sentence."
)
SEARCH_NOTE = "To look something up on the web, call web_search."
SEARCH_TODO = "TO BE IMPLEMENTED: web search isn't available yet. Answer without it and don't call it again."


def spec(name: str, description: str, **properties) -> dict:
    parameters = {"type": "object", "properties": properties, "required": list(properties)}
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


IMAGE_SPECS = [
    spec("generate_image", "Create a new image from a text description.",
         prompt={"type": "string", "description": "What the image shows, in detail."}),
    spec("edit_image", "Change an image from this conversation, or combine up to four into a new one.",
         images={"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4,
                 "description": 'Labels of the images, in order, e.g. ["image_2"].'},
         prompt={"type": "string", "description": "What to change. Refer to the images by their order in images: "
                                                  "the first image, the second image."}),
]
SEARCH_SPEC = spec("web_search", "Search the web for current information.",
                   query={"type": "string", "description": "What to search for."})
KINDS = {"generate_image": ("image", "generate"), "edit_image": ("image", "edit"), "web_search": ("search", "search")}


@dataclass
class Call:
    """One hub tool call, parsed once."""

    id: str  # 32 random hex digits; Responses item ids and the generated image's file name use it
    tool_call: dict  # the model's call: {"id", "type": "function", "function": {"name", "arguments"}}
    kind: str  # "image" or "search"
    action: str  # "generate", "edit" or "search"
    text: str  # the prompt or the query; "" when the arguments don't give one
    labels: list[str]  # edit: the image labels, in order; [] otherwise


@dataclass
class ToolContext:
    images: list[str]  # file names by label: image_1 is images[0]; each name appears once

    def label(self, name: str) -> str:
        if name not in self.images:
            self.images.append(name)
        return f"image_{self.images.index(name) + 1}"


@dataclass
class ToolOutput:
    content: str  # the tool message the model reads
    images: list[str]  # FileStore names this call produced, shown to the model after the round


class ToolError(Exception):
    pass


class FileStore:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, data: bytes, stem: str | None = None) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        else:
            raise ValueError("unsupported image type")
        name = f"{stem or hashlib.sha256(data).hexdigest()[:32]}.{ext}"
        path = self.folder / name
        if not path.exists():
            tmp = self.folder / f"{name}.tmp"
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as file:
                file.write(data)
            os.replace(tmp, path)
        return name

    def path(self, name: str) -> Path | None:
        path = self.folder / name
        return path if FILE_NAME.match(name) and path.is_file() else None

    def find(self, stem: str) -> str | None:
        return next((f"{stem}.{ext}" for ext in MEDIA_TYPES if self.path(f"{stem}.{ext}")), None)

    def data_url(self, name: str) -> str:
        data = base64.b64encode(self.path(name).read_bytes()).decode()
        return f"data:{MEDIA_TYPES[name.split('.')[1]]};base64,{data}"


class Tools:
    def __init__(
        self,
        client: httpx.AsyncClient,
        store: FileStore,
        image_target: Callable[[], tuple[str, str] | None],
        timeout: float,
    ):
        self.client, self.store, self.image_target, self.timeout = client, store, image_target, timeout

    def offer(self) -> tuple[list[dict], str]:
        if self.image_target():
            return [*IMAGE_SPECS, SEARCH_SPEC], IMAGE_NOTE + "\n\n" + SEARCH_NOTE
        return [SEARCH_SPEC], SEARCH_NOTE

    def call(self, tool_call: dict) -> Call:
        function = tool_call["function"]
        try:
            args = json.loads(function["arguments"])
        except ValueError:
            args = {}
        args = args if isinstance(args, dict) else {}
        text = args.get("prompt", args.get("query"))
        labels = args.get("images") if function["name"] == "edit_image" else None
        return Call(secrets.token_hex(16), tool_call, *KINDS[function["name"]], text if isinstance(text, str) else "",
                    [str(label) for label in labels] if isinstance(labels, list) else [])

    async def run(self, call: Call, context: ToolContext) -> ToolOutput:
        if call.kind == "search":
            raise ToolError(SEARCH_TODO)
        if not (target := self.image_target()):
            raise ToolError("no image deployment is running")
        if not call.text:
            raise ToolError("invalid arguments")
        base, model = target
        fields = {"model": model, "prompt": call.text, "n": 1, "response_format": "b64_json"}
        if call.action == "edit":
            if not 1 <= len(call.labels) <= 4:
                raise ToolError("edit_image takes one to four images")
            images = []
            for label in call.labels:
                match = re.fullmatch(r"image_([1-9][0-9]*)", label)
                if not match or int(match[1]) > len(context.images):
                    raise ToolError(f"unknown image {label}")
                file = context.images[int(match[1]) - 1]
                images.append(("image", (file, self.store.path(file).read_bytes(), MEDIA_TYPES[file.split(".")[1]])))
            request = {"url": f"{base}/images/edits", "data": {k: str(v) for k, v in fields.items()}, "files": images}
        else:
            request = {"url": f"{base}/images/generations", "json": fields}
        try:
            response = await self.client.post(**request, timeout=self.timeout)
        except httpx.RequestError as error:
            raise ToolError(f"image server: {str(error) or type(error).__name__}")
        if not response.is_success:
            raise ToolError(f"image server returned HTTP {response.status_code}: {response.text[:200]}")
        try:
            name = self.store.save(base64.b64decode(response.json()["data"][0]["b64_json"], validate=True), call.id)
        except (ValueError, KeyError, IndexError, TypeError):
            raise ToolError("image server returned no image")
        return ToolOutput(f"{context.label(name)} is shown to the user and attached below", [name])
