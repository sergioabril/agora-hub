import base64
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

FILES_PATH = "/agent/files/"  # where the gateway serves FileStore files
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
FILE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp)$")
INSTRUCTIONS = (
    "Images in this conversation are labelled [image_1], [image_2], … in order. To create an image, call "
    "generate_image. To change one, call edit_image with its label. The user sees every result automatically, "
    "so never write image links or markdown for it; describe what you did in one short sentence."
)
SPECS = [
    {"type": "function", "function": {
        "name": "generate_image", "description": "Create a new image from a text description.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "What the image shows, in detail."},
        }, "required": ["prompt"]},
    }},
    {"type": "function", "function": {
        "name": "edit_image", "description": "Change an existing image from this conversation.",
        "parameters": {"type": "object", "properties": {
            "image": {"type": "string", "description": "Label of the image to change, e.g. image_2."},
            "prompt": {"type": "string", "description": "What to change."},
        }, "required": ["image", "prompt"]},
    }},
]


@dataclass
class ToolContext:
    images: list[str]  # file names by label: image_1 is images[0]; each name appears once
    base_url: str  # where browsers reach the gateway, ending in "/"

    def label(self, name: str) -> str:
        if name not in self.images:
            self.images.append(name)
        return f"image_{self.images.index(name) + 1}"

    def url(self, name: str) -> str:
        return self.base_url + FILES_PATH[1:] + name


@dataclass
class ToolOutput:
    content: str  # the tool message the model reads
    display: str  # markdown added to the reply the user sees


class ToolError(Exception):
    pass


class FileStore:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        else:
            raise ValueError("unsupported image type")
        name = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
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


class Tools:
    def __init__(
        self,
        client: httpx.AsyncClient,
        store: FileStore,
        image_target: Callable[[], tuple[str, str] | None],
        timeout: float,
    ):
        self.client, self.store, self.image_target, self.timeout = client, store, image_target, timeout

    def specs(self) -> list[dict]:
        return SPECS if self.image_target() else []

    def instructions(self) -> str:
        return INSTRUCTIONS

    def progress(self, name: str, arguments: str) -> str:
        if name != "edit_image":
            return "\n\n_generating image…_\n\n"
        try:
            label = json.loads(arguments)["image"]
        except (ValueError, TypeError, KeyError):
            label = "an image"
        return f"\n\n_editing {label}…_\n\n"

    async def run(self, name: str, arguments: str, context: ToolContext) -> ToolOutput:
        if not (target := self.image_target()):
            raise ToolError("no image deployment is running")
        base, model = target
        try:
            args = json.loads(arguments)
        except ValueError:
            args = None
        required = ("image", "prompt") if name == "edit_image" else ("prompt",)
        if not isinstance(args, dict) or any(key not in args for key in required):
            raise ToolError("invalid arguments")
        fields = {"model": model, "prompt": args["prompt"], "n": 1, "response_format": "b64_json"}
        if name == "edit_image":
            match = re.fullmatch(r"image_([1-9][0-9]*)", str(args["image"]))
            if not match or int(match[1]) > len(context.images):
                raise ToolError(f"unknown image {args['image']}")
            file = context.images[int(match[1]) - 1]
            image = (file, self.store.path(file).read_bytes(), MEDIA_TYPES[file.split(".")[1]])
            request = {"url": f"{base}/images/edits", "data": {k: str(v) for k, v in fields.items()},
                       "files": {"image": image}}
        else:
            request = {"url": f"{base}/images/generations", "json": fields}
        try:
            response = await self.client.post(**request, timeout=self.timeout)
        except httpx.RequestError as error:
            raise ToolError(f"image server: {str(error) or type(error).__name__}")
        if not response.is_success:
            raise ToolError(f"image server returned HTTP {response.status_code}")
        try:
            name = self.store.save(base64.b64decode(response.json()["data"][0]["b64_json"], validate=True))
        except (ValueError, KeyError, IndexError, TypeError):
            raise ToolError("image server returned no image")
        label = context.label(name)
        return ToolOutput(f"{label} is shown to the user", f"![{label}]({context.url(name)})\n\n")
