"""Explicit hub tool registry. Client functions take precedence on name collisions."""
import asyncio
import json
import secrets

from ..config import ToolsConfig
from ..trace import trace
from .base import Call, ToolContext, ToolError, ToolGroup, ToolOutput
from .store import FILES_PATH, FileStore
from .images import MADE, ImageGroup

__all__ = [
    "Call", "FILES_PATH", "FileStore", "MADE", "ToolContext", "ToolError",
    "ToolOutput", "Tools",
]


class Tools:
    def __init__(self, client, store, image_target, timeout, config=None, *, search_group=None):
        self.config = config or ToolsConfig()
        self.store = store
        if search_group is None:
            from .search import SearchGroup
            search_group = SearchGroup(self.config.search, store)
        self.groups: list[ToolGroup] = [
            ImageGroup(client, store, image_target, timeout),
            search_group,
        ]
        self.owners: dict[str, ToolGroup] = {}
        for group in self.groups:
            for name in group.actions:
                if name in self.owners:
                    raise ValueError(f"duplicate hub tool: {name}")
                self.owners[name] = group

    @property
    def owned_names(self):
        return set(self.owners)

    def reason(self, group, context):
        if context.total_actions >= self.config.total_round_limit:
            return "global hub action limit reached"
        if context.counters.get(group.id, 0) >= (group.round_limit or self.config.default_round_limit):
            return f"{group.id} action limit reached"
        return group.limit_reason(context)

    def offer(self, context=None):
        context = context or ToolContext([])
        schemas, notes = [], []
        for group in self.groups:
            if not group.enabled:
                continue
            note = group.note
            if note:
                notes.append(note)
            reason = self.reason(group, context)
            if reason:
                notes.append(f"Tool limitation: {reason}. Answer from evidence already collected; state any gaps.")
            else:
                schemas.extend(group.schemas)
        return schemas, "\n\n".join(notes)

    def call(self, tool_call):
        function = tool_call["function"]
        name = function["name"]
        try:
            args = json.loads(function.get("arguments", "{}"))
        except (ValueError, TypeError):
            args = {}
        args = args if isinstance(args, dict) else {}
        text = args.get("prompt", args.get("query", ""))
        labels = args.get("images", [])
        group = self.owners[name]
        kind, action = group.actions[name]
        progress = {"type": name, "label": name.replace("_", " ").capitalize()}
        return Call(secrets.token_hex(16), tool_call, kind, action, text if isinstance(text, str) else "",
                    [str(x) for x in labels] if isinstance(labels, list) else [], args, progress)

    async def prepare(self, call, context):
        group = self.owners[call.tool_call["function"]["name"]]
        await group.prepare(call, context)

    async def run(self, call, context):
        group = self.owners[call.tool_call["function"]["name"]]
        reason = self.reason(group, context)
        # Count attempts, including stale calls and malformed arguments, before executing anything.
        if context.total_actions >= self.config.total_round_limit:
            raise ToolError(reason)
        context.total_actions += 1
        context.counters[group.id] = context.counters.get(group.id, 0) + 1
        if not group.enabled:
            raise ToolError(f"{group.id} is disabled")
        if call.tool_call["function"]["name"] not in {s["function"]["name"] for s in group.schemas}:
            raise ToolError("tool is disabled or unavailable")
        if reason:
            raise ToolError(reason)
        return await group.run(call, context)

    async def cleanup(self, context):
        results = await asyncio.gather(*(g.cleanup(context) for g in self.groups), return_exceptions=True)
        context.state.clear()
        for group, result in zip(self.groups, results):
            if isinstance(result, BaseException):
                trace(None, "tool.cleanup_error", family=group.id, error=str(result)[:500])
        return results

    async def close(self):
        results = await asyncio.gather(*(g.close() for g in self.groups), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
