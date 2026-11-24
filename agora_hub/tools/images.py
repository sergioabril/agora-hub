from collections.abc import Callable
import base64
import re

import httpx

from .base import Call, ToolContext, ToolOutput, ToolError
from .store import MEDIA_TYPES, FileStore

VISION_NOTE = (
    "Images in this conversation are labelled [image_1], [image_2], … in order. You see the recent ones. An older one "
    'is marked "not attached": you cannot see it until you call view_image with its label, so call it before you '
    "describe that image or answer a question about how it looks."
)
IMAGE_NOTE = (
    "To create an image, call generate_image. To change an image, or to combine up to four, call edit_image with "
    "their labels: it works on any image, attached or not. You see each result after the call: if it clearly misses "
    "the request, fix it once with edit_image; otherwise don't retry. The user sees every result automatically, so "
    "never write image links or markdown for it; describe what you did in one short sentence."
)
MADE = "{} is shown to the user"  # the tool message of a call that made an image: {} is its label
VIEWED = "You can see {} now."  # the tool message of view_image: {} is the labels
# The image server encodes the result itself; the hub converts nothing. Its default is an uncompressed PNG (7 MB for
# 1536×1152), which hit the LLM's 20 MB image limit after three images and slowed the app. JPEG at quality 95 is
# about a tenth of that. For lossless images use {"output_format": "png", "output_compression": 1}: a third to a half
# of the default size, and about 3 s more per image.
IMAGE_OUTPUT = {"output_format": "jpeg", "output_compression": 95}
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
VIEW_SPEC = spec("view_image", 'Look at images that are marked "not attached".',
                 images={"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4,
                         "description": 'Labels of the images, e.g. ["image_1"].'})


class ImageGroup:
    id = "images"
    actions = {
        "generate_image": ("image", "generate"),
        "edit_image": ("image", "edit"),
        "view_image": ("view", "view"),
    }
    enabled = True
    round_limit = None

    def __init__(
        self,
        client: httpx.AsyncClient,
        store: FileStore,
        image_target: Callable[[], tuple[str, str] | None],
        timeout: float,
    ):
        self.client, self.store, self.image_target, self.timeout = client, store, image_target, timeout

    @property
    def schemas(self):
        return [*IMAGE_SPECS, VIEW_SPEC] if self.image_target() else [VIEW_SPEC]

    @property
    def note(self):
        return VISION_NOTE + ("\n\n" + IMAGE_NOTE if self.image_target() else "")

    async def prepare(self, call, context):
        pass

    def limit_reason(self, context):
        return None

    async def cleanup(self, context):
        pass

    async def close(self):
        pass

    def _files(self, call: Call, context: ToolContext) -> list[str]:
        """The files a call's labels name. Any image of the conversation counts, attached for the model or not."""
        if not 1 <= len(call.labels) <= 4:
            raise ToolError(f"{call.tool_call['function']['name']} takes one to four images")
        files = []
        for label in call.labels:
            match = re.search(r"image_([1-9][0-9]*)", label)  # search: the model may copy the brackets or the mark
            if not match or int(match[1]) > len(context.images):
                raise ToolError(f"unknown image {label}")
            files.append(context.images[int(match[1]) - 1])
        return files

    async def run(self, call: Call, context: ToolContext) -> ToolOutput:
        if call.kind == "view":
            files = self._files(call, context)
            return ToolOutput(VIEWED.format(", ".join(f"[{context.label(file)}]" for file in files)), files)
        if not (target := self.image_target()):
            raise ToolError("no image deployment is running")
        if not call.text:
            raise ToolError("invalid arguments")
        base, model = target
        fields = {"model": model, "prompt": call.text, "n": 1, "response_format": "b64_json", **IMAGE_OUTPUT}
        if call.action == "edit":
            images = [("image", (file, self.store.path(file).read_bytes(), MEDIA_TYPES[file.split(".")[1]]))
                      for file in self._files(call, context)]
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
        self.store.save_call(call.id, call.tool_call)
        return ToolOutput(MADE.format(f"[{context.label(name)}]"), [name])
