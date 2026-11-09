import asyncio

from agora_hub.adapters import AdapterError, CommandResult


class FakeAdapters:
    def __init__(self):
        self.reachable: dict[str, bool] = {}
        self.states: dict[str, str] = {}
        self.readiness: dict[str, bool] = {}
        self.failures: dict[tuple[str, str], AdapterError] = {}
        self.delays: dict[tuple[str, str], asyncio.Event] = {}
        self.calls: list[tuple] = []
        self.run_results: dict[tuple[str, str], CommandResult] = {}
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

    async def run(self, machine_id, command, *, cwd=None, timeout, stdin=None):
        self.calls.append(("run", machine_id, command))
        if event := self.delays.get(("run", machine_id, command)) or self.delays.get(("run", machine_id)):
            await event.wait()
        if error := self.failures.get(("run", machine_id, command)) or self.failures.get(("run", machine_id)):
            raise error
        if hook := self.run_hooks.get((machine_id, command)):
            hook()
        return self.run_results.get((machine_id, command), CommandResult(0, "", ""))

    async def wake(self, machine_id: str) -> None:
        await self._call("wake", machine_id)
        self.reachable[machine_id] = True

    async def shutdown(self, machine_id: str) -> CommandResult:
        await self._call("shutdown", machine_id)
        self.reachable[machine_id] = False
        return CommandResult(0, "", "")

    async def deployment_status(self, deployment_id: str) -> str:
        await self._call("deployment_status", deployment_id)
        return self.states.get(deployment_id, "stopped")

    async def ready(self, deployment_id: str) -> None:
        await self._call("ready", deployment_id)
        if not self.readiness.get(deployment_id, False):
            raise AdapterError("http", "not ready")
