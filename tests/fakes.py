import asyncio

from agora_hub.adapters import AdapterError, CommandResult


class FakeAdapters:
    def __init__(self):
        self.reachable: dict[str, bool] = {}
        self.readiness = {}  # deployment_id -> endpoint answers 2xx
        self.models: dict[str, list[str]] = {}  # deployment_id -> ids /models returns when ready
        self.failures: dict[tuple[str, str], AdapterError] = {}
        self.delays: dict[tuple[str, str], asyncio.Event] = {}
        self.calls: list[tuple] = []
        self.stream_lines: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.stream_exits: dict[tuple[str, str], int] = {}
        self.run_hooks: dict[tuple[str, str], object] = {}

    async def _call(self, action: str, target: str) -> None:
        self.calls.append((action, target))
        if event := self.delays.get((action, target)):
            await event.wait()
        if error := self.failures.get((action, target)):
            raise error

    async def observe(self, machine_id: str) -> None:
        await self._call("observe", machine_id)
        if not self.reachable.get(machine_id, False):
            raise AdapterError("connect", "unreachable")

    async def stream(self, machine_id, command, *, timeout, on_line):
        self.calls.append(("stream", machine_id, command))
        if event := self.delays.get(("stream", machine_id, command)) or self.delays.get(("stream", machine_id)):
            await event.wait()
        if error := self.failures.get(("stream", machine_id, command)) or self.failures.get(("stream", machine_id)):
            raise error
        for source, text in self.stream_lines.get((machine_id, command), []):
            on_line(source, text)
        if hook := self.run_hooks.get((machine_id, command)):
            hook()
        return self.stream_exits.get((machine_id, command), 0)

    async def login(self, machine) -> None:
        await self._call("login", machine.host)

    async def wake(self, machine_id: str) -> None:
        await self._call("wake", machine_id)
        self.reachable[machine_id] = True

    async def shutdown(self, machine_id: str) -> CommandResult:
        await self._call("shutdown", machine_id)
        self.reachable[machine_id] = False
        return CommandResult(0, "", "")

    async def ready(self, deployment_id: str) -> list[str]:
        await self._call("ready", deployment_id)
        if not self.readiness.get(deployment_id, False):
            raise AdapterError("http", "not ready")
        return self.models.get(deployment_id, [])
