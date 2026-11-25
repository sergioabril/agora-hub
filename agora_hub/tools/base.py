from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Call:
    """One hub tool call, parsed once."""

    id: str  # 32 random hex digits; Responses item ids and the generated image's file name use it
    tool_call: dict  # the model's call: {"id", "type": "function", "function": {"name", "arguments"}}
    kind: str  # output category used by Responses
    action: str  # group-defined action
    text: str  # the prompt or the query; "" when the arguments don't give one
    labels: list[str]  # edit and view labels
    args: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    images: list[str]  # file names by label: image_1 is images[0]; each name appears once

    state: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    total_actions: int = 0
    request_id: str | None = None  # the response being answered, for tool traces

    def label(self, name: str) -> str:
        if name not in self.images:
            self.images.append(name)
        return f"image_{self.images.index(name) + 1}"


@dataclass
class ToolOutput:
    content: str  # the tool message the model reads
    images: list[str]  # FileStore names this call made or asked to see, attached for the model after the round

    progress: dict = field(default_factory=dict)
    media: dict | None = None
    web_images: list[str] = field(default_factory=list)


class ToolError(Exception):
    pass


class ToolGroup(Protocol):
    """Registration and lifecycle contract for one tool family.

    Actions retain ownership when schemas are hidden or budgets expire.
    prepare supplies progress metadata; run validates and executes arguments.
    """

    id: str
    actions: dict[str, tuple[str, str]]
    round_limit: int | None

    @property
    def enabled(self) -> bool: ...

    @property
    def schemas(self) -> list[dict]: ...

    @property
    def note(self) -> str: ...

    def limit_reason(self, context: ToolContext) -> str | None: ...

    async def prepare(self, call: Call, context: ToolContext) -> None: ...

    async def run(self, call: Call, context: ToolContext) -> ToolOutput: ...

    async def cleanup(self, context: ToolContext) -> None: ...

    async def close(self) -> None: ...
